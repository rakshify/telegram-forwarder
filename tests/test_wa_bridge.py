"""wa-forward end to end: the WhatsApp source and the pipeline, with fake Telegram and Signal
destinations and a fake WhatsApp client."""
import asyncio
import base64
import unittest

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message as WAMessage
from telethon import errors, types

from src.adapters.telegram import sink as tg_sink
from src.adapters.whatsapp import accounts, bridge as bridge_mod, source as source_mod
from src.core import pipeline as pipeline_mod
from src.core.model import WHATSAPP, Dest, Pair, Routes
from src.core.store import Store
from tests import signal_helpers
from tests import wa_helpers as h
from tests.signal_helpers import FIRST_TS, SENDER, FakeSignalAPI
from tests.telegram_helpers import FakeBot

GROUP = h.GROUP


def tg(dest_id=111, dest_hash=1, topic=0, attribution=False, source=GROUP) -> Pair:
    return Pair(source=source, dest=Dest("telegram", str(dest_id), hash=dest_hash, topic=topic),
                  attribution=attribution)


def sig(chat="group.AAAA", attribution=False, source=GROUP) -> Pair:
    return Pair(source=source, dest=Dest("signal", chat), attribution=attribution)


class FakeWA:
    def __init__(self, fail=False):
        self.fail = fail
        self.downloads = 0

    async def download_any(self, message, path=None):
        self.downloads += 1
        if self.fail:
            raise RuntimeError("download failed with status code 410")
        with open(path, "wb") as f:
            f.write(b"media-bytes")


def make_bridge(*pairs, dry_run=False, signal_api=None):
    acct = accounts.WaAccount(short_id="t00001", phone="919999999999",
                              jid="919999999999@s.whatsapp.net", push_name="Me")
    pairs = pairs or (tg(attribution=True),)
    for p in pairs:
        p.dest.handle = f"peer{p.dest.chat}"  # as if resolved at startup
    b = bridge_mod.Bridge(acct, Routes(WHATSAPP, "t00001", pairs=list(pairs)),
                          dry_run=dry_run, store=Store(":memory:"))
    b.telegram.client = FakeBot()
    if "signal" in b.sinks:
        b.sinks["signal"], _ = signal_helpers.make_sink(signal_api)
    b.source.wa = FakeWA()
    b.source.connected = True

    async def no_sleep(_s):
        return None
    b.pipeline.sleep = no_sleep
    return b


async def feed(b, *events):
    for ev in events:
        await b.source._on_message(ev)
    while True:
        item = b.store.next_pending()
        if item is None:
            return
        await b.pipeline._handle(item)
        if b.store.next_pending() is not None and b.store.next_pending().seq == item.seq \
                and b.store.next_pending().attempts == item.attempts:
            return  # waiting (not connected); don't spin


def run(coro):
    return asyncio.run(coro)


def entity_view(text, entities):
    """[(type, covered text)] using UTF-16 offsets, the way Telegram reads them."""
    data = text.encode("utf-16-le")
    out = []
    for e in entities:
        piece = data[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le")
        out.append((type(e).__name__.replace("MessageEntity", ""), piece))
    return sorted(out)


class TextForwardingTests(unittest.TestCase):
    def test_attribution_and_formatting(self):
        b = make_bridge()
        run(feed(b, h.event(h.text("*buy* 🚀 now"), pushname="Rahul 🦁")))
        (kind, peer, text, kw), = b.telegram.client.calls
        self.assertEqual((kind, peer, text), ("message", "peer111", "Rahul 🦁:\n\nbuy 🚀 now"))
        self.assertEqual(entity_view(text, kw["formatting_entities"]),
                         [("Bold", "Rahul 🦁"), ("Bold", "buy")])
        self.assertIsNone(kw["parse_mode"])
        self.assertIsNone(kw["reply_to"])

    def test_no_attribution(self):
        b = make_bridge(tg())
        run(feed(b, h.event(h.text("hello"))))
        self.assertEqual(b.telegram.client.calls[0][2], "hello")
        self.assertEqual(b.telegram.client.calls[0][3]["formatting_entities"], [])

    def test_reply_threads_when_original_was_forwarded(self):
        b = make_bridge()
        run(feed(b, h.event(h.text("first"), msg_id="A"),
                 h.event(h.ext_text("second", reply_to="A", quoted=h.text("first")), msg_id="B")))
        first_id = 101
        self.assertEqual(b.telegram.client.calls[1][3]["reply_to"], first_id)
        self.assertNotIn("first", b.telegram.client.calls[1][2])

    def test_reply_to_unknown_message_gets_a_quote(self):
        b = make_bridge()
        b.store.remember_name("2222@lid", "Priya")
        run(feed(b, h.event(h.ext_text("agree", reply_to="OLD", quoted_sender="2222@lid",
                                       quoted=h.text("target *120*")))))
        (_, _, text, kw), = b.telegram.client.calls
        self.assertEqual(text, "Rahul:\n\nPriya: target 120\nagree")
        self.assertIn(("Blockquote", "Priya: target 120"), entity_view(text, kw["formatting_entities"]))
        self.assertIsNone(kw["reply_to"])

    def test_topic_routing(self):
        b = make_bridge(tg(topic=7))
        run(feed(b, h.event(h.text("x"))))
        self.assertEqual(b.telegram.client.calls[0][3]["reply_to"], 7)

    def test_long_text_is_split(self):
        b = make_bridge(tg())
        run(feed(b, h.event(h.text(("word " * 900).strip()))))
        texts = [c[2] for c in b.telegram.client.calls]
        self.assertEqual(len(texts), 2)
        self.assertTrue(all(len(t) <= 4096 for t in texts))

    def test_duplicate_delivery_is_posted_once(self):
        b = make_bridge()
        ev = h.event(h.text("once"), msg_id="SAME")
        run(feed(b, ev, ev))
        self.assertEqual(len(b.telegram.client.calls), 1)

    def test_key_handover_then_content_with_the_same_id(self):
        # What WhatsApp really sent for the first test message: a key
        # handover, then the text, both under one message id.
        b = make_bridge()
        handover = WAMessage()
        handover.senderKeyDistributionMessage.groupID = GROUP
        handover.messageContextInfo.deviceListMetadataVersion = 2
        content = h.text("first message")
        content.messageContextInfo.deviceListMetadataVersion = 2
        run(feed(b, h.event(handover, msg_id="SAME", from_me=True),
                 h.event(content, msg_id="SAME", from_me=True)))
        self.assertEqual([c[2] for c in b.telegram.client.calls], ["Rahul:\n\nfirst message"])
        self.assertEqual(b.store.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)

    def test_same_id_twice_with_content_posts_once(self):
        b = make_bridge()
        first = h.text("hello")
        second = h.text("hello")
        second.messageContextInfo.deviceListMetadataVersion = 3  # different payload, same id
        run(feed(b, h.event(first, msg_id="X"), h.event(second, msg_id="X")))
        self.assertEqual(len(b.telegram.client.calls), 1)

    def test_order_is_kept(self):
        b = make_bridge(tg())
        run(feed(b, *[h.event(h.text(str(i)), msg_id=f"M{i}") for i in range(5)]))
        self.assertEqual([c[2] for c in b.telegram.client.calls], ["0", "1", "2", "3", "4"])

    def test_other_chats_are_ignored(self):
        b = make_bridge()
        run(feed(b, h.event(h.text("x"), chat="999@g.us"),
                 h.event(h.text("status"), chat="status@broadcast")))
        self.assertEqual(b.telegram.client.calls, [])

    def test_one_to_one_chat_matched_by_alternate_id(self):
        b = make_bridge(tg(source="919800000000@s.whatsapp.net"))
        run(feed(b, h.event(h.text("dm"), chat="7777@lid", sender="7777@lid",
                            sender_alt="919800000000@s.whatsapp.net")))
        self.assertEqual(len(b.telegram.client.calls), 1)

    def test_dry_run_posts_nothing(self):
        b = make_bridge(dry_run=True)
        run(feed(b, h.event(h.text("x")), h.event(h.image("c"), msg_id="I")))
        self.assertEqual(b.telegram.client.calls, [])
        self.assertEqual(b.source.wa.downloads, 0)


class MediaForwardingTests(unittest.TestCase):
    def test_photo_with_caption(self):
        b = make_bridge()
        run(feed(b, h.event(h.image("chart"))))
        (kind, peer, file, kw), = b.telegram.client.calls
        self.assertEqual((kind, kw["caption"], kw["force_document"]), ("file", "Rahul:\n\nchart", False))
        self.assertTrue(file.endswith("photo.jpg"))

    def test_voice_note(self):
        b = make_bridge(tg())
        run(feed(b, h.event(h.voice(seconds=9))))
        kw = b.telegram.client.calls[0][3]
        self.assertTrue(kw["voice_note"])
        attr = kw["attributes"][0]
        self.assertIsInstance(attr, types.DocumentAttributeAudio)
        self.assertEqual((attr.duration, attr.voice), (9, True))
        self.assertTrue(attr.waveform)
        self.assertTrue(b.telegram.client.calls[0][2].endswith(".ogg"))

    def test_video_attributes(self):
        b = make_bridge()
        run(feed(b, h.event(h.video("v", seconds=12))))
        attr = b.telegram.client.calls[0][3]["attributes"][0]
        self.assertEqual((attr.duration, attr.w, attr.h, attr.supports_streaming), (12.0, 640, 360, True))

    def test_document_keeps_name(self):
        b = make_bridge()
        run(feed(b, h.event(h.document("Q3 results.pdf"))))
        (_, _, file, kw), = b.telegram.client.calls
        self.assertTrue(file.endswith("Q3 results.pdf"))
        self.assertTrue(kw["force_document"])

    def test_long_caption_continues_as_reply(self):
        b = make_bridge(tg())
        run(feed(b, h.event(h.image(("long " * 400).strip()))))
        media, more = b.telegram.client.calls[0], b.telegram.client.calls[1:]
        self.assertLessEqual(len(media[3]["caption"]), 1024)
        self.assertTrue(more)
        self.assertTrue(all(c[3]["reply_to"] == 101 for c in more))

    def test_static_sticker_has_no_caption(self):
        b = make_bridge()
        run(feed(b, h.event(h.sticker())))
        kw = b.telegram.client.calls[0][3]
        self.assertEqual(kw["caption"], "")
        self.assertIsInstance(kw["attributes"][0], types.DocumentAttributeSticker)

    def test_unavailable_media_posts_the_text(self):
        b = make_bridge()
        b.source.wa = FakeWA(fail=True)
        run(feed(b, h.event(h.image("cap"))))
        (kind, _, text, _), = b.telegram.client.calls
        self.assertEqual(kind, "message")
        self.assertIn("cap", text)
        self.assertIn("couldn't be downloaded", text)

    def test_waits_for_whatsapp_before_downloading(self):
        b = make_bridge()
        b.source.connected = False

        async def stop_instead_of_sleeping(_s):
            b.pipeline.request_stop()
        b.pipeline.sleep = stop_instead_of_sleeping
        run(feed(b, h.event(h.image("cap"))))
        self.assertEqual(b.telegram.client.calls, [])
        self.assertEqual(b.store.pending_count(), 1)


class PollAndEditTests(unittest.TestCase):
    def test_poll(self):
        b = make_bridge()
        run(feed(b, h.event(h.poll("Which?", ["TCS", "INFY"], selectable=0))))
        media = b.telegram.client.calls[0][3]["file"] if "file" in b.telegram.client.calls[0][3] else b.telegram.client.calls[0][2]
        self.assertIsInstance(media, types.InputMediaPoll)
        self.assertEqual(media.poll.question.text, "Rahul: Which?")
        self.assertEqual([a.text.text for a in media.poll.answers], ["TCS", "INFY"])
        self.assertTrue(media.poll.multiple_choice)

    def test_poll_too_big_for_telegram_becomes_text(self):
        b = make_bridge()
        run(feed(b, h.event(h.poll("Pick", [f"opt{i}" for i in range(12)]))))
        (kind, _, text, _), = b.telegram.client.calls
        self.assertEqual(kind, "message")
        self.assertIn("12. opt11", text)

    def test_edit_applies_to_forwarded_copy(self):
        b = make_bridge()
        run(feed(b, h.event(h.text("target 100"), msg_id="A"),
                 h.event(h.edit("A", "target *110*"), msg_id="E")))
        kind, peer, msg_id, text, kw = b.telegram.client.calls[1]
        self.assertEqual((kind, msg_id, text), ("edit", 101, "Rahul:\n\ntarget 110"))
        self.assertEqual(entity_view(text, kw["formatting_entities"]),
                         [("Bold", "110"), ("Bold", "Rahul")])

    def test_edit_of_unknown_message_is_ignored(self):
        b = make_bridge()
        run(feed(b, h.event(h.edit("NEVER", "x"))))
        self.assertEqual(b.telegram.client.calls, [])


class UnreachableDestinationTests(unittest.TestCase):
    def test_other_pairs_keep_working(self):
        ok = tg()
        missing = tg(222, dest_hash=0)
        b = make_bridge(ok, missing)
        missing.dest.handle = None  # the bot isn't in that chat
        run(feed(b, h.event(h.text("hi"))))
        self.assertEqual([c[1] for c in b.telegram.client.calls], ["peer111"])
        self.assertEqual(b.store.db.execute("SELECT status FROM inbox").fetchone()[0], "done")

    def test_destination_is_retried_until_reachable(self):
        b = make_bridge()
        pair = b.cfg.pairs[0]
        attempts = {"n": 0}

        async def fake_resolve(bot, dest_id, dest_hash, role, for_bot):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise SystemExit("Bot could not resolve dest chat (id=111): ChatIdInvalidError\n"
                                 "Make sure the bot is a member of that chat with permission to post.")
            return "peer-now"

        original = tg_sink.resolve_peer
        tg_sink.resolve_peer = fake_resolve
        try:
            self.assertFalse(run(b.pipeline._resolve_dest(pair, first_try=True)))
            self.assertIsNone(pair.dest.handle)
            self.assertTrue(run(b.pipeline._resolve_dest(pair)))
            self.assertEqual(pair.dest.handle, "peer-now")
        finally:
            tg_sink.resolve_peer = original


class FailureTests(unittest.TestCase):
    def test_retry_skips_pairs_already_done(self):
        p1 = tg()
        p2 = tg(222)
        b = make_bridge(p1, p2)
        calls = {"n": 0}
        real = b.telegram.client.send_message

        async def flaky(peer, text, **kw):
            calls["n"] += 1
            if peer == "peer222" and calls["n"] == 2:
                raise ConnectionError("network blip")
            return await real(peer, text, **kw)
        b.telegram.client.send_message = flaky

        run(feed(b, h.event(h.text("hi"))))
        peers = [c[1] for c in b.telegram.client.calls]
        self.assertEqual(peers, ["peer111", "peer222"])  # 111 once, 222 after the retry

    def test_rejected_by_telegram_is_not_retried(self):
        b = make_bridge()
        b.telegram.client.fail = [errors.ChatWriteForbiddenError(request=None)]
        run(feed(b, h.event(h.text("hi"), msg_id="A"), h.event(h.text("next"), msg_id="B")))
        self.assertEqual([c[2] for c in b.telegram.client.calls], ["Rahul:\n\nnext"])
        status = b.store.db.execute("SELECT status FROM inbox WHERE msg_id='A'").fetchone()[0]
        self.assertEqual(status, "failed")

    def test_bad_request_gets_one_more_try(self):
        b = make_bridge()
        b.telegram.client.fail = [errors.MediaEmptyError(request=None)]
        run(feed(b, h.event(h.text("hi"))))
        self.assertEqual(len(b.telegram.client.calls), 1)  # second try went through
        b2 = make_bridge()
        b2.telegram.client.fail = [errors.MediaEmptyError(request=None)] * 2
        run(feed(b2, h.event(h.text("hi"))))
        self.assertEqual(b2.telegram.client.calls, [])
        self.assertEqual(b2.store.db.execute("SELECT status FROM inbox").fetchone()[0], "failed")

    def test_stored_device_reads_names_with_commas(self):
        import sqlite3
        import tempfile
        from src.adapters.whatsapp import client as wa
        path = tempfile.mkdtemp() + "/wa.db"
        self.assertIsNone(wa.stored_device(path))
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, push_name TEXT)")
        self.assertIsNone(wa.stored_device(path))
        con.execute("INSERT INTO whatsmeow_device VALUES ('91999:3@s.whatsapp.net', 'Rahul, CFA')")
        con.commit()
        con.close()
        self.assertEqual(wa.stored_device(path).push_name, "Rahul, CFA")

    def test_gives_up_after_retries(self):
        b = make_bridge()
        b.telegram.client.fail = [RuntimeError("boom")] * (len(pipeline_mod.RETRY_DELAYS) + 1)
        run(feed(b, h.event(h.text("hi"))))
        self.assertEqual(b.telegram.client.calls, [])
        row = b.store.db.execute("SELECT status, attempts FROM inbox").fetchone()
        self.assertEqual(row, ("failed", len(pipeline_mod.RETRY_DELAYS) + 1))


class OwnNameTests(unittest.TestCase):
    def test_own_messages_use_the_profile_name(self):
        b = make_bridge()
        b.source.my_name, b.account.push_name = "", None
        run(feed(b, h.event(h.text("mine"), msg_id="A", pushname="", from_me=True)))
        self.assertEqual(b.telegram.client.calls[0][2], "Unknown:\n\nmine")

        class Setting:  # shape of PushNameSettingEv
            class Action:
                name = "Ankit K"
        run(b.source._on_push_name_setting(Setting()))
        self.assertEqual(b.account.push_name, "Ankit K")
        run(feed(b, h.event(h.text("mine too"), msg_id="B", pushname="", from_me=True)))
        self.assertEqual(b.telegram.client.calls[1][2], "Ankit K:\n\nmine too")

    def test_profile_name_is_fetched_when_missing(self):
        from src.adapters.whatsapp import client as wa

        class Me:
            def __init__(self, name):
                self.PushName = name

        class FakeClient:
            def __init__(self):
                self.fetched = []
                self.name = ""

            async def get_me(self):
                return Me(self.name)

            async def fetch_app_state(self, name, full_sync=False):
                self.fetched.append((name, full_sync))
                self.name = "Ankit K"

        c = FakeClient()
        self.assertEqual(run(wa.own_profile_name(c)), "Ankit K")
        self.assertEqual(c.fetched, [("critical_block", True)])
        self.assertEqual(run(wa.own_profile_name(c)), "Ankit K")
        self.assertEqual(len(c.fetched), 1)  # not fetched again once known


class SignalDestinationTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeSignalAPI()

    def bridge(self, *pairs):
        return make_bridge(*pairs, signal_api=self.api)

    def status(self, b):
        return b.store.db.execute("SELECT status FROM inbox ORDER BY seq DESC").fetchone()[0]

    def test_fans_out_to_telegram_and_signal(self):
        b = self.bridge(tg(attribution=True), sig(attribution=True))
        run(feed(b, h.event(h.text("*buy* 🚀 now"))))
        self.assertEqual([c[2] for c in b.telegram.client.calls], ["Rahul:\n\nbuy 🚀 now"])
        self.assertEqual(self.api.sent, [{"message": "Rahul:\n\nbuy 🚀 now", "number": SENDER,
                                          "recipients": ["group.AAAA"], "text_mode": "normal"}])
        self.assertEqual(b.store.posted(b.cfg.pairs[1].key, "WA1"), (FIRST_TS, False))

    def test_reply_quotes_the_message_forwarded_earlier(self):
        b = self.bridge(sig())
        run(feed(b, h.event(h.text("first"), msg_id="A"),
                 h.event(h.ext_text("second", reply_to="A", quoted=h.text("first")), msg_id="B")))
        first, second = self.api.sent
        self.assertNotIn("quote_timestamp", first)
        self.assertEqual((second["message"], second["quote_timestamp"], second["quote_author"]),
                         ("second", FIRST_TS, SENDER))

    def test_reply_to_unknown_message_is_quoted_as_lines(self):
        b = self.bridge(sig(attribution=True))
        b.store.remember_name("2222@lid", "Priya")
        run(feed(b, h.event(h.ext_text("agree", reply_to="OLD", quoted_sender="2222@lid",
                                       quoted=h.text("target *120*")))))
        self.assertEqual(self.api.sent[0]["message"], "Rahul:\n\n> Priya: target 120\nagree")

    def test_photo_goes_as_an_attachment(self):
        b = self.bridge(sig(attribution=True))
        run(feed(b, h.event(h.image("chart"))))
        (sent,) = self.api.sent
        self.assertEqual(sent["message"], "Rahul:\n\nchart")
        prefix, data = sent["base64_attachments"][0].split(",", 1)
        self.assertEqual(prefix, "data:image/jpeg;filename=photo.jpg;base64")
        self.assertEqual(base64.b64decode(data), b"media-bytes")
        self.assertEqual(b.store.posted(b.cfg.pairs[0].key, "WA1"), (FIRST_TS, True))

    def test_poll_becomes_a_numbered_list(self):
        b = self.bridge(sig())
        run(feed(b, h.event(h.poll("Which?", ["TCS", "INFY"]))))
        self.assertEqual(self.api.sent[0]["message"], "📊 Which?\n1. TCS\n2. INFY")

    def test_edit_applies_to_the_signal_copy(self):
        b = self.bridge(sig())
        run(feed(b, h.event(h.text("target 100"), msg_id="A"),
                 h.event(h.edit("A", "target *110*"), msg_id="E")))
        _, edit = self.api.sent
        self.assertEqual((edit["edit_timestamp"], edit["message"]), (FIRST_TS, "target 110"))

    def test_signal_down_does_not_hold_up_telegram(self):
        b = self.bridge(sig(), tg())
        signal_pair = b.cfg.pairs[0]
        self.api.down = True
        run(feed(b, h.event(h.text("one"), msg_id="A")))
        self.assertIsNone(signal_pair.dest.handle)  # paused until the watchdog finds it again
        self.assertEqual(self.status(b), "done")
        run(feed(b, h.event(h.text("two"), msg_id="B")))
        self.assertEqual([c[2] for c in b.telegram.client.calls], ["one", "two"])

        self.api.down = False
        self.assertTrue(run(b.pipeline._resolve_dest(signal_pair)))
        run(feed(b, h.event(h.text("three"), msg_id="C")))
        self.assertEqual([s["message"] for s in self.api.sent], ["three"])

    def test_signal_refusal_does_not_stop_telegram(self):
        b = self.bridge(sig(), tg())
        self.api.fail = [(400, {"error": "Failed to send message"})] * 2
        run(feed(b, h.event(h.text("hi"))))
        self.assertEqual([c[2] for c in b.telegram.client.calls], ["hi"])  # not again on the retry
        sends = [r for r in self.api.requests if r == ("POST", "/v2/send")]
        self.assertEqual(len(sends), 2)  # tried once more, then given up
        self.assertEqual(self.status(b), "failed")


class LifecycleTests(unittest.TestCase):
    def test_run_halts_when_not_linked_then_stops_cleanly(self):
        b = make_bridge()
        b.source.wa = None    # as before start(): no WhatsApp client yet
        del b.pipeline.sleep  # the real, stop-aware sleep: the watchdog loops on it

        async def started():
            pass
        b.telegram.start = started

        async def resolved(bot, dest_id, dest_hash, role, for_bot):
            return "peer-resolved"

        async def scenario():
            async def stop_soon():
                await asyncio.sleep(0.05)
                b.pipeline.request_stop()
            code, _ = await asyncio.gather(b.pipeline.run(), stop_soon())
            return code

        original, tg_sink.resolve_peer = tg_sink.resolve_peer, resolved
        try:
            self.assertEqual(run(scenario()), 0)
        finally:
            tg_sink.resolve_peer = original
        self.assertEqual(b.cfg.pairs[0].dest.handle, "peer-resolved")  # destinations resolved first
        self.assertIn("isn't linked", b.source.halted)                  # no WhatsApp session file
        self.assertIsNone(b.source.wa)


class HelperTests(unittest.TestCase):
    def test_media_filenames(self):
        from src.adapters.whatsapp.convert import Media
        f = source_mod.media_filename
        self.assertEqual(f(Media(kind="document", field="documentMessage", mimetype="application/pdf",
                                 filename="../../etc/x")), "x.pdf")
        self.assertEqual(f(Media(kind="document", field="documentMessage")), "file")
        self.assertEqual(f(Media(kind="photo", field="imageMessage", mimetype="image/png")), "photo.png")
        self.assertEqual(f(Media(kind="voice", field="audioMessage",
                                 mimetype="audio/ogg; codecs=opus")), "voice.ogg")


if __name__ == "__main__":
    unittest.main()
