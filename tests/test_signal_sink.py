"""SignalSink against a fake signal-cli-rest-api (tests/signal_helpers.py)."""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from src.adapters.signal import sink as signal_sink
from src.core import rich as fmt
from src.core.model import Dest, Media, Poll
from src.core.sink import CantEdit, Rejected, Unreachable
from tests.signal_helpers import FIRST_TS, SENDER, make_sink

GROUP = Dest("signal", "group.AAAA")
PERSON = Dest("signal", "+15550000001")


def run(coro):
    return asyncio.run(coro)


def media_file(name="photo.jpg", data=b"media-bytes") -> Path:
    path = Path(tempfile.mkdtemp()) / name
    path.write_bytes(data)
    return path


def attachment_of(sent):
    """(data URI prefix, decoded bytes) of a payload's only attachment."""
    (uri,) = sent["base64_attachments"]
    prefix, data = uri.split(",", 1)
    return prefix, base64.b64decode(data)


class ResolveTests(unittest.TestCase):
    def test_account_and_group_are_checked(self):
        sink, api = make_sink()
        self.assertEqual(run(sink.resolve(GROUP)), "group.AAAA")
        self.assertEqual(api.requests, [("GET", "/v1/accounts"), ("GET", "/v1/groups/+15550000000")])

    def test_older_api_lists_bare_numbers(self):
        sink, api = make_sink()
        api.accounts = [SENDER]
        self.assertEqual(run(sink.resolve(PERSON)), "+15550000001")

    def test_account_not_set_up(self):
        sink, api = make_sink()
        api.accounts = []
        with self.assertRaises(Unreachable) as cm:
            run(sink.resolve(PERSON))
        self.assertIn("isn't registered or linked", str(cm.exception))

    def test_not_a_member_of_the_group(self):
        sink, _ = make_sink()
        with self.assertRaises(Unreachable):
            run(sink.resolve(Dest("signal", "group.BBBB")))

    def test_api_down_is_tried_twice(self):
        sink, api = make_sink()
        api.down = True
        with self.assertRaises(Unreachable) as cm:
            run(sink.resolve(PERSON))
        self.assertEqual(len(api.requests), 2)
        self.assertIn("isn't answering", str(cm.exception))

    def test_wrong_url(self):
        sink, _ = make_sink()
        sink.base_url += "/nope"
        with self.assertRaises(Unreachable) as cm:
            run(sink.resolve(PERSON))
        self.assertIn("404", str(cm.exception))

    def test_start_needs_a_sender(self):
        with self.assertRaises(SystemExit):
            run(signal_sink.SignalSink("http://signal-api:8080", "").start())


class SendTests(unittest.TestCase):
    def test_text(self):
        sink, api = make_sink()
        self.assertEqual(run(sink.send_text(PERSON, fmt.styled("hi", fmt.BOLD), None)), FIRST_TS)
        self.assertEqual(api.sent, [{"message": "hi", "number": SENDER,
                                     "recipients": ["+15550000001"], "text_mode": "normal"}])

    def test_reply_quotes_by_timestamp(self):
        sink, api = make_sink()
        run(sink.send_text(GROUP, fmt.plain("yes"), 1699999999999))
        self.assertEqual((api.sent[0]["quote_timestamp"], api.sent[0]["quote_author"]),
                         (1699999999999, SENDER))

    def test_long_text_is_split_and_only_the_first_part_quotes(self):
        sink, api = make_sink()
        self.assertEqual(run(sink.send_text(GROUP, fmt.plain(("word " * 900).strip()), 5)), FIRST_TS)
        self.assertEqual(len(api.sent), 3)
        self.assertTrue(all(len(s["message"]) <= signal_sink.TEXT_LIMIT for s in api.sent))
        self.assertEqual(["quote_timestamp" in s for s in api.sent], [True, False, False])

    def test_quote_blocks_become_lines(self):
        r = fmt.concat(fmt.styled("Rahul", fmt.BOLD), fmt.plain(":\n\n"),
                       fmt.Rich("a\nb", [fmt.Span(fmt.QUOTE, 0, 3)]), fmt.plain("\nreply"))
        out = signal_sink.quotes_as_lines(r)
        self.assertEqual(out.text, "Rahul:\n\n> a\n> b\nreply")
        self.assertEqual(signal_sink.quotes_as_lines(out).text, out.text)  # nothing quoted twice

    def test_document(self):
        sink, api = make_sink()
        path = media_file("Q3; final, v2.pdf", b"%PDF-1.4 data")
        media = Media(kind="document", mimetype="application/pdf; charset=binary")
        self.assertEqual(run(sink.send_media(GROUP, media, fmt.plain("report"), path, None)), FIRST_TS)
        (sent,) = api.sent
        self.assertEqual(sent["message"], "report")
        self.assertEqual(attachment_of(sent),
                         ("data:application/pdf;filename=Q3_ final_ v2.pdf;base64", b"%PDF-1.4 data"))

    def test_large_attachment_is_streamed_intact(self):
        sink, api = make_sink()
        data = bytes(range(256)) * 2000  # 512 kB: several base64 blocks
        run(sink.send_media(PERSON, Media(kind="video", mimetype="video/mp4"), fmt.Rich(),
                            media_file("v.mp4", data), None))
        self.assertEqual(attachment_of(api.sent[0]), ("data:video/mp4;filename=v.mp4;base64", data))
        self.assertEqual(api.sent[0]["message"], "")

    def test_mime_type_guessed_from_the_name(self):
        sink, api = make_sink()
        run(sink.send_media(PERSON, Media(kind="voice"), fmt.Rich(), media_file("voice.ogg"), None))
        self.assertEqual(attachment_of(api.sent[0])[0], "data:audio/ogg;filename=voice.ogg;base64")

    def test_too_big_for_signal_sends_the_caption_with_a_note(self):
        sink, api = make_sink()
        sink.max_attachment = 4
        run(sink.send_media(PERSON, Media(kind="video"), fmt.plain("clip"),
                            media_file("v.mp4", b"12345"), None))
        (sent,) = api.sent
        self.assertNotIn("base64_attachments", sent)
        self.assertEqual(sent["message"], "clip\n\n" + signal_sink.TOO_BIG_NOTE)

    def test_no_polls(self):
        sink, api = make_sink()
        self.assertIsNone(run(sink.send_poll(GROUP, Poll("Q", ["a", "b"], False), None)))
        self.assertEqual(api.requests, [])


class EditTests(unittest.TestCase):
    def test_text_edit(self):
        sink, api = make_sink()
        self.assertTrue(run(sink.edit(GROUP, 1699999999999, fmt.plain("fixed"), is_media=False)))
        self.assertEqual((api.sent[0]["edit_timestamp"], api.sent[0]["message"]),
                         (1699999999999, "fixed"))

    def test_caption_edits_are_not_sent(self):
        sink, api = make_sink()
        with self.assertRaises(CantEdit):
            run(sink.edit(GROUP, 1699999999999, fmt.plain("x"), is_media=True))
        self.assertEqual(api.requests, [])

    def test_refused_edit(self):
        sink, api = make_sink()
        api.fail = [(400, {"error": "Failed to send message"})]
        with self.assertRaises(CantEdit):
            run(sink.edit(GROUP, 1699999999999, fmt.plain("x"), is_media=False))


class ErrorTests(unittest.TestCase):
    def send(self, *answers):
        sink, api = make_sink()
        api.fail = list(answers)
        return run(sink.send_text(PERSON, fmt.plain("hi"), None)), api

    def test_refused_message_gets_one_more_try(self):
        with self.assertRaises(Rejected) as cm:
            self.send((400, {"error": "Failed to send message"}))
        self.assertTrue(cm.exception.retry_once)
        self.assertEqual(cm.exception.platform, "Signal")

    def test_setup_problems_are_unreachable(self):
        for error in ("Specified account does not exist", "Invalid identifier group.abc=",
                      "Failed to send message: Unregistered user +15550000001"):
            with self.assertRaises(Unreachable, msg=error):
                self.send((400, {"error": error}))

    def test_rate_limit_challenge_is_unreachable(self):
        with self.assertRaises(Unreachable):
            self.send((400, {"error": "Failed to send message", "challenge_tokens": ["t"]}))

    def test_server_error_is_tried_once_more(self):
        ts, _ = self.send((500, {"error": "boom"}))
        self.assertEqual(ts, FIRST_TS)
        with self.assertRaises(Unreachable):
            self.send((500, {"error": "boom"}), (500, {"error": "boom"}))

    def test_no_timestamp_still_counts_as_sent(self):
        ts, _ = self.send((201, b"{}"))
        self.assertEqual(ts, 0)


if __name__ == "__main__":
    unittest.main()
