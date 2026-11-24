"""WhatsApp accounts registry and configs/wa-<short_id>.json."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.adapters import routes
from src.adapters.whatsapp import accounts, config_file


class AccountTests(unittest.TestCase):
    def test_phone_needs_country_code(self):
        self.assertEqual(accounts.normalize_phone("+91 98765-43210"), "919876543210")
        self.assertEqual(accounts.normalize_phone("0091 9876543210"), "919876543210")
        with self.assertRaises(SystemExit):
            accounts.normalize_phone("9876543210")
        with self.assertRaises(SystemExit):
            accounts.normalize_phone("+12")

    def test_short_id_is_stable(self):
        self.assertEqual(accounts.make_short_id("919876543210"), accounts.make_short_id("919876543210"))
        self.assertEqual(len(accounts.make_short_id("1")), 6)

    def test_registry_round_trip(self):
        a = accounts.WaAccount(short_id="abc123", phone="919876543210", push_name="Me")
        accounts.upsert(a)
        self.assertEqual(accounts.get_account("wa-abc123").push_name, "Me")
        accounts.remove("abc123")
        with self.assertRaises(SystemExit):
            accounts.get_account("abc123")


class ConfigTests(unittest.TestCase):
    def write(self, data) -> Path:
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        return Path(f.name)

    def load(self, data):
        """Through the one loader, platform not given: these are files in the old WhatsApp format."""
        cfg = asyncio.run(routes.load(self.write(data)))
        self.assertEqual(cfg.platform, "whatsapp")
        return cfg

    def test_sources(self):
        n = config_file.normalize_source
        self.assertEqual(n("120363012345678901@g.us"), "120363012345678901@g.us")
        self.assertEqual(n("+91 98765 43210"), "919876543210@s.whatsapp.net")
        self.assertEqual(n("123@LID"), "123@lid")
        for bad in ("120363@newsletter", "9876543210", "abc", "x@example.com"):
            with self.assertRaises(SystemExit, msg=bad):
                n(bad)

    def test_load(self):
        cfg = self.load({
            "account": "wa-abc123",
            "attribution": True,
            "alerts": "12345",
            "pairs": [
                {"source": "1@g.us", "dest": "-1004200000001", "dest_hash": 99},
                {"source": "2@g.us", "dest": "-4200000004", "dest_hash": 0, "dest_topic": 7,
                 "attribution": False, "_comment": "ignored"},
            ],
        })
        self.assertEqual((cfg.account, cfg.alerts), ("abc123", 12345))
        a, b = cfg.pairs
        self.assertEqual((a.dest.platform, a.dest.chat, a.dest.hash, a.dest.topic, a.attribution),
                         ("telegram", "4200000001", 99, 0, True))
        self.assertEqual((b.dest.chat, b.dest.topic, b.attribution), ("4200000004", 7, False))
        # Same keys as before Signal existed, so existing message maps keep matching.
        self.assertEqual((a.key, b.key), ("1@g.us>4200000001:0", "2@g.us>4200000004:7"))

    def test_signal_destinations(self):
        cfg = self.load({
            "account": "abc123",
            "pairs": [
                {"source": "1@g.us", "dest": "-1004200000001", "dest_hash": 99},
                {"source": "1@g.us", "dest_platform": "signal", "dest": "group.QUJD="},
                {"source": "2@g.us", "dest_platform": "Signal", "dest": "+1 (555) 000-0001"},
            ],
        })
        telegram, group, person = cfg.pairs
        self.assertEqual(telegram.dest.platform, "telegram")
        self.assertEqual((group.dest.platform, group.dest.chat), ("signal", "group.QUJD="))
        self.assertEqual(person.dest.chat, "+15550000001")
        self.assertEqual(group.key, "1@g.us>signal:group.QUJD=")
        self.assertEqual(group.label, "1@g.us -> signal:group.QUJD=")

    def test_signal_destination_errors(self):
        for pair in (
            {"source": "1@g.us", "dest_platform": "signal", "dest": "15550000001"},  # no +
            {"source": "1@g.us", "dest_platform": "signal", "dest": "group."},
            {"source": "1@g.us", "dest_platform": "signal", "dest": "group.QUJD=", "dest_topic": 3},
            {"source": "1@g.us", "dest_platform": "signal"},  # no dest
            {"source": "1@g.us", "dest_platform": "matrix", "dest": "!room:x.org"},
        ):
            with self.assertRaises(SystemExit, msg=str(pair)):
                self.load({"account": "a", "pairs": [pair]})

    def test_load_errors(self):
        for data in (
            {"pairs": [{"source": "1@g.us", "dest": "-1001", "dest_hash": 1}]},  # no account
            {"account": "a", "pairs": []},
            {"account": "a", "pairs": [{"source": "1@g.us", "dest": "-1001"}]},  # no dest_hash
            {"account": "a", "alerts": "me", "pairs": [{"source": "1@g.us", "dest": "1", "dest_hash": 1}]},
        ):
            with self.assertRaises(SystemExit, msg=str(data)):
                self.load(data)

    def test_a_duplicate_pair_is_used_once(self):
        pair = {"source": "1@g.us", "dest": "1", "dest_hash": 1}
        self.assertEqual(len(self.load({"account": "a", "pairs": [pair, pair]}).pairs), 1)


if __name__ == "__main__":
    unittest.main()
