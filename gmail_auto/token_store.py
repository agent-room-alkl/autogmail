"""把 Gmail 授权存在 Vercel KV / Upstash，而不是写在会消失的磁盘上。"""

import json
import os
import urllib.error
import urllib.request

from gmail_auto.errors import ConfigError

_KEY = "autogmail:token"


def storage_ready() -> bool:
    return _redis() is not None


def load_token_raw() -> str | None:
    if not storage_ready():
        return None
    try:
        result = _command(["GET", _KEY])
    except Exception as exc:
        raise ConfigError("读不到已保存的 Gmail 授权。请检查 Vercel KV 是否已连接。") from exc
    if not result:
        return None
    return str(result)


def save_token_raw(raw: str) -> bool:
    if not storage_ready():
        return False
    try:
        _command(["SET", _KEY, raw])
    except Exception as exc:
        raise ConfigError("Gmail 授权没有保存成功。请检查 Vercel KV 是否已连接。") from exc
    return True


def _redis() -> tuple[str, str] | None:
    url = (os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL") or "").strip().rstrip("/")
    token = (os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if not url or not token:
        return None
    return url, token


def _command(command: list[str]):
    pair = _redis()
    if pair is None:
        return None
    url, token = pair
    request = urllib.request.Request(
        url,
        data=json.dumps(command).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ConfigError("连不上 Vercel KV。") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ConfigError("Vercel KV 没有完成这次读写。")
    return payload.get("result")
