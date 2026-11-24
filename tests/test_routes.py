"""One config format for every source (src/adapters/routes.py)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.adapters import routes
from src.adapters.telegram import forward as tg_forward
from src.adapters.whatsapp import bridge as wa_bridge
from src.core.model import SIGNAL, TELEGRAM, WHATSAPP, Routes

TG_SOURCE = {"source": "-1004200000003", "source_hash": 11}
TG_DEST = {"dest": "-1005555555555", "dest_hash": 0}
SIGNAL_DEST = {"dest_platform": "signal", "dest": "group.QUJD="}


def write(data) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return Path(f.name)


def load(data, **kw) -> Routes:
    return asyncio.run(routes.load(write(data), **kw))


def keys(cfg):
    return [p.key for p in cfg.pairs]


class FormatTests(unittest.TestCase):
    def test_telegram_source(self):
        cfg = load({"platform": "telegram", "account": "abc123", "attribution": True, "alerts": 42,
                    "pairs": [{**TG_SOURCE, "topic": 17, "dest": "-1006666666666", "dest_hash": 22,
                               "dest_topic": 5},
                              {**TG_SOURCE, "dests": [TG_DEST, {**SIGNAL_DEST, "attribution": False}]}]})
        self.assertEqual((cfg.platform, cfg.account, cfg.alerts), (TELEGRAM, "abc123", 42))
        self.assertEqual(keys(cfg), ["4200000003#17>6666666666:5", "4200000003>5555555555:0",
                                     "4200000003>signal:group.QUJD="])
        self.assertEqual([p.attribution for p in cfg.pairs], [True, True, False])
        self.assertEqual(list(cfg.sources), ["4200000003#17", "4200000003"])
        self.assertEqual(cfg.sources["4200000003#17"].topic, 17)

    def test_whatsapp_source(self):
        cfg = load({"platform": "whatsapp", "account": "wa-a1b2c3",
                    "pairs": [{"source": "120363012345678901@g.us", "dests": [TG_DEST, SIGNAL_DEST]},
                              {"source": "+91 98765 43210", "dest": "-1007777777777", "dest_hash": 1}]})
        self.assertEqual((cfg.platform, cfg.account), (WHATSAPP, "a1b2c3"))
        self.assertEqual(keys(cfg), ["120363012345678901@g.us>5555555555:0",
                                     "120363012345678901@g.us>signal:group.QUJD=",
                                     "919876543210@s.whatsapp.net>7777777777:0"])
        self.assertEqual(list(cfg.sources), ["120363012345678901@g.us", "919876543210@s.whatsapp.net"])

    def test_dests_is_shorthand_for_one_pair_per_destination(self):
        head = {"platform": "telegram", "account": "abc123"}
        apart = load({**head, "pairs": [{**TG_SOURCE, **TG_DEST}, {**TG_SOURCE, **SIGNAL_DEST}]})
        together = load({**head, "pairs": [{**TG_SOURCE, "dests": [TG_DEST, SIGNAL_DEST]}]})
        self.assertEqual(keys(apart), keys(together))
        self.assertEqual([p.dest for p in apart.pairs], [p.dest for p in together.pairs])

    def test_attribution_goes_file_then_pair_then_destination(self):
        cfg = load({"platform": "telegram", "account": "abc123", "attribution": True, "pairs": [
            {**TG_SOURCE, **TG_DEST},                                            # file: on
            {**TG_SOURCE, "topic": 1, **TG_DEST, "attribution": False},          # pair: off
            {**TG_SOURCE, "topic": 2, "attribution": False,
             "dests": [TG_DEST, {**SIGNAL_DEST, "attribution": True}]},          # pair off, one dest on
        ]})
        self.assertEqual([p.attribution for p in cfg.pairs], [True, False, False, True])

    def test_comments_are_ignored(self):
        cfg = load({"_comment": "x", "platform": "telegram", "account": "abc123",
                    "pairs": [{"_comment": "y", **TG_SOURCE, "dests": [{"_comment": "z", **TG_DEST}]}]})
        self.assertEqual(len(cfg.pairs), 1)


class OldFilesTests(unittest.TestCase):
    """Configs written before the one format load as they always did."""

    def test_telegram_file_with_user(self):
        cfg = load({"user": "abc123", "pairs": [{**TG_SOURCE, **TG_DEST}]})
        self.assertEqual((cfg.platform, cfg.account), (TELEGRAM, "abc123"))

    def test_telegram_file_relying_on_minus_u(self):
        cfg = load({"pairs": [{**TG_SOURCE, **TG_DEST}]}, account="cli123")
        self.assertEqual((cfg.platform, cfg.account), (TELEGRAM, "cli123"))

    def test_whatsapp_file(self):
        cfg = load({"account": "a1b2c3", "pairs": [{"source": "1@g.us", **TG_DEST}]})
        self.assertEqual((cfg.platform, cfg.account), (WHATSAPP, "a1b2c3"))

    def test_whatsapp_file_relying_on_minus_a(self):
        cfg = load({"pairs": [{"source": "+919876543210", **TG_DEST}]}, account="wa-a1b2c3")
        self.assertEqual((cfg.platform, cfg.account), (WHATSAPP, "a1b2c3"))

    def test_account_without_platform_is_told_apart_by_its_sources(self):
        self.assertEqual(load({"account": "abc123", "pairs": [{**TG_SOURCE, **TG_DEST}]}).platform, TELEGRAM)
        self.assertEqual(load({"account": "abc123", "pairs": [{"source": "1@g.us", **TG_DEST}]}).platform,
                         WHATSAPP)

    def test_wa_forward_means_whatsapp(self):
        cfg = load({"account": "a1b2c3", "pairs": [{"source": "1@g.us", **TG_DEST}]}, platform=WHATSAPP)
        self.assertEqual(cfg.platform, WHATSAPP)


class ErrorTests(unittest.TestCase):
    def fails(self, data, *, says="", **kw):
        with self.assertRaises(SystemExit) as cm:
            load(data, **kw)
        self.assertIn(says, str(cm.exception))

    def test_errors(self):
        tg = {"platform": "telegram", "account": "abc123"}
        self.fails({**tg, "pairs": [{**TG_SOURCE, **TG_DEST}]}, platform=WHATSAPP, says="is for telegram")
        self.fails({"platform": "matrix", "account": "a", "pairs": []}, says="platform 'matrix'")
        self.fails({"user": "a", "account": "b", "pairs": [{**TG_SOURCE, **TG_DEST}]}, says="different accounts")
        self.fails({"platform": "telegram", "pairs": [{**TG_SOURCE, **TG_DEST}]}, says="no -u given")
        self.fails({**tg, "pairs": [{**TG_SOURCE, **TG_DEST, "dests": [TG_DEST]}]}, says="not both")
        self.fails({**tg, "pairs": [{**TG_SOURCE, "dests": []}]}, says="one or more")
        self.fails({**tg, "pairs": [{**TG_SOURCE, "dests": ["-1005555555555"]}]}, says="pairs[0].dests[0]")
        self.fails({**tg, "pairs": [{**TG_SOURCE, "dests": [TG_DEST, {"dest_platform": "signal", "dest": "12"}]}]},
                   says="pairs[0].dests[1]")
        self.fails({**tg, "pairs": [{"source": "-1001"}]}, says="missing key 'source_hash'")
        self.fails({"platform": "whatsapp", "account": "a", "pairs": [{"source": "1@g.us", **TG_DEST}],
                    "auto": [{"source": "x"}]}, says="Telegram sources only")
        self.fails({**tg, "pairs": []}, says="no pairs defined")

    def test_not_a_config(self):
        with self.assertRaises(SystemExit):
            asyncio.run(routes.load(write(["not", "an", "object"])))


class RunTests(unittest.TestCase):
    def test_run_starts_the_sources_forwarder(self):
        calls = []

        def fake(platform):
            async def run(cfg, dry_run=False):
                calls.append((platform, cfg.account, dry_run))
                return 0
            return run

        originals = tg_forward.run, wa_bridge.run
        tg_forward.run, wa_bridge.run = fake(TELEGRAM), fake(WHATSAPP)
        try:
            asyncio.run(routes.run(Routes(WHATSAPP, "a1b2c3"), dry_run=True))
            asyncio.run(routes.run(Routes(TELEGRAM, "abc123")))
        finally:
            tg_forward.run, wa_bridge.run = originals
        self.assertEqual(calls, [(WHATSAPP, "a1b2c3", True), (TELEGRAM, "abc123", False)])


if __name__ == "__main__":
    unittest.main()
