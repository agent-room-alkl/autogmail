import json
import os
import re
from datetime import datetime

from gmail_auto.errors import ConfigError
from gmail_auto.paths import atomic_write, data_dir

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def live_from_env() -> bool | None:
    flag = os.getenv("LIVE_SEND", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return None


def is_live() -> bool:
    forced = live_from_env()
    if forced is not None:
        return forced
    data = _read(data_dir(create=False) / "state.json", {"live_send": False})
    return bool(isinstance(data, dict) and data.get("live_send"))


def set_live(live: bool) -> None:
    _write(data_dir() / "state.json", {"live_send": bool(live)})


def load_processed() -> dict:
    data = _read(data_dir(create=False) / "processed.json", {})
    if not isinstance(data, dict):
        raise ConfigError("data/processed.json 格式不对，程序没有改它。")
    return data


def get_processed(message_id: str) -> dict | None:
    item = load_processed().get(message_id)
    return item if isinstance(item, dict) else None


def mark_processed(record: dict) -> None:
    path = data_dir() / "processed.json"
    data = _read(path, {})
    if not isinstance(data, dict):
        raise ConfigError("data/processed.json 格式不对，程序没有改它。")
    data[record["id"]] = record
    _write(path, data)


def list_processed(limit: int = 25) -> list[dict]:
    rows = [item for item in load_processed().values() if isinstance(item, dict)]
    rows.sort(key=lambda item: item.get("processed_at", ""), reverse=True)
    return rows[:limit]


def bump_failure(message_id: str) -> int:
    path = data_dir() / "failures.json"
    data = _read(path, {})
    if not isinstance(data, dict):
        data = {}
    count = int(data.get(message_id, 0) or 0) + 1
    data[message_id] = count
    _write(path, data)
    return count


def clear_failure(message_id: str) -> None:
    path = data_dir(create=False) / "failures.json"
    if not path.exists():
        return
    data = _read(path, {})
    if isinstance(data, dict) and message_id in data:
        data.pop(message_id, None)
        _write(path, data)


def load_last_run() -> dict | None:
    data = _read(data_dir(create=False) / "last_run.json", None)
    return data if isinstance(data, dict) else None


def save_last_run(payload: dict) -> None:
    _write(data_dir() / "last_run.json", payload)


def write_draft(mail, reply: str, language: str) -> str:
    name = safe_id(mail.id) + ".txt"
    path = data_dir() / "outbox" / name
    who = mail.from_name or mail.from_email or "未知发件人"
    if mail.from_name and mail.from_email:
        who = f"{mail.from_name} <{mail.from_email}>"
    text = (
        f"发件人：{who}\n"
        f"主题：{mail.subject or '（无主题）'}\n"
        f"时间：{mail.date}\n"
        f"语言：{'中文' if language == 'zh' else '英文'}\n"
        "说明：演练稿，没有发送。\n\n"
        f"{reply}\n"
    )
    atomic_write(path, text)
    return f"data/outbox/{name}"


def safe_id(value: str) -> str:
    cleaned = _SAFE_ID.sub("_", value or "")
    if not cleaned:
        raise ConfigError("邮件编号无效。")
    return cleaned


def _read(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} 损坏了，程序没有改它。") from exc


def _write(path, data) -> None:
    try:
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        if os.environ.get("VERCEL"):
            return
        raise ConfigError("暂时写不了本机记录。")
