"""TelegramSink with a fake bot: entities, topics, polls, edits, and Telethon errors."""
import asyncio
import unittest

from telethon import errors

from src.adapters.telegram import sink as tg
from src.adapters.telegram.formatting import to_entities
from src.core import rich as fmt
from src.core.model import Dest, Poll
from src.core.sink import CantEdit, RateLimited, Rejected, Unreachable
from src.adapters.whatsapp.formatting import parse_whatsapp
from tests.telegram_helpers import FakeBot

LONG = ("word " * 900).strip()  # two Telegram messages


def run(coro):
    return asyncio.run(coro)


def dest(topic=0):
    return Dest("telegram", "111", hash=1, topic=topic, handle="peer111")


def sink_with_bot():
    s = tg.TelegramSink("unused")
    s.client = FakeBot()
    return s


class EntityTests(unittest.TestCase):
    def test_offsets_are_utf16(self):
        r = fmt.concat(fmt.styled("Rahul 🚀", fmt.BOLD), fmt.plain(":\n\n"),
                       parse_whatsapp("*hi* there"))
        self.assertEqual(r.text, "Rahul 🚀:\n\nhi there")
        got = sorted((type(e).__name__, e.offset, e.length) for e in to_entities(r))
        # "Rahul 🚀" is 7 code points but 8 UTF-16 units; "hi" starts at unit 11.
        self.assertEqual(got, [("MessageEntityBold", 0, 8), ("MessageEntityBold", 11, 2)])

    def test_entity_types(self):
        r = parse_whatsapp("> *a* _b_ ~c~ `d`\n```e\nf```")
        names = sorted(type(e).__name__ for e in to_entities(r))
        self.assertEqual(names, sorted([
            "MessageEntityBlockquote", "MessageEntityBold", "MessageEntityItalic",
            "MessageEntityStrike", "MessageEntityCode", "MessageEntityPre",
        ]))

    def test_waveform(self):
        self.assertIsNone(tg.telegram_waveform(b""))
        self.assertTrue(tg.telegram_waveform(bytes([0, 100] * 32)))


class TopicTests(unittest.TestCase):
    def test_every_part_lands_in_the_topic(self):
        s = sink_with_bot()
        run(s.send_text(dest(topic=7), fmt.plain(LONG), None))
        self.assertEqual([c[3]["reply_to"] for c in s.client.calls], [7, 7])

    def test_a_reply_target_wins_for_the_first_part(self):
        s = sink_with_bot()
        run(s.send_text(dest(topic=7), fmt.plain(LONG), 55))
        self.assertEqual([c[3]["reply_to"] for c in s.client.calls], [55, 7])

    def test_no_topic(self):
        s = sink_with_bot()
        self.assertEqual(run(s.send_text(dest(), fmt.plain("x"), None)), 101)
        self.assertIsNone(s.client.calls[0][3]["reply_to"])

    def test_nothing_to_post(self):
        s = sink_with_bot()
        self.assertIsNone(run(s.send_text(dest(), fmt.plain("  "), None)))
        self.assertEqual(s.client.calls, [])


class PollTests(unittest.TestCase):
    def test_poll_that_does_not_fit_is_left_to_the_caller(self):
        s = sink_with_bot()
        poll = Poll("Pick", [f"opt{i}" for i in range(12)], multiple=False)
        self.assertIsNone(run(s.send_poll(dest(), poll, None)))
        self.assertEqual(s.client.calls, [])

    def test_poll_in_a_topic(self):
        s = sink_with_bot()
        run(s.send_poll(dest(topic=7), Poll("Which?", ["A", "B"], multiple=False), None))
        self.assertEqual(s.client.calls[0][3]["reply_to"], 7)


class EditTests(unittest.TestCase):
    def test_edit(self):
        s = sink_with_bot()
        self.assertTrue(run(s.edit(dest(), 5, fmt.styled("new", fmt.BOLD), is_media=False)))
        kind, peer, msg_id, text, kw = s.client.calls[0]
        self.assertEqual((kind, peer, msg_id, text), ("edit", "peer111", 5, "new"))

    def test_caption_is_cut_to_the_caption_limit(self):
        s = sink_with_bot()
        run(s.edit(dest(), 5, fmt.plain("x" * 2000), is_media=True))
        self.assertEqual(len(s.client.calls[0][3]), tg.CAPTION_LIMIT)

    def test_not_modified(self):
        s = sink_with_bot()
        s.client.fail = [errors.MessageNotModifiedError(request=None)]
        self.assertFalse(run(s.edit(dest(), 5, fmt.plain("x"), is_media=False)))

    def test_too_old(self):
        s = sink_with_bot()
        s.client.fail = [errors.MessageEditTimeExpiredError(request=None)]
        with self.assertRaises(CantEdit):
            run(s.edit(dest(), 5, fmt.plain("x"), is_media=False))


class ErrorTests(unittest.TestCase):
    def send_failing_with(self, error):
        s = sink_with_bot()
        s.client.fail = [error]
        return run(s.send_text(dest(), fmt.plain("x"), None))

    def test_flood_wait(self):
        with self.assertRaises(RateLimited) as cm:
            self.send_failing_with(errors.FloodWaitError(request=None, capture=30))
        self.assertEqual((cm.exception.seconds, cm.exception.platform), (30, "Telegram"))

    def test_forbidden_is_final(self):
        with self.assertRaises(Rejected) as cm:
            self.send_failing_with(errors.ChatWriteForbiddenError(request=None))
        self.assertFalse(cm.exception.retry_once)

    def test_bad_request_gets_one_more_try(self):
        with self.assertRaises(Rejected) as cm:
            self.send_failing_with(errors.MediaEmptyError(request=None))
        self.assertTrue(cm.exception.retry_once)

    def test_other_errors_pass_through(self):
        with self.assertRaises(ConnectionError):
            self.send_failing_with(ConnectionError("network blip"))

    def test_unreachable_chat(self):
        async def fake_resolve(bot, dest_id, dest_hash, role, for_bot):
            raise SystemExit("Bot could not resolve dest chat (id=111): ChatIdInvalidError\n"
                             "Make sure the bot is a member of that chat with permission to post.")

        original, tg.resolve_peer = tg.resolve_peer, fake_resolve
        try:
            with self.assertRaises(Unreachable) as cm:
                run(sink_with_bot().resolve(dest()))
        finally:
            tg.resolve_peer = original
        self.assertEqual(str(cm.exception), "Bot could not resolve dest chat (id=111): ChatIdInvalidError")
        self.assertEqual(cm.exception.hint, "Add the bot there")


if __name__ == "__main__":
    unittest.main()
