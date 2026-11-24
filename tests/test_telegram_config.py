"""configs/<short_id>.json and the `forward` flags (src/adapters/telegram/config_file.py)."""
import json
import tempfile
import unittest
from pathlib import Path

from src.adapters import routes
from src.adapters.telegram import config_file
from tests.telegram_fixtures import run


def load(data, fallback=None):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return run(routes.load(Path(f.name), account=fallback))


class LoadTests(unittest.TestCase):
    def test_pairs(self):
        cfg = load({
            "user": "abc123", "attribution": True, "alerts": "4242",
            "pairs": [
                {"source": "-1004200000003", "source_hash": 11, "dest": "-1005555555555", "dest_hash": 0},
                {"source": "-1004200000003", "source_hash": "11", "topic": 17,
                 "dest": "-1006666666666", "dest_hash": 22, "dest_topic": 5, "attribution": False},
                {"source": "-1004200000003", "source_hash": 11, "dest_platform": "signal",
                 "dest": "group.QUJD="},
            ],
        })
        self.assertEqual((cfg.platform, cfg.account, cfg.alerts), ("telegram", "abc123", 4242))
        a, b, c = cfg.pairs
        self.assertEqual((a.source, a.dest.chat, a.dest.hash, a.dest.topic, a.attribution),
                         ("4200000003", "5555555555", 0, 0, True))
        self.assertEqual((b.source, b.dest.topic, b.attribution), ("4200000003#17", 5, False))
        self.assertEqual((c.dest.platform, c.dest.chat), ("signal", "group.QUJD="))
        self.assertEqual([(ch.chat_id, ch.access_hash, ch.topic) for ch in cfg.sources.values()],
                         [(4200000003, 11, 0), (4200000003, 11, 17)])
        self.assertEqual(a.key, "4200000003>5555555555:0")

    def test_user_from_the_command_line(self):
        cfg = load({"pairs": [{"source": "1", "source_hash": 1, "dest": "2", "dest_hash": 1}]}, "cli123")
        self.assertEqual(cfg.account, "cli123")

    def test_duplicates_are_used_once(self):
        pair = {"source": "1", "source_hash": 1, "dest": "2", "dest_hash": 1}
        self.assertEqual(len(load({"user": "u", "pairs": [pair, pair]}).pairs), 1)

    def test_errors(self):
        for data in (
            {"pairs": [{"source": "1", "source_hash": 1, "dest": "2", "dest_hash": 1}]},  # no user
            {"user": "u", "pairs": []},
            {"user": "u", "pairs": [{"source": "1", "dest": "2", "dest_hash": 1}]},  # no source_hash
            {"user": "u", "pairs": [{"source": "1", "source_hash": 1, "dest_platform": "signal",
                                     "dest": "12345"}]},
            {"user": "u", "alerts": "me", "pairs": [{"source": "1", "source_hash": 1, "dest": "2",
                                                     "dest_hash": 1}]},
        ):
            with self.assertRaises(SystemExit, msg=str(data)):
                load(data)


class CliTests(unittest.TestCase):
    def test_flags(self):
        cfg = config_file.from_cli("u1", ["-1001", "-1002"], ["7", "0"], ["-1003", "-1004"], ["1", "1"],
                                   ["0", "17"], None, attribution=True)
        self.assertEqual([p.source for p in cfg.pairs], ["1", "2#17"])
        self.assertEqual([p.dest.chat for p in cfg.pairs], ["3", "4"])
        self.assertTrue(all(p.attribution for p in cfg.pairs))
        self.assertEqual(list(cfg.sources.values())[1].access_hash, 0)

    def test_length_mismatch(self):
        with self.assertRaises(SystemExit):
            config_file.from_cli("u1", ["-1001"], ["7", "8"], ["-1003"], ["1"], None, None, False)


if __name__ == "__main__":
    unittest.main()
