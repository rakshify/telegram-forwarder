"""`forward` end to end: Telegram source -> pipeline -> Telegram (fake bot) and Signal (fake API).

The expectations follow what the old forwarder (_forward_one) sent, checked
side by side against it. Where they differ, a comment says why.
"""
import os
import tempfile
import unittest
from pathlib import Path

from telethon.tl import types as T

from src import users
from src.adapters.signal.sink import SignalSink
from src.adapters.telegram import forward
from src.adapters.telegram.sink import TelegramSink
from src.adapters.telegram.source import Chat
from src.core.model import SIGNAL, TELEGRAM, Dest, Routes
from src.core.store import Store
from tests import signal_helpers
from tests import telegram_fixtures as fx
from tests.telegram_fixtures import Event, FakeUserClient, run
from tests.telegram_helpers import FakeBot


class Forwarder:
    def __init__(self, *pairs, store=None, signal_api=None, dry_run=False):
        """pairs: (Chat, Dest, attribution)."""
        user = users.UserRecord(short_id="u00001", user_id=7, first_name="Me", last_name=None,
                                username=None, phone=None)
        cfg = Routes(TELEGRAM, "u00001")
        for chat, dest, attribution in pairs or ((Chat(fx.CHAT, 1), Dest(TELEGRAM, "999", hash=1), False),):
            cfg.add(chat.key, chat, dest, attribution)
        self.bot = FakeBot()
        tg = TelegramSink("unused")
        tg.client = self.bot
        sinks = {TELEGRAM: tg}
        if any(p.dest.platform == SIGNAL for p in cfg.pairs):
            sinks[SIGNAL], self.signal_api = signal_helpers.make_sink(signal_api)
        self.pipeline = forward.build(cfg, user, store or Store(":memory:"), sinks=sinks, dry_run=dry_run)
        for p in cfg.pairs:
            p.dest.handle = f"peer{p.dest.chat}"
        self.source = self.pipeline.source
        self.source.client = FakeUserClient()
        self.source.client.connected = True
        self.source.peers = {c.chat_id: f"peer{c.chat_id}" for c in cfg.sources.values()}
        self.source._catching_up = False

        async def no_sleep(_s):
            return None
        self.pipeline.sleep = no_sleep

    def feed(self, *messages):
        async def go():
            for m in messages:
                await self.source._on_new_message(Event(m))
            while (item := self.pipeline.store.next_pending()) is not None:
                await self.pipeline._handle(item)
                after = self.pipeline.store.next_pending()
                if after is not None and after.seq == item.seq and after.attempts == item.attempts:
                    return
        run(go())
        return self.bot.calls


def one(calls):
    (call,) = calls
    return call


def entity_view(entities):
    return sorted((type(e).__name__, e.offset, e.length) for e in entities or [])


def attrs(kw, cls):
    return [a for a in kw.get("attributes") or [] if isinstance(a, cls)]


def tg_pair(topic=0, dest_topic=0, attribution=False, dest="999"):
    return Chat(fx.CHAT, 1, topic), Dest(TELEGRAM, dest, hash=1, topic=dest_topic), attribution


class TextTests(unittest.TestCase):
    def test_text_and_every_entity_pass_through(self):
        entities = [T.MessageEntityCustomEmoji(3, 2, document_id=99), T.MessageEntityBold(5, 4),
                    T.MessageEntityTextUrl(10, 4, url="https://x.com"),
                    T.MessageEntityMentionName(15, 4, user_id=42), T.MessageEntityPre(20, 4, language="py")]
        kind, peer, text, kw = one(Forwarder().feed(fx.message(1, "Hi 🚀 bold link @bob code", entities=entities)))
        self.assertEqual((kind, peer, text), ("message", "peer999", "Hi 🚀 bold link @bob code"))
        self.assertEqual(entity_view(kw["formatting_entities"]), entity_view(entities))
        self.assertEqual(kw["formatting_entities"][2].url, "https://x.com")
        self.assertIsNone(kw["reply_to"])

    def test_attribution_as_before(self):
        f = Forwarder(tg_pair(attribution=True))
        _, _, text, kw = one(f.feed(fx.message(1, "hello world", entities=[T.MessageEntityItalic(6, 5)])))
        self.assertEqual(text, "Alex Doe (@alex_doe):\n\nhello world")
        self.assertEqual(entity_view(kw["formatting_entities"]), [("MessageEntityBold", 0, 20),
                                                                  ("MessageEntityItalic", 29, 5)])

    def test_attribution_counts_emoji_in_names_right(self):
        # The old forwarder shifted entities by Python characters, not UTF-16 units.
        f = Forwarder(tg_pair(attribution=True))
        _, _, text, kw = one(f.feed(fx.message(1, "hi", entities=[T.MessageEntityBold(0, 2)],
                                               sender=fx.user(first="Rahul 🦁", last=None, username=None))))
        self.assertEqual(text, "Rahul 🦁:\n\nhi")
        self.assertEqual(entity_view(kw["formatting_entities"]), [("MessageEntityBold", 0, 8),
                                                                  ("MessageEntityBold", 11, 2)])

    def test_channel_posts_are_attributed_to_the_channel(self):
        f = Forwarder(tg_pair(attribution=True))
        self.assertEqual(one(f.feed(fx.message(1, "news", sender=fx.channel())))[2], "Market News:\n\nnews")

    def test_link_preview_is_rebuilt_from_the_text(self):
        kind, _, text, _ = one(Forwarder().feed(fx.message(1, "see https://x.com", media=fx.webpage())))
        self.assertEqual((kind, text), ("message", "see https://x.com"))

    def test_location_is_now_forwarded_as_text(self):  # the old forwarder dropped it
        kind, _, text, _ = one(Forwarder().feed(fx.message(1, "", media=fx.geo())))
        self.assertEqual(kind, "message")
        self.assertIn("maps.google.com/?q=12.971600,77.594600", text)

    def test_service_messages_and_empty_ones_post_nothing(self):
        self.assertEqual(Forwarder().feed(fx.service(1), fx.message(2, "")), [])


class MediaTests(unittest.TestCase):
    def send(self, media, text=""):
        kind, peer, file, kw = one(Forwarder().feed(fx.message(1, text, media=media)))
        self.assertEqual((kind, peer), ("file", "peer999"))
        return os.path.basename(file), kw

    def test_photo(self):
        name, kw = self.send(fx.photo(), "chart")
        self.assertEqual((name, kw["caption"], kw["force_document"]), ("photo.jpg", "chart", False))

    def test_voice_note_keeps_duration_and_waveform(self):
        name, kw = self.send(fx.voice())
        self.assertTrue(kw["voice_note"])
        (audio,) = attrs(kw, T.DocumentAttributeAudio)
        self.assertEqual((audio.voice, audio.duration, audio.waveform), (True, 9, fx.WAVEFORM))

    def test_round_video(self):
        _, kw = self.send(fx.video_note())
        self.assertTrue(kw["video_note"])
        (video,) = attrs(kw, T.DocumentAttributeVideo)
        self.assertEqual((video.round_message, video.w, video.h, video.duration), (True, 384, 384, 5.0))

    def test_gif_is_sent_as_a_gif(self):  # the old forwarder's video=True was ignored by Telethon
        _, kw = self.send(fx.gif())
        self.assertTrue(attrs(kw, T.DocumentAttributeAnimated))
        self.assertFalse(kw["force_document"])

    def test_video_streams_and_keeps_its_size(self):
        _, kw = self.send(fx.video(), "watch")
        (video,) = attrs(kw, T.DocumentAttributeVideo)
        self.assertEqual((kw["supports_streaming"], video.w, video.h, video.duration), (True, 1280, 720, 12.0))
        self.assertEqual(kw["caption"], "watch")

    def test_audio_keeps_title_and_performer(self):
        _, kw = self.send(fx.audio())
        (audio,) = attrs(kw, T.DocumentAttributeAudio)
        self.assertEqual((audio.title, audio.performer, audio.duration, audio.voice), ("Song", "Band", 180, False))

    def test_sticker_is_sent_as_a_sticker(self):  # the old forwarder uploaded the .webp as a file
        _, kw = self.send(fx.sticker())
        self.assertTrue(attrs(kw, T.DocumentAttributeSticker))

    def test_document_keeps_its_name_and_stays_a_file(self):
        name, kw = self.send(fx.document(), "report")
        self.assertEqual((name, kw["force_document"], kw["caption"]), ("Q3 results.pdf", True, "report"))
        self.assertEqual(attrs(kw, T.DocumentAttributeFilename)[0].file_name, "Q3 results.pdf")


class PollTests(unittest.TestCase):
    def poll(self, media, attribution=False):
        kind, _, file, kw = one(Forwarder(tg_pair(attribution=attribution)).feed(fx.message(1, "", media=media)))
        self.assertIsInstance(file, T.InputMediaPoll)
        return file

    def test_poll_is_forwarded(self):  # the old forwarder crashed here (Poll now needs `hash`)
        media = self.poll(fx.poll())
        p = media.poll
        self.assertEqual((p.question.text, [a.text.text for a in p.answers], p.multiple_choice, p.public_voters),
                         ("Which?", ["TCS", "INFY"], True, True))

    def test_attribution_names_who_asked(self):
        self.assertEqual(self.poll(fx.poll(), attribution=True).poll.question.text, "Alex Doe (@alex_doe): Which?")

    def test_quiz_with_its_answer(self):
        media = self.poll(fx.poll(multiple=False, quiz=True, correct=1))
        self.assertEqual((media.poll.quiz, media.correct_answers), (True, [1]))

    def test_quiz_without_a_known_answer_becomes_a_regular_poll(self):
        media = self.poll(fx.poll(multiple=False, quiz=True))
        self.assertIsNone(media.poll.quiz)


class ThreadingTests(unittest.TestCase):
    def test_replies_thread(self):
        calls = Forwarder().feed(fx.message(1, "first"), fx.message(2, "second", reply_to=1))
        self.assertEqual([c[3]["reply_to"] for c in calls], [None, 101])

    def test_replies_thread_after_a_restart(self):  # the old reply map lived in memory only
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tg_state.sqlite"
            first = Forwarder(store=Store(path))
            first.feed(fx.message(1, "first"))
            first.pipeline.store.close()
            second = Forwarder(store=Store(path))
            second.bot.next_id = 500
            (call,) = second.feed(fx.message(2, "second", reply_to=1))
            self.assertEqual(call[3]["reply_to"], 101)

    def test_topics_route_both_ways(self):
        f = Forwarder(tg_pair(topic=17, dest_topic=12))
        calls = f.feed(fx.message(1, "in 17", topic=17), fx.message(2, "in 18", topic=18),
                       fx.message(3, "reply in 17", reply_to=1, topic=17))
        self.assertEqual([(c[2], c[3]["reply_to"]) for c in calls], [("in 17", 12), ("reply in 17", 101)])


class DryRunTests(unittest.TestCase):
    def test_dry_run_posts_and_downloads_nothing(self):
        f = Forwarder(dry_run=True)
        self.assertEqual(f.feed(fx.message(1, "hi"), fx.message(2, "", media=fx.photo())), [])
        self.assertEqual(f.source.client.downloads, [])


class SignalTests(unittest.TestCase):
    def test_telegram_to_signal_and_telegram(self):
        f = Forwarder(tg_pair(attribution=True), (Chat(fx.CHAT, 1), Dest(SIGNAL, "group.AAAA"), True))
        msg = fx.message(1, "see this here", entities=[T.MessageEntityTextUrl(9, 4, url="https://x.com")])
        (call,) = f.feed(msg)
        self.assertEqual(call[2], "Alex Doe (@alex_doe):\n\nsee this here")
        (sent,) = f.signal_api.sent
        self.assertEqual(sent["message"], "Alex Doe (@alex_doe):\n\nsee this here (https://x.com)")
        self.assertEqual(sent["recipients"], ["group.AAAA"])

    def test_telegram_photo_to_signal(self):
        f = Forwarder((Chat(fx.CHAT, 1), Dest(SIGNAL, "+15550000001"), False))
        f.feed(fx.message(1, "chart", media=fx.photo()))
        (sent,) = f.signal_api.sent
        self.assertTrue(sent["base64_attachments"][0].startswith("data:image/jpeg;filename=photo.jpg;base64,"))
        self.assertEqual(sent["message"], "chart")


if __name__ == "__main__":
    unittest.main()
