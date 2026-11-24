"""The on-disk queue, message map and sender names (src/core/store.py)."""
import tempfile
import time
import unittest

from src.core.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def test_queue_order_and_dedupe(self):
        self.assertTrue(self.s.enqueue("g", "A", b"1"))
        self.assertTrue(self.s.enqueue("g", "B", b"2"))
        self.assertFalse(self.s.enqueue("g", "A", b"1 again"))
        self.assertEqual(self.s.pending_count(), 2)
        first = self.s.next_pending()
        self.assertEqual((first.msg_id, first.raw), ("A", b"1"))
        self.s.mark_done(first.seq)
        self.assertEqual(self.s.next_pending().msg_id, "B")

    def test_same_id_with_different_payloads_is_kept(self):
        self.assertTrue(self.s.enqueue("g", "A", b"handover", "d1"))
        self.assertTrue(self.s.enqueue("g", "A", b"content", "d2"))
        self.assertFalse(self.s.enqueue("g", "A", b"content", "d2"))
        self.assertEqual(self.s.pending_count(), 2)

    def test_migrates_first_release_queue(self):
        import sqlite3
        path = tempfile.mkdtemp() + "/state.sqlite"
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE inbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, msg_id TEXT NOT NULL,
                raw BLOB NOT NULL, received_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, UNIQUE (source, msg_id));
            CREATE INDEX inbox_by_status ON inbox (status, seq);
            INSERT INTO inbox (source, msg_id, raw, received_at, status) VALUES ('g', 'A', x'00', 1, 'done');
            INSERT INTO inbox (source, msg_id, raw, received_at) VALUES ('g', 'B', x'01', 2);
        """)
        old.close()
        s = Store(path)
        self.assertEqual(s.next_pending().msg_id, "B")
        self.assertTrue(s.enqueue("g", "A", b"content", "d2"))  # old key no longer blocks it
        self.assertEqual(s.next_pending().msg_id, "B")
        s.close()
        Store(path).close()  # second open: nothing left to migrate

    def test_attempts_and_failure(self):
        self.s.enqueue("g", "A", b"1")
        item = self.s.next_pending()
        self.assertEqual(self.s.record_attempt(item.seq, "boom"), 1)
        self.assertEqual(self.s.record_attempt(item.seq, "boom"), 2)
        self.s.mark_failed(item.seq, "gave up")
        self.assertIsNone(self.s.next_pending())

    def test_message_map_and_names(self):
        self.assertIsNone(self.s.posted("p", "A"))
        self.s.record_posted("p", "A", 42, is_media=True)
        self.assertEqual(self.s.posted("p", "A"), (42, True))
        self.assertIsNone(self.s.posted("other", "A"))
        self.s.remember_name("1@lid", "Rahul")
        self.s.remember_name("1@lid", "Rahul K")
        self.s.remember_name("", "x")
        self.assertEqual(self.s.name_for("1@lid"), "Rahul K")

    def test_prune_keeps_pending(self):
        self.s.enqueue("g", "old-done", b"")
        self.s.enqueue("g", "old-pending", b"")
        self.s.mark_done(self.s.next_pending().seq)
        self.s.db.execute("UPDATE inbox SET received_at = ?", (time.time() - 30 * 86400,))
        self.s.prune()
        rows = [r[0] for r in self.s.db.execute("SELECT msg_id FROM inbox")]
        self.assertEqual(rows, ["old-pending"])


if __name__ == "__main__":
    unittest.main()
