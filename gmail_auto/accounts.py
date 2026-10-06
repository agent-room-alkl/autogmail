"""邮箱账号和验证码。线上放在 KV，本机放在 data/accounts.json。"""

import hashlib
import json
import os
import secrets
import time
from datetime import datetime

from gmail_auto.errors import ConfigError
from gmail_auto.paths import atomic_write, data_dir
from gmail_auto.token_store import kv_get, kv_set, storage_ready

_KEY = "autogmail:accounts"
_CODE_TTL = 10 * 60
_RESEND_WAIT = 60
_MAX_TRIES = 5
STALE_AFTER = 6 * 24 * 60 * 60


def normalize_email(value: str) -> str:
    return value.strip().lower()


def start_verification(email: str, purpose: str) -> str:
    email = normalize_email(email)
    purpose = _purpose(purpose)
    _ensure_allowed(email)
    data = _load()
    users = data.setdefault("users", {})
    if purpose == "register" and email in users:
        raise ConfigError("这个邮箱已经注册，请直接登录。")
    if purpose == "login" and email not in users:
        raise ConfigError("这个邮箱还没注册。")
    pending = data.setdefault("otp", {}).get(email) or {}
    sent_at = float(pending.get("sent_at") or 0)
    if sent_at and time.time() - sent_at < _RESEND_WAIT:
        raise ConfigError("验证码刚刚发过，请等一分钟再获取。")
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    data.setdefault("otp", {})[email] = {
        "purpose": purpose,
        "salt": salt,
        "hash": _digest(salt, code),
        "sent_at": time.time(),
        "expires": time.time() + _CODE_TTL,
        "tries": 0,
    }
    _save(data)
    return code


def finish_verification(email: str, purpose: str, code: str) -> str:
    email = normalize_email(email)
    purpose = _purpose(purpose)
    entered = "".join(ch for ch in code if ch.isdigit())
    data = _load()
    pending = data.setdefault("otp", {}).get(email)
    if not pending or pending.get("purpose") != purpose:
        raise ConfigError("请先获取验证码。")
    if time.time() > float(pending.get("expires") or 0):
        data["otp"].pop(email, None)
        _save(data)
        raise ConfigError("验证码已过期，请重新获取。")
    if int(pending.get("tries") or 0) >= _MAX_TRIES:
        data["otp"].pop(email, None)
        _save(data)
        raise ConfigError("验证码已失效，请重新获取。")
    salt = str(pending.get("salt") or "")
    if not secrets.compare_digest(_digest(salt, entered), str(pending.get("hash") or "")):
        pending["tries"] = int(pending.get("tries") or 0) + 1
        _save(data)
        raise ConfigError("验证码不对。")
    data["otp"].pop(email, None)
    now = time.time()
    users = data.setdefault("users", {})
    if purpose == "register":
        users[email] = {"created_at": now, "last_login": now}
    else:
        record = users.get(email)
        if not isinstance(record, dict):
            raise ConfigError("这个邮箱还没注册。")
        record["last_login"] = now
    _save(data)
    return email


def clear_pending(email: str) -> None:
    data = _load()
    data.setdefault("otp", {}).pop(normalize_email(email), None)
    _save(data)


def mark_gmail_authorized(email: str, at: float | None = None) -> None:
    data = _load()
    data["gmail"] = {"email": normalize_email(email) if email else "", "authorized_at": time.time() if at is None else at}
    _save(data)


def gmail_status() -> dict:
    data = _load()
    gmail = data.get("gmail") if isinstance(data.get("gmail"), dict) else {}
    authorized_at = float(gmail.get("authorized_at") or 0)
    return {
        "email": str(gmail.get("email") or ""),
        "authorized_at": authorized_at,
        "authorized_text": _when(authorized_at),
        "stale": bool(authorized_at) and time.time() - authorized_at > STALE_AFTER,
    }


def _purpose(value: str) -> str:
    if value not in {"login", "register"}:
        raise ConfigError("请选择登录或注册。")
    return value


def _ensure_allowed(email: str) -> None:
    raw = os.getenv("ALLOWED_EMAILS", "").strip()
    if not raw:
        return
    allowed = {normalize_email(item) for item in raw.split(",") if item.strip()}
    if email not in allowed:
        raise ConfigError("这个邮箱不能用来登录。")


def _digest(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def _when(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _load() -> dict:
    if os.environ.get("VERCEL") or storage_ready():
        if not storage_ready():
            return {"users": {}, "otp": {}, "gmail": {}}
        raw = kv_get(_KEY)
        return _parse(raw)
    path = data_dir(create=False) / "accounts.json"
    if not path.exists():
        return {"users": {}, "otp": {}, "gmail": {}}
    try:
        return _parse(path.read_text(encoding="utf-8"))
    except OSError:
        return {"users": {}, "otp": {}, "gmail": {}}


def _parse(raw) -> dict:
    if not raw:
        return {"users": {}, "otp": {}, "gmail": {}}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"users": {}, "otp": {}, "gmail": {}}
    if not isinstance(data, dict):
        return {"users": {}, "otp": {}, "gmail": {}}
    data.setdefault("users", {})
    data.setdefault("otp", {})
    data.setdefault("gmail", {})
    return data


def _save(data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    if os.environ.get("VERCEL") or storage_ready():
        if not storage_ready():
            raise ConfigError("验证码没法保存。请先给这个项目接上 Vercel KV。")
        kv_set(_KEY, raw)
        return
    atomic_write(data_dir() / "accounts.json", raw + "\n")
