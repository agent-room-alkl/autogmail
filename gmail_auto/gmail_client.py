import base64
import html
import json
import re
from email.message import EmailMessage
from email.header import decode_header
from email.utils import formataddr, parseaddr

from gmail_auto.console import say
from gmail_auto.errors import ConfigError
from gmail_auto.filters import reply_subject
from gmail_auto.models import Mail
from gmail_auto.paths import atomic_write, root

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
UNREAD_QUERY = (
    "is:unread in:inbox -category:promotions -category:social "
    "-category:forums -in:chats -in:spam -in:trash"
)
RECENT_QUERY = "in:inbox -in:chats -in:spam -in:trash"


class GmailClient:
    def __init__(self, allow_browser: bool = False):
        creds = load_credentials(allow_browser=allow_browser)
        self.service = _build_service(creds)
        self._email = ""

    def get_user_email(self) -> str:
        if not self._email:
            try:
                profile = self.service.users().getProfile(userId="me").execute()
            except Exception as exc:
                raise _translate(exc) from exc
            self._email = (profile.get("emailAddress") or "").strip()
        if not self._email:
            raise ConfigError("没有读到当前 Gmail 地址。")
        return self._email

    def list_unread(self, limit: int = 20) -> list[Mail]:
        return self._list(UNREAD_QUERY, limit)

    def list_recent(self, limit: int = 25) -> list[Mail]:
        return self._list(RECENT_QUERY, limit)

    def send_reply(self, mail: Mail, body: str, user_email: str) -> str:
        payload = build_send_body(mail, body, user_email)
        try:
            sent = self.service.users().messages().send(userId="me", body=payload).execute()
        except Exception as exc:
            raise _translate(exc) from exc
        return sent.get("id", "")

    def mark_read(self, message_id: str) -> None:
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception as exc:
            raise _translate(exc) from exc

    def _list(self, query: str, limit: int) -> list[Mail]:
        size = max(1, min(int(limit), 25))
        try:
            listed = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=size)
                .execute()
            )
        except Exception as exc:
            raise _translate(exc) from exc
        mails = []
        for item in listed.get("messages", []):
            try:
                full = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=item["id"], format="full")
                    .execute()
                )
            except Exception as exc:
                raise _translate(exc) from exc
            mails.append(parse_message(full))
        return mails


def load_credentials(allow_browser: bool):
    token_path = root() / "token.json"
    creds = None
    if token_path.exists():
        try:
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise ConfigError("还没安装依赖。请先运行 pip install -r requirements.txt。") from exc
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, OSError) as exc:
            if not allow_browser:
                raise ConfigError("token.json 打不开。请删掉后重新运行程序。这次没有打开浏览器。") from exc
            creds = None
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                atomic_write(token_path, creds.to_json())
            except RefreshError as exc:
                token_path.unlink(missing_ok=True)
                creds = None
                if not allow_browser:
                    raise ConfigError(
                        "本机保存的 Gmail 授权已失效。请重新运行程序，再在浏览器里同意一次。这次没有打开浏览器。"
                    ) from exc
            except Exception as exc:
                raise ConfigError("暂时刷新不了 Gmail 授权，请检查网络后再试。这次没有打开浏览器。") from exc
        if creds and creds.valid and creds.has_scopes(SCOPES):
            return creds

    if not allow_browser:
        if creds and not creds.has_scopes(SCOPES):
            raise ConfigError("已保存的授权缺少读信或发信权限。请删掉 token.json 后重新运行。")
        raise ConfigError("还没有完成本机 Gmail 授权。请重新运行程序，并在弹出的浏览器里同意一次。")

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_file = find_client_file()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError("Gmail 凭证打不开。请使用「桌面应用」下载的 JSON。") from exc
    say("正在打开浏览器，请用你自己的 Gmail 同意一次。完成后可以关掉那个页面。")
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="如果浏览器没有自动打开，请复制终端里的链接。",
        success_message="授权已完成，可以关掉这个页面。",
    )
    atomic_write(token_path, creds.to_json())
    return creds


def find_client_file():
    primary = root() / "credentials.json"
    if primary.exists():
        chosen = primary
    else:
        matches = sorted(root().glob("client_secret*.json"))
        if len(matches) > 1:
            raise ConfigError("目录里有多份 client_secret 文件。请只留一份，或改名为 credentials.json。")
        if len(matches) == 1:
            chosen = matches[0]
        else:
            raise ConfigError(
                "没有找到 Gmail 桌面应用凭证。请把下载的 JSON 放在程序目录，并命名为 credentials.json。"
            )
    try:
        payload = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("Gmail 凭证文件打不开。请重新下载「桌面应用」的 JSON。") from exc
    if not isinstance(payload, dict) or "installed" not in payload:
        raise ConfigError("这份凭证不是桌面应用。请在 Google Cloud 里创建 OAuth 客户端，类型选「桌面应用」。")
    return chosen


def parse_message(raw: dict) -> Mail:
    payload = raw.get("payload") or {}
    headers: dict[str, str] = {}
    for item in payload.get("headers") or []:
        name = (item.get("name") or "").strip().lower()
        if name and name not in headers:
            headers[name] = decode_mime_header(item.get("value") or "")
    from_name, from_email = parseaddr(headers.get("from", ""))
    _, reply_email = parseaddr(headers.get("reply-to", ""))
    internal = raw.get("internalDate")
    if internal:
        from datetime import datetime

        date = datetime.fromtimestamp(int(internal) / 1000).strftime("%Y-%m-%d %H:%M")
    else:
        date = headers.get("date", "")
    return Mail(
        id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        from_name=from_name.strip(),
        from_email=from_email.strip(),
        reply_email=reply_email.strip(),
        subject=headers.get("subject", "").strip(),
        date=date,
        body=extract_body(payload).strip(),
        headers=headers,
        label_ids=list(raw.get("labelIds") or []),
        snippet=raw.get("snippet") or "",
    )


def extract_body(payload: dict) -> str:
    plain: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        disposition = ""
        for item in part.get("headers") or []:
            if (item.get("name") or "").lower() == "content-disposition":
                disposition = item.get("value") or ""
        if "attachment" in disposition.lower():
            return
        data = (part.get("body") or {}).get("data")
        mime = part.get("mimeType") or ""
        if data and mime == "text/plain":
            plain.append(decode_b64(data))
        elif data and mime == "text/html":
            html_parts.append(decode_b64(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html_parts:
        return strip_html("\n".join(html_parts))
    data = (payload.get("body") or {}).get("data")
    if not data:
        return ""
    text = decode_b64(data)
    if "html" in (payload.get("mimeType") or ""):
        return strip_html(text)
    return text


def strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def decode_b64(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    chunks = []
    for text, encoding in decode_header(value):
        if isinstance(text, bytes):
            chunks.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            chunks.append(text)
    return "".join(chunks)


def build_reply_message(mail: Mail, body: str, user_email: str) -> EmailMessage:
    recipient = (mail.reply_email or mail.from_email).strip()
    if not recipient:
        raise ConfigError("这封信没有可回复的地址。")
    message = EmailMessage()
    if mail.reply_email:
        message["To"] = recipient
    elif mail.from_name:
        message["To"] = formataddr((mail.from_name, recipient))
    else:
        message["To"] = recipient
    message["From"] = user_email
    message["Subject"] = reply_subject(mail.subject)
    message_id = (mail.headers.get("message-id") or "").strip()
    references = (mail.headers.get("references") or "").strip()
    if message_id:
        message["In-Reply-To"] = message_id
        message["References"] = f"{references} {message_id}".strip()
    message.set_content(body, subtype="plain", charset="utf-8")
    return message


def build_send_body(mail: Mail, body: str, user_email: str) -> dict:
    message = build_reply_message(mail, body, user_email)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    payload = {"raw": raw}
    if mail.thread_id:
        payload["threadId"] = mail.thread_id
    return payload


def _build_service(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _translate(exc: Exception) -> ConfigError:
    if isinstance(exc, ConfigError):
        return exc
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
    if status in (401, 403):
        return ConfigError("Gmail 拒绝了这次访问。请确认授权的是你自己的邮箱，并且已启用 Gmail API。")
    if status == 429:
        return ConfigError("Gmail 请求太频繁，请过一会儿再试。")
    return ConfigError("访问 Gmail 失败。")
