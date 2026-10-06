import os
import secrets

from flask import Flask, abort, flash, redirect, request, session, url_for

from gmail_auto.errors import ConfigError
from gmail_auto.filters import Skip, classify
from gmail_auto.paths import CODE_ROOT, atomic_write, data_dir
from gmail_auto.profile_store import (
    load_profile,
    profile_from_form,
    profile_is_placeholder,
    save_profile,
)
from gmail_auto.gmail_client import SCOPES, web_client_config
from gmail_auto.runner import format_report, run_once, toggle_and_run
from gmail_auto.store import is_live, list_processed, load_last_run, load_processed

HOST = "127.0.0.1"
PORT = 8765
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(CODE_ROOT / "templates"),
        static_folder=str(CODE_ROOT / "static"),
    )
    configure_app(app)
    return app


def configure_app(app: Flask) -> None:
    app.config["MAX_CONTENT_LENGTH"] = 200_000
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.secret_key = _secret()
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("VERCEL"))

    @app.before_request
    def local_only():
        if not _host_allowed(_request_host()):
            abort(403)
        if request.method == "POST":
            origin = request.headers.get("Origin", "")
            if origin and not _host_allowed(_origin_host(origin)):
                abort(403)

    @app.get("/")
    def home():
        context = _home_context()
        if not context["account"] and (context["hosted"] or web_client_config()):
            return redirect(url_for("login_page"))
        return _render("index.html", **context)

    @app.get("/login")
    def login_page():
        return _render("login.html", email=session.get("login_email", ""), error="")

    @app.post("/login")
    def login_submit():
        email = request.form.get("email", "").strip()
        if not _email_ok(email):
            return _render("login.html", email=email, error="请输入要授权的 Gmail 地址。"), 400
        session["login_email"] = email
        return redirect(url_for("oauth_start"))

    @app.post("/run")
    def run_now():
        _run(lambda: run_once())
        return redirect(url_for("home"))

    @app.post("/toggle")
    def toggle():
        confirmed = request.form.get("confirm") == "1"
        _run(lambda: toggle_and_run(confirmed))
        return redirect(url_for("home"))

    @app.get("/oauth")
    def oauth_start():
        config = web_client_config()
        if config is None:
            flash("请先在 Vercel 设置 GMAIL_CLIENT_ID 和 GMAIL_CLIENT_SECRET。")
            return redirect(url_for("home"))
        if os.environ.get("VERCEL") and not os.getenv("FLASK_SECRET_KEY", "").strip():
            flash("请先在 Vercel 设置 FLASK_SECRET_KEY，再连接 Gmail。")
            return redirect(url_for("home"))
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(config, SCOPES)
        flow.redirect_uri = _oauth_redirect()
        hint = session.get("login_email", "")
        options = {"access_type": "offline", "prompt": "consent"}
        if hint:
            options["login_hint"] = hint
        auth_url, state = flow.authorization_url(**options)
        session["oauth_state"] = state
        return redirect(auth_url)

    @app.get("/oauth/callback")
    def oauth_callback():
        config = web_client_config()
        expected = session.get("oauth_state")
        if config is None or not expected or request.args.get("state") != expected:
            flash("授权状态对不上，请再点一次「连接 Gmail」。")
            return redirect(url_for("home"))
        if request.args.get("error"):
            flash("Gmail 授权没有完成。")
            return redirect(url_for("home"))
        from google_auth_oauthlib.flow import Flow
        from gmail_auto.token_store import save_token_raw, storage_ready

        if not storage_ready():
            flash("授权页能打开，但还没有 Vercel KV，刷新令牌没法保存。")
            return redirect(url_for("home"))
        flow = Flow.from_client_config(config, SCOPES)
        flow.redirect_uri = _oauth_redirect()
        try:
            flow.fetch_token(authorization_response=_external_url())
        except Exception:
            flash("Gmail 授权没有完成。请再试一次。")
            return redirect(url_for("home"))
        creds = flow.credentials
        if not getattr(creds, "refresh_token", None):
            flash("没有拿到可长期使用的授权。请再点一次「连接 Gmail」。")
            return redirect(url_for("home"))
        save_token_raw(creds.to_json())
        session.pop("oauth_state", None)
        flash("Gmail 已连接。接下来每小时会自动检查。")
        return redirect(url_for("home"))

    @app.get("/cron")
    def cron():
        if not _cron_allowed():
            abort(403)
        try:
            report = run_once()
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        text = format_report(report)
        return {"ok": True, "summary": text.split("\n", 1)[0]}

    @app.get("/profile")
    def profile_page():
        return _render("profile.html", **_profile_context())

    @app.post("/profile")
    def profile_save():
        try:
            save_profile(profile_from_form(request.form))
        except ConfigError as exc:
            context = _profile_context(request.form)
            context["error"] = str(exc)
            return _render("profile.html", **context), 400
        flash("个人资料已保存。")
        return redirect(url_for("profile_page"))

    @app.errorhandler(403)
    def forbidden(_exc):
        return "这个页面只给本机使用。", 403

    @app.errorhandler(404)
    def missing(_exc):
        return "没有这个页面。", 404

    @app.errorhandler(405)
    def method_not_allowed(_exc):
        return "这个操作不能这样打开。", 405

    @app.errorhandler(413)
    def too_large(_exc):
        return "提交的内容太长了。", 413


def run_server() -> None:
    app = create_app()
    try:
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
    except OSError as exc:
        raise ConfigError("本机 8765 端口已被占用，后台页面没有打开。") from exc


def _run(action) -> None:
    try:
        action()
    except ConfigError as exc:
        last = load_last_run() or {}
        if last.get("error") != str(exc):
            flash(str(exc))


def _home_context() -> dict:
    errors = []
    placeholder = True
    try:
        profile = load_profile()
        placeholder = profile_is_placeholder(profile)
    except ConfigError as exc:
        errors.append(str(exc))

    account = ""
    rows = []
    try:
        from gmail_auto.gmail_client import GmailClient

        client = GmailClient(allow_browser=False)
        account = client.get_user_email()
        processed = load_processed()
        for mail in client.list_recent(25):
            rows.append(_row_from_mail(mail, processed.get(mail.id), account))
    except ConfigError as exc:
        errors.append(str(exc))
        rows = _rows_from_store()
    except Exception as exc:
        errors.append(_safe_error(exc))
        rows = _rows_from_store()

    last_run = load_last_run()
    if isinstance(last_run, dict):
        last_run.setdefault("lines", [])
        last_run.setdefault("summary", "")
        last_run.setdefault("error", "")
    return {
        "live": is_live(),
        "placeholder": placeholder,
        "account": account,
        "errors": errors,
        "rows": rows,
        "last_run": last_run,
        "hosted": bool(os.environ.get("VERCEL")),
        "connect_url": url_for("login_page") if web_client_config() or os.environ.get("VERCEL") else "",
    }


def _profile_context(form=None) -> dict:
    if form is not None:
        return {
            "profile": _profile_view(form=form),
            "placeholder": False,
            "error": "",
        }
    try:
        profile = load_profile()
    except ConfigError as exc:
        return {"profile": _profile_view(), "placeholder": True, "error": str(exc)}
    return {
        "profile": _profile_view(profile),
        "placeholder": profile_is_placeholder(profile),
        "error": "",
    }


def _profile_view(profile=None, form=None) -> dict:
    if form is not None:
        return {
            "name": form.get("name", ""),
            "role": form.get("role", ""),
            "introduction": form.get("introduction", ""),
            "tone": form.get("tone", ""),
            "cannot_promise_text": form.get("cannot_promise", ""),
            "sample_phrases_text": form.get("sample_phrases", ""),
        }
    profile = profile or {}
    return {
        "name": profile.get("name", ""),
        "role": profile.get("role", ""),
        "introduction": profile.get("introduction", ""),
        "tone": profile.get("tone", ""),
        "cannot_promise_text": "\n".join(profile.get("cannot_promise") or []),
        "sample_phrases_text": "\n".join(profile.get("sample_phrases") or []),
    }


def _row_from_mail(mail, record, user_email: str) -> dict:
    label, kind, reply, outbox = _describe(mail, record, user_email)
    body = (mail.body or "").strip() or "（没有正文）"
    return {
        "who": _who(mail),
        "subject": mail.subject or "（无主题）",
        "date": mail.date,
        "body": _clip(body),
        "status": label,
        "kind": kind,
        "reply": reply,
        "outbox": outbox,
    }


def _rows_from_store() -> list[dict]:
    try:
        records = list_processed(25)
    except ConfigError:
        return []
    rows = []
    for record in records:
        action = record.get("action")
        if action == "sent":
            label, kind = "已自动回复", "sent"
        elif action == "drafted":
            label, kind = "演练已保存", "drafted"
        else:
            label, kind = f"已跳过：{record.get('reason') or '已跳过'}", "skipped"
        who = record.get("from_name") or record.get("from_email") or "（未知发件人）"
        if record.get("from_name") and record.get("from_email"):
            who = f"{record['from_name']} <{record['from_email']}>"
        rows.append(
            {
                "who": who,
                "subject": record.get("subject") or "（无主题）",
                "date": record.get("processed_at") or record.get("date") or "",
                "body": "暂时连不上 Gmail。这里只显示本机处理记录，没有正文。",
                "status": label,
                "kind": kind,
                "reply": record.get("reply") or "",
                "outbox": record.get("outbox") or "",
            }
        )
    return rows


def _describe(mail, record, user_email: str) -> tuple[str, str, str, str]:
    if isinstance(record, dict):
        reply = record.get("reply") or ""
        outbox = record.get("outbox") or ""
        action = record.get("action")
        if action == "sent":
            return "已自动回复", "sent", reply, outbox
        if action == "drafted":
            return "演练已保存", "drafted", reply, outbox
        reason = record.get("reason") or "已跳过"
        return f"已跳过：{reason}", "skipped", reply, ""
    if user_email:
        decision = classify(mail, user_email)
        if isinstance(decision, Skip):
            return f"不会自动回复：{decision.reason}", "skipped", "", ""
    if "UNREAD" in (mail.label_ids or []):
        return "未处理", "pending", "", ""
    return "已读，未曾自动回复", "read", "", ""


def _who(mail) -> str:
    if mail.from_name and mail.from_email:
        return f"{mail.from_name} <{mail.from_email}>"
    return mail.from_name or mail.from_email or "（未知发件人）"


def _clip(text: str, limit: int = 20000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n（后面的内容省略了）"


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip().replace("\n", " ")
    lowered = text.lower()
    if not text or len(text) > 300 or "sk-" in text or "api_key" in lowered or "token" in lowered:
        return "读取收件箱时出错。"
    return text


def _render(template: str, **context):
    from flask import render_template

    return render_template(template, **context)


def _email_ok(value: str) -> bool:
    if len(value) < 6 or len(value) > 254 or "@" not in value:
        return False
    name, _, domain = value.partition("@")
    return bool(name) and "." in domain and " " not in value


def _oauth_redirect() -> str:
    return _public_origin() + "/oauth/callback"


def _external_url() -> str:
    path = request.full_path if request.query_string else request.path
    return _public_origin() + path


def _public_origin() -> str:
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    return f"{proto}://{host}"


def _cron_allowed() -> bool:
    if not os.environ.get("VERCEL"):
        return _request_host() in _ALLOWED_HOSTS
    secret = os.getenv("CRON_SECRET", "").strip()
    header = request.headers.get("Authorization", "")
    return bool(secret) and header == f"Bearer {secret}"


def _request_host() -> str:
    forwarded = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    raw = forwarded or (request.host or "")
    return raw.split(":")[0].strip().lower()


def _origin_host(origin: str) -> str:
    return origin.split("://", 1)[-1].split("/")[0].split(":")[0].strip().lower()


def _host_allowed(host: str) -> bool:
    if os.environ.get("VERCEL"):
        return True
    return host in _ALLOWED_HOSTS


def _secret() -> str:
    configured = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if configured:
        return configured
    if os.environ.get("VERCEL"):
        return "autogmail-vercel"
    try:
        path = data_dir() / "web_secret.txt"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_hex(32)
        atomic_write(path, value + "\n")
        return value
    except OSError:
        return secrets.token_hex(32)
