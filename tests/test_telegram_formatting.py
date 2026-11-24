"""Telegram entities <-> Rich (src/adapters/telegram/formatting.py): nothing is lost."""
import copy
import datetime
import unittest

from telethon.tl import types as T

from src.adapters.telegram.formatting import to_entities, to_rich
from src.core import rich as fmt


def view(entities):
    """Comparable form: each entity's TL bytes (type, offsets and every field)."""
    return sorted(bytes(e) for e in entities)


def at(text, word):
    """UTF-16 (offset, length) of `word` in `text`, the way Telegram counts."""
    start = text.index(word)
    units = lambda s: len(s.encode("utf-16-le")) // 2
    return units(text[:start]), units(word)


TEXT = ("🚀 bold ital under gone hide code pre quote link url me@x.co +15551234 @bob Bob "
        "#tag $TCS /start 4111111111111111 ✨ friday ton")


def every_entity():
    def e(cls, word, **kw):
        return cls(*at(TEXT, word), **kw)
    return [
        e(T.MessageEntityBold, "bold"), e(T.MessageEntityItalic, "ital"),
        e(T.MessageEntityUnderline, "under"), e(T.MessageEntityStrike, "gone"),
        e(T.MessageEntitySpoiler, "hide"), e(T.MessageEntityCode, "code"),
        e(T.MessageEntityPre, "pre", language="python"),
        e(T.MessageEntityBlockquote, "quote", collapsed=True),
        e(T.MessageEntityTextUrl, "link", url="https://example.com/a"),
        e(T.MessageEntityUrl, "url"), e(T.MessageEntityEmail, "me@x.co"),
        e(T.MessageEntityPhone, "+15551234"), e(T.MessageEntityMention, "@bob"),
        e(T.MessageEntityMentionName, "Bob", user_id=4242),
        e(T.MessageEntityHashtag, "#tag"), e(T.MessageEntityCashtag, "$TCS"),
        e(T.MessageEntityBotCommand, "/start"), e(T.MessageEntityBankCard, "4111111111111111"),
        e(T.MessageEntityCustomEmoji, "✨", document_id=5368324170671202286),
        # Newer types this module has no mapping for: kept as they are.
        e(T.MessageEntityFormattedDate, "friday", relative=True,
          date=datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)),
        e(T.MessageEntityTonAddress, "ton"),
    ]


class RoundTripTests(unittest.TestCase):
    def test_every_entity_type_comes_back_unchanged(self):
        entities = every_entity()
        self.assertEqual(view(to_entities(to_rich(TEXT, entities))), view(entities))

    def test_spans_carry_what_entities_carry(self):
        rich = to_rich(TEXT, every_entity())
        data = {s.kind: s.data for s in rich.spans}
        self.assertEqual(data[fmt.TEXT_URL], "https://example.com/a")
        self.assertEqual(data[fmt.MENTION_NAME], "4242")
        self.assertEqual(data[fmt.PRE], "python")
        self.assertEqual(data[fmt.QUOTE], "collapsed")
        self.assertEqual(rich.text[[s for s in rich.spans if s.kind == fmt.BOLD][0].start:][:4], "bold")

    def test_offsets_follow_the_text_when_a_prefix_is_added(self):
        """Attribution puts the sender's name in front; every entity has to move with the text."""
        entities = every_entity()
        prefixed = fmt.concat(fmt.styled("Rahul 🦁", fmt.BOLD), fmt.plain(":\n\n"), to_rich(TEXT, entities))
        shift = len("Rahul 🦁:\n\n".encode("utf-16-le")) // 2
        out = to_entities(prefixed)
        name = [e for e in out if isinstance(e, T.MessageEntityBold) and e.offset == 0]
        self.assertEqual([(e.offset, e.length) for e in name], [(0, 8)])
        expected = []
        for e in entities:
            moved = copy.copy(e)
            moved.offset += shift
            expected.append(moved)
        self.assertEqual(view([e for e in out if e not in name]), view(expected))

    def test_side_by_side_custom_emoji_stay_separate(self):
        ents = [T.MessageEntityCustomEmoji(0, 1, document_id=7), T.MessageEntityCustomEmoji(1, 1, document_id=7)]
        self.assertEqual(len(to_entities(to_rich("✨✨", ents))), 2)

    def test_unknown_and_input_only_entities_are_dropped(self):
        ents = [T.MessageEntityUnknown(0, 2), T.InputMessageEntityMentionName(0, 2, user_id=T.InputUserSelf())]
        self.assertEqual(to_rich("hi", ents).spans, [])

    def test_no_entities(self):
        self.assertEqual(to_rich(None, None), fmt.Rich("", []))
        self.assertEqual(to_entities(fmt.plain("x")), [])


class FromOtherSourcesTests(unittest.TestCase):
    def test_whatsapp_style_spans_make_the_same_entities_as_before(self):
        r = fmt.Rich("q\ncode", [fmt.Span(fmt.QUOTE, 0, 1), fmt.Span(fmt.PRE, 2, 6)])
        self.assertEqual(view(to_entities(r)), view([T.MessageEntityBlockquote(0, 1),
                                                     T.MessageEntityPre(2, 4, language="")]))

    def test_another_platforms_raw_spans_are_ignored(self):
        r = fmt.Rich("ab", [fmt.Span(fmt.RAW, 0, 2, "signal:whatever")])
        self.assertEqual(to_entities(r), [])


if __name__ == "__main__":
    unittest.main()
