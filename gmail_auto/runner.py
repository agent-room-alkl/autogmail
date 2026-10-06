import threading
import time
from dataclasses import dataclass, field

from gmail_auto.console import say
from gmail_auto.envfile import require_openai_key
from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.filters import Allow, Skip, classify, short_subject
from gmail_auto.models import Mail
from gmail_auto.profile_store import load_profile, profile_is_placeholder
from gmail_auto.store import (
    bump_failure,
    clear_failure,
    get_processed,
    is_live,
    live_from_env,
    mark_processed,
    now_text,
    save_last_run,
    set_live,
    write_draft,
)

CHECK_INTERVAL_SECONDS = 60 * 60
LOCK = threading.Lock()


@dataclass
class RunReport:
    live: bool
    finished_at: str
    checked: int = 0
    sent: int = 0
    drafted: int = 0
    skipped: int = 0
    failed: int = 0
    lines: list[str] = field(default_factory=list)


def run_once(gmail=None, generate=None) -> RunReport:
    with LOCK:
        return _execute(gmail, generate)


def toggle_and_run(confirmed: bool, gmail=None, generate=None) -> RunReport:
    with LOCK:
        if live_from_env() is not None:
            raise ConfigError("真实发送由环境变量 LIVE_SEND 控制。改它之后重新部署即可。")
        if not is_live():
            if not confirmed:
                raise ConfigError("开启真实发送前，请勾选确认。")
            profile = load_profile()
            if profile_is_placeholder(profile):
                raise ConfigError("个人资料还是占位示例。请先写成你自己的事实，再开启真实发送。")
            require_openai_key()
            set_live(True)
        else:
            set_live(False)
        return _execute(gmail, generate)


def start_scheduler() -> None:
    def loop() -> None:
        time.sleep(5)
        while True:
            try:
                run_once()
            except ConfigError:
                pass
            except Exception as exc:
                text = str(exc).strip().replace("\n", " ")
                if len(text) > 300:
                    text = text[:300] + "…"
                say(f"这一轮没有完成：{text or '未知错误'}")
            time.sleep(CHECK_INTERVAL_SECONDS)

    threading.Thread(target=loop, name="gmail-check", daemon=True).start()


def format_report(report: RunReport) -> str:
    mode = "真实发送" if report.live else "演练"
    text = (
        f"[{report.finished_at}] {mode}：检查 {report.checked} 封，"
        f"写到本地 {report.drafted} 封，已发送 {report.sent} 封，"
        f"跳过 {report.skipped} 封，失败 {report.failed} 封。"
    )
    if report.checked == 0 and not report.lines:
        text += " 没有新的未读信。"
    if report.lines:
        text += "\n" + "\n".join(f"- {line}" for line in report.lines)
    return text


def _execute(gmail=None, generate=None) -> RunReport:
    try:
        report = _run_locked(gmail, generate)
    except ConfigError as exc:
        say(str(exc))
        save_last_run(
            {
                "finished_at": now_text(),
                "live": is_live(),
                "summary": "这一轮没有开始。",
                "lines": [],
                "error": str(exc),
            }
        )
        raise
    save_last_run(_report_dict(report))
    say(format_report(report))
    return report


def _run_locked(gmail=None, generate=None) -> RunReport:
    profile = load_profile()
    live = is_live()
    if live and profile_is_placeholder(profile):
        set_live(False)
        raise ConfigError("个人资料还是占位示例，已关掉真实发送。请先写成你自己的事实。")
    if generate is None:
        require_openai_key()
        from gmail_auto.generator import generate_reply

        generate = generate_reply
    if gmail is None:
        from gmail_auto.gmail_client import GmailClient

        gmail = GmailClient(allow_browser=False)

    user = gmail.get_user_email()
    mails = gmail.list_unread(20)
    report = RunReport(live=live, finished_at=now_text())
    already = 0
    for mail in mails:
        if get_processed(mail.id):
            already += 1
            continue
        report.checked += 1
        decision = classify(mail, user)
        if isinstance(decision, Skip):
            _skip(gmail, mail, decision, report)
            continue
        if not isinstance(decision, Allow):
            continue
        try:
            reply = generate(mail, profile, decision.language)
        except ReplyError as exc:
            _fail(gmail, mail, report, str(exc))
            continue
        try:
            if live:
                gmail.send_reply(mail, reply, user)
                mark_processed(_record(mail, "sent", "", reply, decision.language, False))
                clear_failure(mail.id)
                report.sent += 1
                report.lines.append(f"已回复：{short_subject(mail.subject)}")
                _remember(gmail, mail.id, done=True)
                try:
                    gmail.mark_read(mail.id)
                except ConfigError:
                    report.lines.append(f"已回复 {short_subject(mail.subject)}，但没有标成已读。")
            else:
                outbox = write_draft(mail, reply, decision.language)
                mark_processed(_record(mail, "drafted", "", reply, decision.language, False, outbox))
                clear_failure(mail.id)
                report.drafted += 1
                report.lines.append(f"已写入本地：{short_subject(mail.subject)}")
        except ConfigError as exc:
            _fail(gmail, mail, report, str(exc))
    if already and report.checked == 0:
        report.lines.append(f"有 {already} 封未读信已经处理过，没有再次回复。")
    report.lines = report.lines[:40]
    return report


def _skip(gmail, mail: Mail, decision: Skip, report: RunReport) -> None:
    mark_processed(_record(mail, "skipped", decision.reason, "", "", decision.sensitive))
    _remember(gmail, mail.id, done=True)
    report.skipped += 1
    if decision.sensitive:
        report.lines.append("跳过一封涉及密码、验证码、证件或银行卡的信。")
    else:
        report.lines.append(f"跳过：{short_subject(mail.subject)}（{decision.reason}）")


def _fail(gmail, mail: Mail, report: RunReport, message: str) -> None:
    already = False
    has_retry = getattr(gmail, "has_retry", None)
    if has_retry is not None:
        try:
            already = bool(has_retry(mail))
        except Exception:
            already = False
    try:
        count = bump_failure(mail.id)
    except ConfigError:
        count = 1
    if already:
        count = max(count, 2)
    subject = short_subject(mail.subject)
    if count >= 2:
        mark_processed(_record(mail, "skipped", f"{message} 已停止重复处理。", "", "", False))
        _remember(gmail, mail.id, done=True)
        report.skipped += 1
        report.lines.append(f"停止处理：{subject}（{message}）")
        return
    _remember(gmail, mail.id, done=False)
    report.failed += 1
    report.lines.append(f"这次没写成：{subject}（{message}）下一轮会再试一次。")


def _remember(gmail, message_id: str, done: bool) -> None:
    note = getattr(gmail, "note_done" if done else "note_retry", None)
    if note is None:
        return
    try:
        note(message_id)
    except Exception:
        return


def _record(mail: Mail, action: str, reason: str, reply: str, language: str, sensitive: bool, outbox: str = "") -> dict:
    return {
        "id": mail.id,
        "thread_id": "" if sensitive else mail.thread_id,
        "from_name": "" if sensitive else mail.from_name,
        "from_email": "" if sensitive else mail.from_email,
        "subject": "" if sensitive else mail.subject,
        "date": mail.date,
        "action": action,
        "reason": reason,
        "reply": "" if sensitive else reply,
        "language": language,
        "processed_at": now_text(),
        "sensitive": sensitive,
        "outbox": "" if sensitive else outbox,
    }


def _report_dict(report: RunReport) -> dict:
    return {
        "finished_at": report.finished_at,
        "live": report.live,
        "summary": format_report(report).split("\n", 1)[0],
        "lines": report.lines,
        "error": "",
        "checked": report.checked,
        "sent": report.sent,
        "drafted": report.drafted,
        "skipped": report.skipped,
        "failed": report.failed,
    }
