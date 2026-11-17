import asyncio
import unittest
from urllib.parse import parse_qs, urlparse

import aiohttp

from telegram_client import TelegramClient, TelegramError, build_notification, control_keyboard


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def client(self, body, status=200, headers=None):
        return TelegramClient(Session(Response(body, status, headers)), "123:SECRET", "123")

    async def test_send_confirmation_and_payload(self):
        client = self.client({"ok": True, "result": {"message_id": 5}})
        self.assertTrue(await client.send("<b>hello</b>", {"inline_keyboard": []}))
        payload = client.session.requests[0][1]["json"]
        self.assertEqual(payload["chat_id"], "123")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(payload["link_preview_options"]["is_disabled"])

    async def test_http_200_ok_false_is_failure(self):
        client = self.client({"ok": False, "error_code": 403, "description": "123:SECRET"})
        with self.assertRaises(TelegramError) as caught:
            await client.send("hello")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertNotIn("SECRET", str(caught.exception))

    async def test_rate_limit_body_takes_precedence(self):
        client = self.client({"ok": False, "error_code": 429, "parameters": {"retry_after": 17}},
                             429, {"Retry-After": "5"})
        with self.assertRaises(TelegramError) as caught:
            await client.send("hello")
        self.assertEqual(caught.exception.retry_after, 17)

    async def test_rate_limit_header_without_json(self):
        client = self.client(ValueError("bad json"), 429, {"Retry-After": "9"})
        with self.assertRaises(TelegramError) as caught:
            await client.send("hello")
        self.assertEqual(caught.exception.retry_after, 9)

    async def test_transport_error_does_not_leak_token(self):
        client = TelegramClient(Session(aiohttp.ClientConnectionError("https://api.telegram.org/bot123:SECRET/sendMessage")),
                                "123:SECRET", "123")
        with self.assertRaises(TelegramError) as caught:
            await client.send("hello")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_empty_token_does_not_make_request(self):
        client = TelegramClient(Session(None), "", "123")
        with self.assertRaises(TelegramError):
            await client.send("hello")
        self.assertEqual(client.session.requests, [])

    async def test_updates_and_verify(self):
        client = self.client({"ok": True, "result": [{"update_id": 7}]})
        self.assertEqual(await client.updates(5, timeout=10), [{"update_id": 7}])
        self.assertEqual(client.session.requests[0][1]["timeout"], 20)
        client = self.client({"ok": True, "result": {"is_bot": True, "username": "bot"}})
        self.assertEqual((await client.verify())["username"], "bot")

    async def test_false_delivery_confirmation(self):
        client = self.client({"ok": True, "result": None})
        with self.assertRaises(TelegramError):
            await client.send("hello")

    async def test_cancellation_propagates(self):
        client = TelegramClient(Session(asyncio.CancelledError()), "123:SECRET", "123")
        # CancelledError is BaseException; use a session that raises it directly.
        def cancel(*args, **kwargs):
            raise asyncio.CancelledError
        client.session.post = cancel
        with self.assertRaises(asyncio.CancelledError):
            await client.send("hello")


class NotificationTests(unittest.TestCase):
    def gift(self):
        return dict(slug="CandyCane", number=123, owner_username="alice_123",
                    owner_display="Alice", model="A < B & C", backdrop="Black", symbol="<Star>")

    def test_escaped_html_and_encoded_draft(self):
        text, markup = build_notification(self.gift(), '<bad>& "reason"',
                                          "Привет! {gift}; {model}, {backdrop}, {number}, {collection}")
        self.assertIn("A &lt; B &amp; C", text)
        self.assertIn("&lt;bad&gt;&amp; &quot;reason&quot;", text)
        dm = markup["inline_keyboard"][1][0]["url"]
        self.assertEqual(urlparse(dm).netloc, "t.me")
        self.assertEqual(parse_qs(urlparse(dm).query)["text"][0],
                         "Привет! CandyCane #123; A < B & C, Black, 123, CandyCane")

    def test_hidden_owner_has_no_contact_button(self):
        gift = self.gift()
        gift["owner_username"] = None
        text, markup = build_notification(gift, "reason", "hello")
        self.assertEqual(len(markup["inline_keyboard"]), 1)
        self.assertIn("нет доступного", text)

    def test_invalid_username_not_used_for_link(self):
        gift = self.gift()
        gift["owner_username"] = "evil.example/path"
        _, markup = build_notification(gift, "reason", "hello")
        self.assertEqual(len(markup["inline_keyboard"]), 1)

    def test_keyboard_only_controls_dm_hunter(self):
        text = str(control_keyboard())
        self.assertIn("Старт DM Hunter", text)
        self.assertIn("Стоп DM Hunter", text)
        self.assertNotIn("Market", text)


if __name__ == "__main__":
    unittest.main()
