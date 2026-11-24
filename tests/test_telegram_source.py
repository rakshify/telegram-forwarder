"""The Telegram source with a fake user client: catch-up, live messages, routing, downloads."""
import json
import tempfile
import unittest
from pathlib import Path

from src import config, state, users
from src.adapters.telegram import source as source_mod
from src.adapters.telegram.source import Chat, TelegramSource
from src.core.model import TELEGRAM, Dest, Pair
from src.core.pipeline import Pipeline
from src.core.source import MediaUnavailable
from src.core.store import Store
from tests import telegram_fixtures as fx
from tests.telegram_fixtures import Event, FakeUserClient, run

WHOLE = Chat(fx.CHAT, 1)
TOPIC = Chat(fx.CHAT, 1, topic=17)
_ids = iter(range(1, 10 ** 6))


def make(*chats, history=(), short_id=None):
    """(source, pipeline) with a fake user client already connected."""
    chats = chats or (WHOLE,)
    short_id = short_id or f"s{next(_ids):05d}"
    user = users.UserRecord(short_id=short_id, user_id=7, first_name="Me", last_name=None,
                            username=None, phone=None)
    source = TelegramSource(user, list(chats))
    pairs = [Pair(c.key, Dest(TELEGRAM, "999", hash=1)) for c in chats]
    pipeline = Pipeline(source=source, pairs=pairs, sinks={}, store=Store(":memory:"), title="test")
    source.client = FakeUserClient(history)
    source.client.connected = True
    source.peers = {c.chat_id: f"peer{c.chat_id}" for c in chats}
    return source, pipeline


def queued(pipeline):
    """[(chat key, message id)] in queue order."""
    return [(r[0], r[1]) for r in pipeline.store.db.execute("SELECT source, msg_id FROM inbox ORDER BY seq")]


class CatchUpTests(unittest.TestCase):
    def test_first_run_starts_at_the_latest_message(self):
        source, p = make(history=[fx.message(i, f"m{i}") for i in (1, 2, 3)])
        run(source._catch_up(WHOLE))
        self.assertEqual(queued(p), [])                  # no backfill
        self.assertEqual(p.store.cursor(WHOLE.key), 3)

    def test_missed_messages_are_queued_oldest_first(self):
        source, p = make(history=[fx.message(i, f"m{i}") for i in (1, 2, 3, 4)])
        p.store.set_cursor(WHOLE.key, 2)
        run(source._catch_up(WHOLE))
        self.assertEqual(queued(p), [("1001", "3"), ("1001", "4")])
        self.assertEqual(p.store.cursor(WHOLE.key), 4)

    def test_topic_catch_up_only_takes_its_topic(self):
        history = [fx.message(1, "a", topic=17), fx.message(2, "b", topic=18), fx.message(3, "c", topic=17)]
        source, p = make(TOPIC, history=history)
        p.store.set_cursor(TOPIC.key, 0)
        p.store.set_cursor(TOPIC.key, 1)
        run(source._catch_up(TOPIC))
        self.assertEqual(queued(p), [("1001#17", "3")])

    def test_old_forward_state_is_carried_over(self):
        source, p = make(WHOLE, TOPIC, history=[fx.message(i, "x", topic=17 if i % 2 else 0)
                                                for i in range(1, 10)])
        sid = source.user.short_id
        state.save_state(sid, {f"{sid}:1001:999": 5, f"{sid}:1001:888": 6,  # two pairs, same source
                               f"{sid}:1001:999:17": 3, f"{sid}:2002:999": 99})
        run(source._catch_up(WHOLE))
        run(source._catch_up(TOPIC))
        self.assertEqual(p.store.cursor(WHOLE.key), 9)
        self.assertEqual([m for c, m in queued(p) if c == "1001"], ["7", "8", "9"])  # after max(5, 6)
        self.assertEqual([m for c, m in queued(p) if c == "1001#17"], ["5", "7", "9"])

    def test_old_baseline_of_zero_means_start_fresh(self):
        source, p = make(history=[fx.message(i, "x") for i in (1, 2)])
        sid = source.user.short_id
        state.save_state(sid, {f"{sid}:1001:999": 0})  # the old forwarder saw an empty chat
        run(source._catch_up(WHOLE))
        self.assertEqual(queued(p), [])                 # not a full-history backfill
        self.assertEqual(p.store.cursor(WHOLE.key), 2)


class LiveTests(unittest.TestCase):
    def test_messages_go_to_every_matching_chat(self):
        source, p = make(WHOLE, TOPIC)
        source._catching_up = False
        run(source._on_new_message(Event(fx.message(5, "in the topic", topic=17))))
        run(source._on_new_message(Event(fx.message(6, "elsewhere", topic=18))))
        run(source._on_new_message(Event(fx.message(7, "x", chat=4242))))  # not a source chat
        self.assertEqual(queued(p), [("1001", "5"), ("1001#17", "5"), ("1001", "6")])

    def test_a_message_is_queued_once(self):
        source, p = make()
        source._catching_up = False
        for _ in range(2):
            run(source._on_new_message(Event(fx.message(5, "x"))))
        self.assertEqual(queued(p), [("1001", "5")])

    def test_messages_during_catch_up_wait_and_keep_their_order(self):
        """The live handler is on before catch-up; what it sees meanwhile is held, then queued."""
        history = [fx.message(i, f"m{i}") for i in (1, 2, 3)]

        class Client(FakeUserClient):
            async def iter_messages(inner, peer, min_id=0, reply_to=None):
                # Telegram delivers #3 (also in history) and a new #4 while catch-up runs.
                await source._on_new_message(Event(fx.message(3, "m3")))
                await source._on_new_message(Event(fx.message(4, "m4")))
                async for m in FakeUserClient.iter_messages(inner, peer, min_id, reply_to):
                    yield m

        source, p = make()
        client = Client(history)
        p.store.set_cursor(WHOLE.key, 1)
        original, config.make_client = config.make_client, lambda base: client
        try:
            run(source.start())
        finally:
            config.make_client = original
        self.assertEqual(queued(p), [("1001", "2"), ("1001", "3"), ("1001", "4")])
        self.assertEqual(len(client.handlers), 1)
        self.assertFalse(source._catching_up)

    def test_payload_round_trips(self):
        source, p = make()
        source._catching_up = False
        run(source._on_new_message(Event(fx.message(5, "hi", media=fx.photo()))))
        msg = source.decode(p.store.next_pending())
        self.assertEqual((msg.id, msg.kind, msg.sender_name, msg.body.text, msg.media.kind),
                         ("5", "media", "Alex Doe (@alex_doe)", "hi", "photo"))
        self.assertEqual(json.loads(p.store.next_pending().raw)["v"], 1)


class StartTests(unittest.TestCase):
    def start(self, source, client):
        original, config.make_client = config.make_client, lambda base: client
        try:
            run(source.start())
        finally:
            config.make_client = original

    def test_logged_out_session_halts_instead_of_crash_looping(self):
        source, _ = make()
        source.client = None
        self.start(source, FakeUserClient(authorized=False))
        self.assertIn("isn't logged in", source.halted)
        self.assertIsNone(source.client)

    def test_a_chat_that_cant_be_resolved_doesnt_stop_the_others(self):
        basic = Chat(2002, 0)  # basic groups need get_entity, which this fake doesn't have
        source, p = make(WHOLE, basic)
        source.peers = {}
        client = FakeUserClient([fx.message(1, "x")])
        self.start(source, client)
        self.assertEqual(set(source.peers), {fx.CHAT})
        self.assertEqual(p.store.cursor(WHOLE.key), 1)
        self.assertIsNone(p.store.cursor(basic.key))

    def test_losing_the_session_for_good_restarts_the_process(self):
        source, p = make()
        self.start(source, FakeUserClient())
        (callback,) = source.client.disconnected.callbacks
        callback(None)
        self.assertTrue(p.stopping)
        self.assertEqual(p.exit_code, 1)


class DownloadTests(unittest.TestCase):
    def test_expired_file_reference_fetches_the_message_again(self):
        original = fx.message(5, "", media=fx.photo())
        source, p = make(history=[original])
        source._catching_up = False
        run(source._on_new_message(Event(original)))
        msg = source.decode(p.store.next_pending())
        source.client.expire_next_download = True
        with tempfile.TemporaryDirectory() as tmp:
            path = run(source.download(msg, Path(tmp)))
            self.assertEqual(path.read_bytes(), b"media-bytes")
        self.assertEqual(len(source.client.downloads), 2)

    def test_message_gone_means_media_unavailable(self):
        source, p = make(history=[])
        source._catching_up = False
        run(source._on_new_message(Event(fx.message(5, "", media=fx.photo()))))
        msg = source.decode(p.store.next_pending())
        source.client.expire_next_download = True
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(MediaUnavailable):
            run(source.download(msg, Path(tmp)))


if __name__ == "__main__":
    unittest.main()
