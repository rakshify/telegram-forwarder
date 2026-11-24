"""Telegram message -> neutral Message (src/adapters/telegram/convert.py)."""
import unittest

from telethon.tl import types as T

from src.adapters.telegram import convert
from src.adapters.telegram.sink import telegram_waveform
from src.core import rich as fmt
from src.core.model import Message, Skip
from tests import telegram_fixtures as fx


def media_of(media, text=""):
    m = convert.to_message(fx.message(1, text, media=media), "Alex")
    assert isinstance(m, Message), m
    return m


class TextTests(unittest.TestCase):
    def test_text_with_formatting(self):
        m = convert.to_message(fx.message(5, "hi there", entities=[T.MessageEntityBold(0, 2)]), "Alex")
        self.assertEqual((m.id, m.kind, m.sender_name, m.body.text), ("5", "text", "Alex", "hi there"))
        self.assertEqual(m.body.spans, [fmt.Span(fmt.BOLD, 0, 2)])

    def test_reply(self):
        m = convert.to_message(fx.message(2, "yes", reply_to=1), "Alex")
        self.assertEqual(m.reply.msg_id, "1")

    def test_reply_to_another_chat_is_not_a_reply_here(self):
        msg = fx.message(2, "yes")
        msg.reply_to = T.MessageReplyHeader(reply_to_msg_id=1, reply_to_peer_id=T.PeerChannel(555))
        self.assertIsNone(convert.to_message(msg, "Alex").reply)

    def test_topics(self):
        self.assertEqual(convert.topic_of(fx.message(2, "x", topic=17)), 17)         # in the topic
        self.assertEqual(convert.topic_of(fx.message(3, "x", reply_to=2, topic=17)), 17)  # a reply in it
        self.assertEqual(convert.topic_of(fx.message(4, "x")), 0)

    def test_link_preview_is_text(self):
        self.assertEqual(media_of(fx.webpage(), "see https://x.com").kind, "text")

    def test_nothing_to_post(self):
        for msg in (fx.message(1, ""), fx.message(1, "", media=fx.webpage()), fx.service(1)):
            result = convert.to_message(msg, "Alex")
            self.assertIsInstance(result, Skip)
            self.assertTrue(result.quiet)
        dice = convert.to_message(fx.message(1, "", media=T.MessageMediaDice(value=6, emoticon="🎲")), "x")
        self.assertIsInstance(dice, Skip)
        self.assertFalse(dice.quiet)  # worth a log line: it was something, just not forwardable

    def test_location_becomes_text_with_a_map_link(self):
        m = media_of(fx.geo())
        self.assertEqual(m.body.text, "📍 Location\nhttps://maps.google.com/?q=12.971600,77.594600")


class MediaTests(unittest.TestCase):
    def test_kinds_and_their_details(self):
        cases = {
            "photo": (fx.photo(), {}),
            "voice": (fx.voice(), {"seconds": 9}),
            "video_note": (fx.video_note(), {"seconds": 5, "width": 384, "height": 384}),
            "animation": (fx.gif(), {"seconds": 2, "width": 320, "height": 240}),
            "video": (fx.video(), {"seconds": 12, "width": 1280, "height": 720}),
            "audio": (fx.audio(), {"seconds": 180, "title": "Song", "performer": "Band"}),
            "sticker": (fx.sticker(), {"mimetype": "image/webp"}),
            "document": (fx.document(), {"filename": "Q3 results.pdf", "mimetype": "application/pdf"}),
        }
        for kind, (media, expected) in cases.items():
            m = media_of(media)
            self.assertEqual((m.kind, m.label, m.media.kind), ("media", kind, kind), kind)
            for field, value in expected.items():
                self.assertEqual(getattr(m.media, field), value, (kind, field))
            self.assertIs(m.media.ref, media)
            self.assertEqual((m.media.chat_id, m.media.msg_id), (fx.CHAT, 1))

    def test_caption(self):
        self.assertEqual(media_of(fx.photo(), "chart").body.text, "chart")

    def test_voice_waveform_round_trips(self):
        m = media_of(fx.voice())
        self.assertTrue(all(0 <= b <= 100 for b in m.media.waveform))
        self.assertEqual(telegram_waveform(m.media.waveform), fx.WAVEFORM)

    def test_animated_sticker_goes_as_a_file(self):
        m = media_of(fx.document(name="AnimatedSticker.tgs", mime="application/x-tgsticker"))
        self.assertEqual(m.media.kind, "document")
        tgs = T.MessageMediaDocument(document=fx._document("application/x-tgsticker", [
            T.DocumentAttributeSticker(alt="😀", stickerset=T.InputStickerSetEmpty())]))
        self.assertEqual(media_of(tgs).media.kind, "document")

    def test_contact_goes_as_a_vcard(self):
        m = media_of(fx.contact())
        self.assertEqual((m.media.kind, m.media.filename), ("document", "Bob Lee.vcf"))


class PollTests(unittest.TestCase):
    def test_poll(self):
        m = media_of(fx.poll(close_period=60))
        p = m.poll
        self.assertEqual((m.kind, p.question, p.options, p.multiple, p.public, p.quiz, p.close_period),
                         ("poll", "Which?", ["TCS", "INFY"], True, True, False, 60))

    def test_quiz_knows_its_answer_when_telegram_says_it(self):
        self.assertEqual(media_of(fx.poll(multiple=False, quiz=True, correct=1)).poll.correct, 1)
        self.assertIsNone(media_of(fx.poll(multiple=False, quiz=True)).poll.correct)


class SenderTests(unittest.TestCase):
    def test_display_names(self):
        d = convert.sender_display
        self.assertEqual(d(fx.user()), "Alex Doe (@alex_doe)")
        self.assertEqual(d(fx.user(last=None)), "Alex (@alex_doe)")
        self.assertEqual(d(fx.user(username=None)), "Alex Doe")
        self.assertEqual(d(fx.user(first=None, last=None)), "@alex_doe")
        self.assertEqual(d(fx.user(first=None, last=None, username=None)), "(unknown user)")
        self.assertEqual(d(fx.channel()), "Market News")
        self.assertEqual(d(None), "(unknown user)")


if __name__ == "__main__":
    unittest.main()
