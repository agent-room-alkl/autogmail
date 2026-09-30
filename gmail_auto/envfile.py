import os

from gmail_auto.errors import ConfigError
from gmail_auto.paths import CODE_ROOT, root


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ConfigError("还没安装依赖。请先运行 pip install -r requirements.txt。") from exc
    load_dotenv(CODE_ROOT / ".env")
    if root() != CODE_ROOT:
        load_dotenv(root() / ".env", override=True)


def require_openai_key() -> str:
    load_env()
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise ConfigError(
            "没有找到 OPENAI_API_KEY。请把 .env.example 复制成 .env，并填入你自己的密钥。"
        )
    return key


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"


def openai_base_url() -> str:
    return os.getenv("OPENAI_BASE_URL", "").strip()
