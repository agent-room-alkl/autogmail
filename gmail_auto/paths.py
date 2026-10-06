import os
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent


def root() -> Path:
    override = os.environ.get("AUTOMAIL_HOME")
    if override:
        return Path(override)
    return CODE_ROOT


def data_dir(create: bool = True) -> Path:
    path = root() / "data"
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            if not os.environ.get("VERCEL"):
                raise
    return path


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
