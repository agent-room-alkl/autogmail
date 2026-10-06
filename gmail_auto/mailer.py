"""把登录验证码发到用户填写的邮箱。"""

import os
import smtplib
from email.message import EmailMessage

from gmail_auto.errors import ConfigError


def deliver_code(to: str, code: str) -> None:
    subject = "邮件自动回复验证码"
    body = f"验证码：{code}\n\n10 分钟内有效。如果不是你本人操作，忽略这封邮件。"
    if _smtp_ready():
        _send_smtp(to, subject, body)
        return
    if _send_via_gmail(to, subject, body):
        return
    raise ConfigError("验证码发不出去。请在 Vercel 设置 SMTP_USER 和 SMTP_PASSWORD。")


def _smtp_ready() -> bool:
    return bool(os.getenv("SMTP_USER", "").strip() and os.getenv("SMTP_PASSWORD", "").strip())


def _send_smtp(to: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT", "587") or "587")
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("MAIL_FROM", "").strip() or user
    message = EmailMessage()
    message["To"] = to
    message["From"] = sender
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise ConfigError("验证码没有发出去。请检查 SMTP 账号和密码。") from exc


def _send_via_gmail(to: str, subject: str, body: str) -> bool:
    try:
        from gmail_auto.gmail_client import GmailClient

        GmailClient(allow_browser=False).send_notice(to, subject, body)
    except ConfigError:
        return False
    return True
