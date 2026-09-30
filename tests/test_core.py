import base64
import json
import os
import tempfile
import unittest
from email import message_from_bytes
from email.header import Header
from email.policy import default

from gmail_auto.errors import ConfigError, ProfileError, ReplyError
from gmail_auto.filters import Allow, Skip, classify, detect_language, is_system_address, same_mailbox
from gmail_auto.generator import clean_reply, ensure_safe
from gmail_auto.gmail_client import build_send_body, find_client_file, parse_message
from gmail_auto.models import Mail
from gmail_auto.paths import CODE_ROOT, root
from gmail_auto.profile_store import clean_profile, load_profile, profile_is_placeholder, save_profile
from gmail_auto.runner import run_once, toggle_and_run
from gmail_auto.store import get_processed, is_live, set_live


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("AUTOMAIL_HOME")
        self.prev_key = os.environ.get("OPENAI_API_KEY")
        os.environ["AUTOMAIL_HOME"] = self.tmp.name
        os.environ["OPENAI_API_KEY"] = "test-key-not-used"

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


def sample(**kwargs) -> Mail:
    data = dict(
        id="m1",
        thread_id="t1",
        from_name="Lee",
        from_email="lee@example.com",
        reply_email="",
        subject="Hello",
        date="2026-09-30 10:00",
        body="Can you look at the notes?",
        headers={"message-id": "<m1@example.com>", "references": "<old@example.com>"},
        label_ids=["INBOX", "UNREAD"],
        snippet="Can you",
    )
    data.update(kwargs)
    return Mail(**data)


def write_test_profile(**overrides) -> dict:
    data = {
        "name": "测试用户",
        "role": "测试身份",
        "introduction": "这是测试用的介绍，没有真实经历。",
        "tone": "简短直接。",
        "cannot_promise": ["不承诺日期"],
        "sample_phrases": ["我看到了。"],
    }
    data.update(overrides)
    return save_profile(data)


class FakeGmail:
    def __init__(self, mails, user="me@example.com"):
        self.mails = mails
        self.user = user
        self.sent = []
        self.read = []

    def get_user_email(self):
        return self.user

    def list_unread(self, limit=20):
        return self.mails[:limit]

    def send_reply(self, mail, body, user_email):
        self.sent.append((mail.id, body, user_email))
        return "sent"

    def mark_read(self, message_id):
        self.read.append(message_id)


def english_reply(*_args):
    return "I saw your note."


def chinese_reply(*_args):
    return "我看到你的来信了。"


class FilterTests(unittest.TestCase):
    def test_language_follows_the_letter(self):
        self.assertEqual(detect_language("Please review the attached notes."), "en")
        self.assertEqual(detect_language("请帮我看一下明天的安排"), "zh")
        self.assertEqual(detect_language("Hello, the meeting is at 3. 请回复"), "en")
        self.assertEqual(classify(sample(), "me@example.com").language, "en")
        chinese = sample(subject="安排", body="请帮我看一下明天的安排")
        self.assertEqual(classify(chinese, "me@example.com").language, "zh")

    def test_skips_self_system_list_auto_and_categories(self):
        self.assertTrue(same_mailbox("me+news@gmail.com", "me@googlemail.com"))
        self.assertFalse(same_mailbox("me@gmail.com", "other@gmail.com"))
        self.assertIsInstance(classify(sample(from_email="me@example.com"), "me@example.com"), Skip)
        self.assertTrue(is_system_address("no-reply@news.example.com"))
        self.assertFalse(is_system_address("mina@example.com"))
        self.assertIn("系统", classify(sample(from_email="noreply@news.example.com"), "me@example.com").reason)
        listed = sample(headers={"list-id": "<news.example.com>"})
        self.assertIn("邮件列表", classify(listed, "me@example.com").reason)
        auto = sample(headers={"auto-submitted": "auto-replied"})
        self.assertIn("自动回复", classify(auto, "me@example.com").reason)
        away = sample(subject="Automatic reply: away")
        self.assertIn("自动回复", classify(away, "me@example.com").reason)
        promo = sample(label_ids=["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"])
        self.assertIn("促销", classify(promo, "me@example.com").reason)
        self.assertIn("没有可回复的正文", classify(sample(body="  "), "me@example.com").reason)

    def test_sensitive_mail_is_skipped_without_treating_ordinary_words_as_secrets(self):
        ordinary = classify(sample(body="I passed the exam yesterday. Can we talk tomorrow?"), "me@example.com")
        self.assertIsInstance(ordinary, Allow)
        secret = classify(sample(body="Please reset my password."), "me@example.com")
        self.assertIsInstance(secret, Skip)
        self.assertTrue(secret.sensitive)
        code = classify(sample(subject="验证码", body="123456"), "me@example.com")
        self.assertTrue(code.sensitive)

    def test_weekly_note_from_a_person_is_allowed(self):
        mail = sample(subject="Weekly notes", from_email="friend@example.com", body="Can you read this draft?")
        self.assertIsInstance(classify(mail, "me@example.com"), Allow)


class SafetyTests(unittest.TestCase):
    def profile(self):
        return write_test_profile()

    def test_blocks_invented_number_link_email_and_wrong_language(self):
        profile = {
            "name": "测试用户",
            "role": "测试身份",
            "introduction": "这是测试用的介绍，没有真实经历。",
            "tone": "简短直接。",
            "cannot_promise": ["不承诺日期"],
            "sample_phrases": ["我看到了。"],
        }
        ensure_safe("我周五再回复你。", profile, "zh")
        ensure_safe("I will reply on Friday.", profile, "en")
        ensure_safe("编号 20260930 我看到了。", profile, "zh")
        with self.assertRaises(ReplyError):
            ensure_safe("请打我电话 13800138000。", profile, "zh")
        with self.assertRaises(ReplyError):
            ensure_safe("请联系 138-0013-8000，我看到了。", profile, "zh")
        with self.assertRaises(ReplyError):
            ensure_safe("详见 https://example.com/secret 。我看到了。", profile, "zh")
        with self.assertRaises(ReplyError):
            ensure_safe("Write me at other@example.com today.", profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("I saw your note.", profile, "zh")
        with self.assertRaises(ReplyError):
            ensure_safe("As an AI I saw your note.", profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("密钥是 sk-abcdefghijklmnopqrstuvwxyz", profile, "zh")

    def test_allows_number_and_link_that_are_already_in_the_material(self):
        profile = {
            "name": "测试用户",
            "role": "测试身份",
            "introduction": "对外电话是 13800138000。",
            "tone": "简短。",
            "cannot_promise": ["不承诺日期"],
            "sample_phrases": ["我看到了。"],
        }
        ensure_safe("我的电话是 13800138000。", profile, "zh")
        mail = sample(body="请看 https://example.com/a")
        ensure_safe("I saw https://example.com/a in your note.", profile, "en", mail)

    def test_clean_reply_strips_wrapper(self):
        self.assertEqual(clean_reply("```\n我看到了。\n```"), "我看到了。")
        self.assertEqual(clean_reply("主题：你好\n我看到了。"), "我看到了。")


class GmailFormatTests(unittest.TestCase):
    def test_reply_stays_on_the_thread_and_uses_reply_to(self):
        mail = sample(subject="你好", reply_email="other@example.com")
        payload = build_send_body(mail, "我看到了。", "me@example.com")
        parsed = message_from_bytes(base64.urlsafe_b64decode(payload["raw"]), policy=default)
        self.assertEqual(payload["threadId"], "t1")
        self.assertEqual(parsed["Subject"], "Re: 你好")
        self.assertEqual(parsed["In-Reply-To"], "<m1@example.com>")
        self.assertIn("<old@example.com>", parsed["References"])
        self.assertIn("<m1@example.com>", parsed["References"])
        self.assertEqual(parsed["To"], "other@example.com")
        self.assertEqual(parsed["From"], "me@example.com")
        self.assertEqual(parsed.get_content().strip(), "我看到了。")

        again = sample(subject="Re: Hello")
        parsed_again = message_from_bytes(
            base64.urlsafe_b64decode(build_send_body(again, "I saw your note.", "me@example.com")["raw"]),
            policy=default,
        )
        self.assertEqual(parsed_again["Subject"], "Re: Hello")
        self.assertIn("Lee", parsed_again["To"])
        self.assertIn("lee@example.com", parsed_again["To"])

    def test_parse_prefers_plain_text_and_strips_html(self):
        raw = {
            "id": "h1",
            "threadId": "t",
            "labelIds": ["INBOX", "UNREAD"],
            "internalDate": "1710000000000",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "Lee <lee@example.com>"},
                    {"name": "Subject", "value": Header("你好", "utf-8").encode()},
                    {"name": "Message-ID", "value": "<m1@example.com>"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "headers": [{"name": "Content-Disposition", "value": "attachment; filename=a.txt"}],
                        "body": {"data": _b64("hidden attachment")},
                    },
                    {"mimeType": "text/plain", "body": {"data": _b64("visible note")}},
                    {"mimeType": "text/html", "body": {"data": _b64("<script>alert(1)</script><p>你好 &amp; 欢迎</p>")}},
                ],
            },
        }
        mail = parse_message(raw)
        self.assertEqual(mail.subject, "你好")
        self.assertEqual(mail.body, "visible note")
        self.assertNotIn("alert", mail.body)
        html_only = parse_message(
            {
                "id": "h2",
                "threadId": "t",
                "payload": {
                    "mimeType": "text/html",
                    "headers": [{"name": "From", "value": "Lee <lee@example.com>"}, {"name": "Subject", "value": "Hi"}],
                    "body": {"data": _b64("<script>alert(1)</script><p>你好 &amp; 欢迎</p>")},
                },
            }
        )
        self.assertIn("你好", html_only.body)
        self.assertIn("&", html_only.body)
        self.assertNotIn("alert", html_only.body)
        self.assertNotIn("<p>", html_only.body)


class RunnerTests(TempHome):
    def test_dry_run_writes_locally_and_is_not_sent_later(self):
        write_test_profile()
        fake = FakeGmail([sample()])
        report = run_once(gmail=fake, generate=english_reply)
        self.assertEqual(report.drafted, 1)
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.read, [])
        record = get_processed("m1")
        self.assertEqual(record["action"], "drafted")
        text = (root() / record["outbox"]).read_text(encoding="utf-8")
        self.assertIn("I saw your note.", text)
        self.assertIn("没有发送", text)
        set_live(True)
        again = run_once(gmail=fake, generate=english_reply)
        self.assertEqual(fake.sent, [])
        self.assertEqual(again.sent, 0)
        self.assertIn("已经处理过", again.lines[0])

    def test_live_send_replies_once_and_marks_read(self):
        write_test_profile()
        set_live(True)
        fake = FakeGmail([sample(id="m2")])
        seen = {}

        def generate(mail, _profile, language):
            seen["language"] = language
            return "I saw your note."

        report = run_once(gmail=fake, generate=generate)
        self.assertEqual(report.sent, 1)
        self.assertEqual(seen["language"], "en")
        self.assertEqual(fake.read, ["m2"])
        self.assertEqual(fake.sent[0][0], "m2")
        run_once(gmail=fake, generate=generate)
        self.assertEqual(len(fake.sent), 1)

    def test_chinese_letter_asks_for_chinese(self):
        write_test_profile()
        seen = {}

        def generate(_mail, _profile, language):
            seen["language"] = language
            return "我看到你的来信了。"

        mail = sample(subject="安排", body="请帮我看一下明天的安排")
        run_once(gmail=FakeGmail([mail]), generate=generate)
        self.assertEqual(seen["language"], "zh")

    def test_sensitive_mail_never_reaches_the_model(self):
        write_test_profile()
        set_live(True)
        called = []

        def generate(*args):
            called.append(args)
            return "I saw your note."

        mail = sample(body="Here is my password: hunter2")
        fake = FakeGmail([mail])
        report = run_once(gmail=fake, generate=generate)
        self.assertEqual(called, [])
        self.assertEqual(fake.sent, [])
        self.assertEqual(report.skipped, 1)
        record = get_processed(mail.id)
        self.assertEqual(record["subject"], "")
        self.assertTrue(record["sensitive"])
        self.assertNotIn("hunter2", json.dumps(record, ensure_ascii=False))

    def test_failed_generation_retries_once_then_stops(self):
        write_test_profile()

        def generate(*_args):
            raise ReplyError("连接不上 OpenAI。")

        fake = FakeGmail([sample()])
        first = run_once(gmail=fake, generate=generate)
        self.assertEqual(first.failed, 1)
        self.assertIsNone(get_processed("m1"))
        second = run_once(gmail=fake, generate=generate)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(get_processed("m1")["action"], "skipped")
        self.assertEqual(fake.sent, [])

    def test_send_failure_does_not_mark_read(self):
        write_test_profile()
        set_live(True)

        class Boom(FakeGmail):
            def send_reply(self, mail, body, user_email):
                raise ConfigError("访问 Gmail 失败。")

        fake = Boom([sample()])
        report = run_once(gmail=fake, generate=english_reply)
        self.assertEqual(report.failed, 1)
        self.assertEqual(fake.read, [])
        self.assertIsNone(get_processed("m1"))

    def test_placeholder_cannot_turn_on_live_sending(self):
        shipped = json.loads((CODE_ROOT / "profile.json").read_text(encoding="utf-8"))
        save_profile(shipped)
        self.assertTrue(profile_is_placeholder(load_profile()))
        set_live(True)
        fake = FakeGmail([sample()])
        with self.assertRaises(ConfigError) as caught:
            run_once(gmail=fake, generate=english_reply)
        self.assertIn("占位", str(caught.exception))
        self.assertFalse(is_live())
        self.assertEqual(fake.sent, [])
        with self.assertRaises(ConfigError):
            toggle_and_run(True)
        self.assertFalse(is_live())

    def test_toggle_requires_confirmation_and_then_sends(self):
        write_test_profile()
        with self.assertRaises(ConfigError) as caught:
            toggle_and_run(False)
        self.assertIn("勾选", str(caught.exception))
        self.assertFalse(is_live())
        fake = FakeGmail([sample(id="m9")])
        report = toggle_and_run(True, gmail=fake, generate=english_reply)
        self.assertTrue(report.live)
        self.assertEqual(fake.sent[0][0], "m9")
        self.assertEqual(fake.read, ["m9"])
        fake_off = FakeGmail([sample(id="m10")])
        dry = toggle_and_run(False, gmail=fake_off, generate=english_reply)
        self.assertFalse(is_live())
        self.assertEqual(fake_off.sent, [])
        self.assertEqual(dry.drafted, 1)

    def test_client_secret_must_be_a_desktop_app(self):
        with self.assertRaises(ConfigError) as missing:
            find_client_file()
        self.assertIn("credentials.json", str(missing.exception))
        path = root() / "credentials.json"
        path.write_text(json.dumps({"web": {"client_id": "placeholder"}}), encoding="utf-8")
        with self.assertRaises(ConfigError) as wrong:
            find_client_file()
        self.assertIn("桌面应用", str(wrong.exception))
        path.write_text(json.dumps({"installed": {"client_id": "placeholder"}}), encoding="utf-8")
        self.assertEqual(find_client_file(), path)

    def test_shipped_profile_stays_a_placeholder(self):
        raw = json.loads((CODE_ROOT / "profile.json").read_text(encoding="utf-8"))
        cleaned = clean_profile(raw)
        self.assertTrue(profile_is_placeholder(cleaned))
        self.assertNotIn("@", json.dumps(cleaned, ensure_ascii=False))
        self.assertNotIn("sk-", json.dumps(cleaned))

    def test_profile_rejects_secrets_and_blank_identity(self):
        with self.assertRaises(ProfileError):
            clean_profile(
                {
                    "name": "测试用户",
                    "role": "测试身份",
                    "introduction": "密钥 sk-abcdefghijklmnopqrstuvwxyz",
                    "tone": "短。",
                    "cannot_promise": ["不承诺日期"],
                    "sample_phrases": ["我看到了。"],
                }
            )
        with self.assertRaises(ProfileError):
            clean_profile(
                {
                    "name": "  ",
                    "role": "测试身份",
                    "introduction": "介绍。",
                    "tone": "短。",
                    "cannot_promise": ["不承诺日期"],
                    "sample_phrases": ["我看到了。"],
                }
            )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
