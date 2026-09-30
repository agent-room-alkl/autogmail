import json
import re

from gmail_auto.errors import ConfigError, ProfileError
from gmail_auto.paths import atomic_write, root

_SECRET = re.compile(r"sk-[A-Za-z0-9]{10,}|BEGIN PRIVATE KEY")
_PLACEHOLDER_MARKS = ("请填写", "请用几句", "请描述")


def profile_path():
    return root() / "profile.json"


def clean_profile(data: object) -> dict:
    if not isinstance(data, dict):
        raise ProfileError("个人资料格式不对。")
    cleaned = {
        "name": _text(data.get("name"), "请填写身份里的名字。", 80),
        "role": _text(data.get("role"), "请填写身份。", 120),
        "introduction": _text(data.get("introduction"), "请填写自我介绍。", 4000),
        "tone": _text(data.get("tone"), "请填写语气。", 1000),
        "cannot_promise": _lines(data.get("cannot_promise"), "请至少写一件不能承诺的事。"),
        "sample_phrases": _lines(data.get("sample_phrases"), "请至少写一句本人会说的话。"),
    }
    blob = json.dumps(cleaned, ensure_ascii=False)
    if _SECRET.search(blob):
        raise ProfileError("个人资料里不要写密钥或私钥。")
    return cleaned


def load_profile() -> dict:
    path = profile_path()
    if not path.exists():
        raise ConfigError("没有找到 profile.json。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("profile.json 不是合法的 JSON，请在后台页面重新保存。") from exc
    return clean_profile(data)


def save_profile(data: dict) -> dict:
    cleaned = clean_profile(data)
    atomic_write(
        profile_path(),
        json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
    )
    return cleaned


def profile_from_form(form) -> dict:
    return clean_profile(
        {
            "name": form.get("name", ""),
            "role": form.get("role", ""),
            "introduction": form.get("introduction", ""),
            "tone": form.get("tone", ""),
            "cannot_promise": _split_lines(form.get("cannot_promise", "")),
            "sample_phrases": _split_lines(form.get("sample_phrases", "")),
        }
    )


def profile_is_placeholder(profile: dict) -> bool:
    if profile.get("name", "").strip() in {"", "示例用户"}:
        return True
    for key in ("role", "introduction", "tone"):
        text = profile.get(key, "")
        if any(mark in text for mark in _PLACEHOLDER_MARKS):
            return True
    return False


def profile_as_prompt(profile: dict) -> str:
    cannot = "\n".join(f"- {item}" for item in profile["cannot_promise"])
    phrases = "\n".join(f"- {item}" for item in profile["sample_phrases"])
    return (
        "姓名：" + profile["name"] + "\n"
        "身份：" + profile["role"] + "\n"
        "自我介绍：" + profile["introduction"] + "\n"
        "语气：" + profile["tone"] + "\n"
        "不能承诺的事：\n" + cannot + "\n"
        "本人会说的话：\n" + phrases
    )


def _text(value: object, message: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ProfileError(message)
    text = value.strip()
    if not text:
        raise ProfileError(message)
    if len(text) > limit:
        raise ProfileError("有一项写得太长了。")
    return text


def _lines(value: object, message: str) -> list[str]:
    if isinstance(value, str):
        items = _split_lines(value)
    elif isinstance(value, list):
        items = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ProfileError("请按一行一条写不能承诺的事，以及本人会说的话。")
            items.append(item.strip())
    else:
        raise ProfileError(message)
    if not items:
        raise ProfileError(message)
    if len(items) > 30:
        raise ProfileError("条目太多了，请留在 30 条以内。")
    for item in items:
        if len(item) > 200:
            raise ProfileError("有一条写得太长了。")
    return items


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]
