import os
import tempfile
import unittest
from unittest.mock import patch

from gmail_auto.paths import CODE_ROOT
from gmail_auto.profile_store import load_profile, save_profile
from gmail_auto.store import is_live
from gmail_auto.web import HOST, create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("AUTOMAIL_HOME")
        self.prev_key = os.environ.get("OPENAI_API_KEY")
        os.environ["AUTOMAIL_HOME"] = self.tmp.name
        os.environ["OPENAI_API_KEY"] = "test-key-not-used"
        save_profile(
            {
                "name": "测试用户",
                "role": "测试身份",
                "introduction": "这是测试用的介绍，没有真实经历。",
                "tone": "简短直接。",
                "cannot_promise": ["不承诺日期"],
                "sample_phrases": ["我看到了。"],
            }
        )
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        if self.prev_home is None:
            os.environ.pop("AUTOMAIL_HOME", None)
        else:
            os.environ["AUTOMAIL_HOME"] = self.prev_home
        if self.prev_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.prev_key
        self.tmp.cleanup()

    def test_page_is_local_and_shows_mail_states(self):
        self.assertEqual(HOST, "127.0.0.1")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("最近的邮件", text)
        self.assertIn("已自动回复", text)
        self.assertIn("演练已保存", text)
        self.assertIn("未处理", text)
        self.assertIn("授权", text)
        remote = self.client.get("/", base_url="http://evil.example")
        self.assertEqual(remote.status_code, 403)
        self.assertIn("本机", remote.get_data(as_text=True))
        forged = self.client.post("/run", headers={"Origin": "http://evil.example"})
        self.assertEqual(forged.status_code, 403)
        os.environ["VERCEL"] = "1"
        try:
            deployed = self.client.get("/", base_url="https://autogmail.vercel.app", follow_redirects=True)
            self.assertEqual(deployed.status_code, 200)
            self.assertIn("邮箱", deployed.get_data(as_text=True))
        finally:
            os.environ.pop("VERCEL", None)

    def test_live_switch_rejects_placeholder_and_missing_confirmation(self):
        missing = self.client.post("/toggle", follow_redirects=True)
        self.assertIn("勾选", missing.get_data(as_text=True))
        self.assertFalse(is_live())
        save_profile(
            {
                "name": "示例用户",
                "role": "（请填写你的身份）",
                "introduction": "（请用几句事实介绍自己。）",
                "tone": "（请描述你平时写信的语气。）",
                "cannot_promise": ["不承诺日期"],
                "sample_phrases": ["我看到了。"],
            }
        )
        refused = self.client.post("/toggle", data={"confirm": "1"}, follow_redirects=True)
        self.assertIn("占位", refused.get_data(as_text=True))
        self.assertFalse(is_live())

    def test_run_button_calls_one_round(self):
        with patch("gmail_auto.web.run_once") as run_once:
            response = self.client.post("/run")
        self.assertEqual(response.status_code, 302)
        run_once.assert_called_once()

    def test_profile_edit_escapes_text_and_stays_in_the_temp_home(self):
        original = (CODE_ROOT / "profile.json").read_text(encoding="utf-8")
        saved = self.client.post(
            "/profile",
            data={
                "name": "<b>新的名字</b>",
                "role": "新的身份",
                "introduction": "新的介绍足够说明这是测试。",
                "tone": "平静。",
                "cannot_promise": "不承诺日期\n不提供电话",
                "sample_phrases": "我看到了。",
            },
            follow_redirects=True,
        )
        self.assertIn("已保存", saved.get_data(as_text=True))
        self.assertEqual(load_profile()["name"], "<b>新的名字</b>")
        self.assertEqual(load_profile()["cannot_promise"], ["不承诺日期", "不提供电话"])
        page = saved.get_data(as_text=True)
        self.assertIn("&lt;b&gt;新的名字&lt;/b&gt;", page)
        self.assertNotIn("<b>新的名字</b>", page)
        self.assertEqual((CODE_ROOT / "profile.json").read_text(encoding="utf-8"), original)
        blank = self.client.post(
            "/profile",
            data={
                "name": "",
                "role": "身份",
                "introduction": "介绍。",
                "tone": "短。",
                "cannot_promise": "不承诺日期",
                "sample_phrases": "我看到了。",
            },
        )
        self.assertEqual(blank.status_code, 400)
        self.assertIn("名字", blank.get_data(as_text=True))

    def test_oauth_without_client_id_explains_the_missing_setting(self):
        previous = os.environ.pop("GMAIL_CLIENT_ID", None)
        try:
            page = self.client.get("/oauth", follow_redirects=True)
        finally:
            if previous is not None:
                os.environ["GMAIL_CLIENT_ID"] = previous
        self.assertIn("GMAIL_CLIENT_ID", page.get_data(as_text=True))

    def test_login_asks_for_an_email_before_google(self):
        page = self.client.get("/login")
        text = page.get_data(as_text=True)
        self.assertIn("邮箱", text)
        self.assertIn("注册", text)
        self.assertIn("验证码", text)
        self.assertNotIn("密码", text)
        rejected = self.client.post("/login/code", data={"email": "不是邮箱", "mode": "register"})
        self.assertEqual(rejected.status_code, 400)

    def test_register_verifies_the_code_then_login_uses_it(self):
        sent = {}

        def capture(_to, code):
            sent["code"] = code

        with patch("gmail_auto.web.deliver_code", capture):
            started = self.client.post(
                "/login/code",
                data={"email": "lee@example.com", "mode": "register"},
                follow_redirects=True,
            )
        self.assertIn("验证码已发到", started.get_data(as_text=True))
        wrong = self.client.post(
            "/login",
            data={"email": "lee@example.com", "mode": "register", "code": "000000"},
        )
        self.assertEqual(wrong.status_code, 400)
        done = self.client.post(
            "/login",
            data={"email": "lee@example.com", "mode": "register", "code": sent["code"]},
            follow_redirects=True,
        )
        self.assertIn("已登录", done.get_data(as_text=True))
        self.assertIn("授权 Gmail", done.get_data(as_text=True))
        again = self.client.post("/login/code", data={"email": "lee@example.com", "mode": "register"})
        self.assertIn("已经注册", again.get_data(as_text=True))
        missing = self.client.post("/login/code", data={"email": "new@example.com", "mode": "login"})
        self.assertIn("还没注册", missing.get_data(as_text=True))

    def test_verification_code_does_not_need_vercel_kv(self):
        previous = os.environ.get("VERCEL")
        saved = {name: os.environ.pop(name, None) for name in (
            "KV_REST_API_URL",
            "KV_REST_API_TOKEN",
            "UPSTASH_REDIS_REST_URL",
            "UPSTASH_REDIS_REST_TOKEN",
        )}
        os.environ["VERCEL"] = "1"
        sent = {}

        def capture(_to, code):
            sent["code"] = code

        try:
            with patch("gmail_auto.web.deliver_code", capture):
                started = self.client.post(
                    "/login/code",
                    data={"email": "lee@example.com", "mode": "register"},
                    follow_redirects=True,
                )
            self.assertNotIn("Vercel KV", started.get_data(as_text=True))
            done = self.client.post(
                "/login",
                data={"email": "lee@example.com", "mode": "register", "code": sent["code"]},
                follow_redirects=True,
            )
        finally:
            if previous is None:
                os.environ.pop("VERCEL", None)
            else:
                os.environ["VERCEL"] = previous
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
        self.assertIn("已登录", done.get_data(as_text=True))

    def test_old_gmail_grant_offers_a_manual_button(self):
        import time

        from gmail_auto.accounts import mark_gmail_authorized

        mark_gmail_authorized("lee@example.com", at=time.time() - 8 * 24 * 60 * 60)
        page = self.client.get("/")
        text = page.get_data(as_text=True)
        self.assertIn("重新授权", text)
        self.assertIn("手工重新授权", text)

    def test_cron_on_vercel_requires_the_secret(self):
        previous = os.environ.get("VERCEL")
        secret = os.environ.get("CRON_SECRET")
        os.environ["VERCEL"] = "1"
        os.environ["CRON_SECRET"] = "cron-test-secret"
        try:
            blocked = self.client.get("/cron")
            allowed = self.client.get("/cron", headers={"Authorization": "Bearer cron-test-secret"})
        finally:
            if previous is None:
                os.environ.pop("VERCEL", None)
            else:
                os.environ["VERCEL"] = previous
            if secret is None:
                os.environ.pop("CRON_SECRET", None)
            else:
                os.environ["CRON_SECRET"] = secret
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertFalse(allowed.get_json()["ok"])


class OnceModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("AUTOMAIL_HOME")
        os.environ["AUTOMAIL_HOME"] = self.tmp.name

    def tearDown(self):
        if self.prev_home is None:
            os.environ.pop("AUTOMAIL_HOME", None)
        else:
            os.environ["AUTOMAIL_HOME"] = self.prev_home
        self.tmp.cleanup()

    def test_once_without_credentials_does_not_open_a_browser(self):
        import main

        before = (CODE_ROOT / "profile.json").read_text(encoding="utf-8")
        code = main.main(["--once"])
        self.assertEqual(code, 1)
        self.assertEqual((CODE_ROOT / "profile.json").read_text(encoding="utf-8"), before)
