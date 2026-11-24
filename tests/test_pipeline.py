"""The pipeline on its own, with a fake source and fake sinks on made-up platforms:
nothing here knows about WhatsApp, Telegram or Signal."""
import asyncio
import json
import unittest

from src.core import rich as fmt
from src.core.model import Dest, Media, Message, Pair, Quote, Skip
from src.core.pipeline import Pipeline
from src.core.sink import Unreachable
from src.core.source import MediaUnavailable, NotConnected
from src.core.store import Store


class FakeSource:
    """Payloads are JSON dicts; decode() turns them into Messages."""
    name = "Fake"

    def __init__(self):
        self.connected = True
        self.media_gone = False
        self.downloads = 0
        self.host = None

    def attach(self, host):
        self.host = host

    async def start(self):
        pass

    async def stop(self):
        pass

    async def check(self):
        pass

    def label(self, chat):
        return f"chat {chat}"

    def push(self, payload, chat="S"):
        return self.host.enqueue(chat, payload["id"], json.dumps(payload).encode())

    def decode(self, item):
        d = json.loads(item.raw)
        if d.get("broken"):
            raise ValueError("unreadable payload")
        if d.get("skip"):
            return Skip(d["skip"])
        media = Media(kind="photo", mimetype="image/jpeg") if d.get("photo") else None
        reply = None
        if d.get("reply_to"):
            reply = Quote(d["reply_to"], d.get("reply_sender", ""), fmt.plain(d.get("reply_preview", "")))
        kind = "edit" if d.get("edit_of") else "media" if media else "text"
        return Message(id=d["id"], kind=kind, label=kind, sender_name=d.get("sender", ""),
                       body=fmt.plain(d.get("text", "")), media=media, reply=reply,
                       edit_of=d.get("edit_of", ""))

    async def download(self, msg, folder):
        if not self.connected:
            raise NotConnected()
        if self.media_gone:
            raise MediaUnavailable("expired")
        self.downloads += 1
        path = folder / "photo.jpg"
        path.write_bytes(b"jpeg")
        return path


class FakeSink:
    name = "Fake"

    def __init__(self):
        self.sent = []   # (kind, chat, text, reply_to)
        self.edits = []  # (chat, msg_id, text)
        self.fail = []   # exceptions for the next sends, in order
        self._id = 0

    async def start(self):
        pass

    async def stop(self):
        pass

    async def resolve(self, dest):
        return dest.chat

    def _posted(self, *record):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append(record)
        self._id += 1
        return self._id

    async def send_text(self, dest, text, reply_to):
        return self._posted("text", dest.chat, text.text, reply_to)

    async def send_media(self, dest, media, caption, path, reply_to):
        return self._posted("media", dest.chat, caption.text, reply_to, path.read_bytes())

    async def send_poll(self, dest, poll, reply_to):
        return None

    async def edit(self, dest, msg_id, text, is_media):
        self.edits.append((dest.chat, msg_id, text.text))
        return True


class FakeNotifier:
    ready = True

    def __init__(self):
        self.sent = []

    async def notify(self, user_id, text):
        self.sent.append((user_id, text))


def make(*dests, dry_run=False, alerts=None, notifier=None):
    """(pipeline, source, sinks by platform). dests: (platform, chat) pairs."""
    source = FakeSource()
    dests = dests or (("fake", "A"),)
    sinks = {platform: FakeSink() for platform, _ in dests}
    pairs = [Pair("S", Dest(platform, chat), attribution=True) for platform, chat in dests]
    for p in pairs:
        p.dest.handle = p.dest.chat  # as if resolved at startup
    pipeline = Pipeline(source=source, pairs=pairs, sinks=sinks, store=Store(":memory:"),
                        title="Test bridge", alerts=alerts, notifier=notifier, dry_run=dry_run)

    async def no_sleep(_seconds):
        return None
    pipeline.sleep = no_sleep
    return pipeline, source, sinks


async def drain(pipeline):
    while True:
        item = pipeline.store.next_pending()
        if item is None:
            return
        await pipeline._handle(item)
        after = pipeline.store.next_pending()
        if after is not None and after.seq == item.seq and after.attempts == item.attempts:
            return  # waiting for the source; don't spin


def run(coro):
    return asyncio.run(coro)


def status(pipeline):
    return pipeline.store.db.execute("SELECT status FROM inbox ORDER BY seq DESC").fetchone()[0]


class PostingTests(unittest.TestCase):
    def test_every_pair_gets_it_with_attribution(self):
        p, source, sinks = make(("fake", "A"), ("fake", "B"))
        source.push({"id": "m1", "sender": "Rahul", "text": "hi"})
        run(drain(p))
        self.assertEqual(sinks["fake"].sent, [("text", "A", "Rahul:\n\nhi", None),
                                              ("text", "B", "Rahul:\n\nhi", None)])
        self.assertEqual([p.store.posted(pair.key, "m1") for pair in p.pairs], [(1, False), (2, False)])
        self.assertEqual(status(p), "done")

    def test_reply_threads_to_the_posted_copy(self):
        p, source, sinks = make()
        source.push({"id": "m1", "text": "first"})
        source.push({"id": "m2", "text": "second", "reply_to": "m1", "reply_preview": "first"})
        run(drain(p))
        self.assertEqual(sinks["fake"].sent[1][3], 1)

    def test_reply_to_a_message_never_posted_gets_a_quote_block(self):
        p, source, sinks = make()
        source.push({"id": "m2", "sender": "Rahul", "text": "agree", "reply_to": "old",
                     "reply_sender": "Priya", "reply_preview": "target 120"})
        run(drain(p))
        self.assertEqual(sinks["fake"].sent[0][2:], ("Rahul:\n\nPriya: target 120\nagree", None))

    def test_edit_goes_to_the_copy(self):
        p, source, sinks = make()
        source.push({"id": "m1", "sender": "Rahul", "text": "target 100"})
        source.push({"id": "e1", "sender": "Rahul", "text": "target 110", "edit_of": "m1"})
        run(drain(p))
        self.assertEqual(sinks["fake"].edits, [("A", 1, "Rahul:\n\ntarget 110")])

    def test_media_is_downloaded_once_for_all_pairs(self):
        p, source, sinks = make(("fake", "A"), ("other", "B"))
        source.push({"id": "m1", "photo": True, "sender": "Rahul", "text": "chart"})
        run(drain(p))
        self.assertEqual(source.downloads, 1)
        for platform, chat in (("fake", "A"), ("other", "B")):
            self.assertEqual(sinks[platform].sent, [("media", chat, "Rahul:\n\nchart", None, b"jpeg")])
        self.assertEqual(p.store.posted(p.pairs[0].key, "m1"), (1, True))

    def test_media_the_source_lost_becomes_a_note(self):
        p, source, sinks = make()
        source.media_gone = True
        source.push({"id": "m1", "photo": True, "text": "chart"})
        run(drain(p))
        ((kind, _, text, _),) = sinks["fake"].sent
        self.assertEqual(kind, "text")
        self.assertIn("couldn't be downloaded from Fake", text)

    def test_skips_and_dry_runs_post_nothing(self):
        p, source, sinks = make()
        source.push({"id": "s1", "skip": "nothing to post"})
        run(drain(p))
        self.assertEqual((sinks["fake"].sent, status(p)), ([], "done"))
        p, source, sinks = make(dry_run=True)
        source.push({"id": "m1", "photo": True, "text": "x"})
        run(drain(p))
        self.assertEqual((sinks["fake"].sent, source.downloads), ([], 0))


class TroubleTests(unittest.TestCase):
    def test_waits_while_the_source_is_disconnected(self):
        p, source, sinks = make()
        source.connected = False

        async def stop_instead_of_sleeping(_seconds):
            p.request_stop()
        p.sleep = stop_instead_of_sleeping
        source.push({"id": "m1", "photo": True})
        run(drain(p))
        self.assertEqual(sinks["fake"].sent, [])
        self.assertEqual(p.store.pending_count(), 1)

    def test_unreadable_payload_is_given_up_and_the_queue_moves_on(self):
        p, source, sinks = make()
        source.push({"id": "bad", "broken": True})
        source.push({"id": "m2", "text": "next"})
        run(drain(p))
        self.assertEqual([s[2] for s in sinks["fake"].sent], ["Unknown:\n\nnext"])  # no sender known
        bad = p.store.db.execute("SELECT status, last_error FROM inbox WHERE msg_id='bad'").fetchone()
        self.assertEqual(bad[0], "failed")
        self.assertIn("unreadable payload", bad[1])

    def test_unreachable_destination_is_paused_and_the_others_go_on(self):
        p, source, sinks = make(("fake", "A"), ("other", "B"))
        sinks["fake"].fail = [Unreachable("service down", "Fake")]
        source.push({"id": "m1", "text": "hi"})
        run(drain(p))
        self.assertIsNone(p.pairs[0].dest.handle)
        self.assertEqual([s[1] for s in sinks["other"].sent], ["B"])
        self.assertEqual(status(p), "done")

    def test_failed_destination_is_retried_without_reposting_the_others(self):
        p, source, sinks = make(("fake", "A"), ("other", "B"))
        sinks["fake"].fail = [ConnectionError("blip")]
        source.push({"id": "m1", "text": "hi"})
        run(drain(p))
        self.assertEqual([s[1] for s in sinks["fake"].sent], ["A"])   # on the retry
        self.assertEqual([s[1] for s in sinks["other"].sent], ["B"])  # once
        self.assertEqual(status(p), "done")

    def test_alerts_go_through_the_notifier_at_most_hourly(self):
        notifier = FakeNotifier()
        p, _, _ = make(alerts=42, notifier=notifier)
        run(p.alert("k", "something broke"))
        run(p.alert("k", "something broke"))
        self.assertEqual(notifier.sent, [(42, "⚠️ Test bridge: something broke")])


if __name__ == "__main__":
    unittest.main()
