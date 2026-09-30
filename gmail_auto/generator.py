import json
import os
import re

from gmail_auto.envfile import openai_base_url, openai_model, require_openai_key
from gmail_auto.errors import ReplyError
from gmail_auto.filters import detect_language
from gmail_auto.models import Mail
from gmail_auto.profile_store import profile_as_prompt

_SYSTEM = """你在代写信的人回复他或她自己的电子邮件。你不是助手，也不要提到自己是模型。

必须遵守：
- 只用第一人称，像本人亲手写的。
- 先回应来信里的具体事情，不要用空泛客套开头。
- 整封回复只用一种语言。要求中文时，从头到尾都用中文；要求英文时，从头到尾都用英文。
- 只能使用个人资料里写明的事实。没有写到的经历、职务、电话、住址、时间安排和承诺，都不要编。
- 资料里没有的内容，就说还不能确认，或按「不能承诺的事」婉拒。不要为了显得有帮助而补细节。
- 可以称呼对方来信里的名字，但第一句就进入正事。
- 语气靠近资料里的描述，可以自然化用「本人会说的话」，不要把和这封来信无关的句子硬塞进去。
- 不要大段引用原信，不要写主题，不要解释你在做什么。
- 只输出回复正文。"""

_AI = re.compile(
    r"(作为\s*(?:一个\s*)?(?:AI|人工智能)|我是\s*(?:一个\s*)?(?:AI|语言模型|人工智能)|"
    r"as an ai|i am an ai|i'm an ai|language model)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def generate_reply(mail: Mail, profile: dict, language: str) -> str:
    try:
        from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI, RateLimitError
    except ImportError as exc:
        raise ReplyError("还没安装 openai 库。请先运行 pip install -r requirements.txt。") from exc

    kwargs = {"api_key": require_openai_key(), "timeout": 60.0, "max_retries": 0}
    base_url = openai_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    try:
        response = client.chat.completions.create(
            model=openai_model(),
            temperature=0.4,
            max_tokens=700,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _user_prompt(mail, profile, language)},
            ],
        )
    except AuthenticationError as exc:
        raise ReplyError("OpenAI 密钥无效，没有生成回复。") from exc
    except RateLimitError as exc:
        raise ReplyError("OpenAI 请求太频繁，请稍后再试。") from exc
    except APIConnectionError as exc:
        raise ReplyError("连接不上 OpenAI。") from exc
    except APIStatusError as exc:
        code = getattr(exc, "status_code", "")
        raise ReplyError(f"OpenAI 返回了错误（{code}）。请检查模型名 OPENAI_MODEL 和密钥。") from exc

    reply = clean_reply(_message_text(response))
    ensure_safe(reply, profile, language, mail)
    return reply


def ensure_safe(reply: str, profile: dict, language: str, mail: Mail | None = None) -> None:
    text = (reply or "").strip()
    if not text:
        raise ReplyError("模型返回了空回复。")
    if len(text) > 5000:
        raise ReplyError("回复异常地长，已丢弃。")
    key = os.getenv("OPENAI_API_KEY", "")
    if key and len(key) > 8 and key in text:
        raise ReplyError("回复里出现了密钥，已丢弃。")
    if re.search(r"sk-[A-Za-z0-9]{10,}", text):
        raise ReplyError("回复里出现了疑似密钥的内容，已丢弃。")
    if _AI.search(text):
        raise ReplyError("回复不像本人写的，已丢弃。")
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    if language == "zh":
        if detect_language(text) != "zh" or cjk < 2:
            raise ReplyError("回复没有整封使用中文，已拦截。")
    elif detect_language(text) != "en":
        raise ReplyError("回复没有整封使用英文，已拦截。")
    if _unknown_number(text, profile):
        raise ReplyError("回复里出现了资料中没有的号码，已拦截。")
    context = _context(profile, mail).lower()
    if any(url.lower() not in context for url in _URL.findall(text)):
        raise ReplyError("回复里出现了资料中没有的链接，已拦截。")
    if any(addr.lower() not in context for addr in _EMAIL.findall(text)):
        raise ReplyError("回复里出现了资料中没有的邮箱，已拦截。")


def clean_reply(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        cleaned = cleaned.strip()
    cleaned = re.sub(r"(?i)^(subject|主题)\s*[:：]\s*.*(?:\n+|$)", "", cleaned, count=1)
    cleaned = re.sub(r"(?i)^(回复|reply)\s*[:：]\s*", "", cleaned, count=1)
    return cleaned.strip()


def _user_prompt(mail: Mail, profile: dict, language: str) -> str:
    if language == "zh":
        lock = "这封来信按中文处理。整封回复只能使用中文，不要夹英文句子。"
    else:
        lock = "This letter is English. Write the entire reply in English only."
    return (
        "个人资料（只能使用这里的事实）：\n"
        f"{profile_as_prompt(profile)}\n\n"
        "来信：\n"
        f"对方：{mail.from_name or mail.from_email}\n"
        f"邮箱：{mail.from_email}\n"
        f"主题：{mail.subject or '（无主题）'}\n"
        "正文：\n"
        f"{_clip(mail.body)}\n\n"
        f"{lock}\n"
        "请只输出回复正文。"
    )


def _message_text(response) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise ReplyError("模型没有返回内容。") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(getattr(part, "text", "") or ""))
        return "".join(parts)
    return ""


def _unknown_number(reply: str, profile: dict) -> bool:
    allowed = re.sub(r"\D", "", json.dumps(profile, ensure_ascii=False))
    for digits in re.findall(r"\d{7,}", reply):
        if len(digits) == 8 and digits.startswith(("19", "20")):
            continue
        if digits not in allowed:
            return True
    for match in re.finditer(r"\d{3,4}[\s\-]\d{3,4}[\s\-]\d{3,4}", reply):
        digits = re.sub(r"\D", "", match.group())
        if digits and digits not in allowed:
            return True
    return False


def _context(profile: dict, mail: Mail | None) -> str:
    parts = [json.dumps(profile, ensure_ascii=False)]
    if mail is not None:
        parts.extend([mail.from_email, mail.reply_email, mail.subject, mail.body])
    return "\n".join(parts)


def _clip(text: str, limit: int = 6000) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "\n（正文在这里截断）"
