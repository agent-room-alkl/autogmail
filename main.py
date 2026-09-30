"""个人 Gmail 自动回复。

运行：python main.py
只跑一轮：python main.py --once
"""

import argparse

from gmail_auto.console import configure_stdio, say
from gmail_auto.envfile import load_env
from gmail_auto.errors import ConfigError
from gmail_auto.runner import start_scheduler
from gmail_auto.store import is_live
from gmail_auto.web import PORT, run_server


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    try:
        return _main(argv)
    except ConfigError as exc:
        say(str(exc))
        return 1
    except KeyboardInterrupt:
        say("已停止。")
        return 0


def _main(argv: list[str] | None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description="个人 Gmail 自动回复（默认只演练，不发信）")
    parser.add_argument("--once", action="store_true", help="只检查一轮，不打开后台页面")
    args = parser.parse_args(argv)
    if args.once:
        _connect(optional=False)
        from gmail_auto.runner import run_once

        run_once()
        return 0
    _connect(optional=True)
    start_scheduler()
    mode = "真实发送已开启" if is_live() else "只演练，回复写到本机，不发信"
    say("个人 Gmail 自动回复已启动。")
    say(f"后台页面：http://127.0.0.1:{PORT}")
    say(f"当前模式：{mode}")
    say("大约每小时检查一次。按 Ctrl+C 停止。")
    run_server()
    return 0


def _connect(optional: bool) -> None:
    from gmail_auto.gmail_client import GmailClient

    try:
        client = GmailClient(allow_browser=True)
        say(f"已连接 {client.get_user_email()}")
    except ConfigError as exc:
        if not optional:
            raise
        say(str(exc))
        say("后台页面仍会打开。邮件要等 Gmail 授权成功后再跑。")
    except Exception as exc:
        text = str(exc).strip().replace("\n", " ")
        if len(text) > 300:
            text = text[:300] + "…"
        message = f"Gmail 授权没有完成：{text}" if text else "Gmail 授权没有完成。"
        if not optional:
            raise ConfigError(message) from exc
        say(message)
        say("后台页面仍会打开。邮件要等 Gmail 授权成功后再跑。")


def _utf8_stdio() -> None:
    configure_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
