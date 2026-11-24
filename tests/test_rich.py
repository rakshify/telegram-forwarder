"""The platform-neutral Rich text toolkit (src/core/rich.py)."""
import unittest

from src.core import rich as fmt


def spans(rich):
    return sorted((s.kind, rich.text[s.start:s.end]) for s in rich.spans)


class ToolkitTests(unittest.TestCase):
    def test_strip_moves_spans(self):
        r = fmt.strip(fmt.Rich("  hi  ", [fmt.Span(fmt.BOLD, 2, 4)]))
        self.assertEqual((r.text, spans(r)), ("hi", [("bold", "hi")]))
        self.assertEqual(fmt.strip(fmt.plain(" \n ")).text, "")

    def test_concat_shifts_and_merges_spans(self):
        r = fmt.concat(fmt.styled("ab", fmt.BOLD), fmt.styled("cd", fmt.BOLD), fmt.plain(" e"))
        self.assertEqual((r.text, spans(r)), ("abcd e", [("bold", "abcd")]))

    def test_replace_ranges_keeps_spans_aligned(self):
        r = fmt.Rich("hey @123 there x", [fmt.Span(fmt.BOLD, 0, 14), fmt.Span(fmt.ITALIC, 15, 16)])
        start = r.text.index("@123")
        r2 = fmt.replace_ranges(r, [(start, start + 4, "@Priya Sharma")])
        self.assertEqual(r2.text, "hey @Priya Sharma there x")
        self.assertEqual(spans(r2), [("bold", "hey @Priya Sharma there"), ("italic", "x")])

    def test_truncate(self):
        self.assertEqual(fmt.truncate(fmt.plain("abcdef"), 4).text, "abc…")
        self.assertEqual(fmt.truncate(fmt.plain("abc"), 4).text, "abc")

    def test_split_fits_limits_and_prefers_newlines(self):
        text = "\n".join("line %02d " % i + "x" * 20 for i in range(20))
        chunks = fmt.split(fmt.plain(text), 100)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(fmt.utf16_len(c.text), 100)
            self.assertTrue(c.text.startswith("line "), c.text)
        self.assertEqual("\n".join(c.text for c in chunks), text)

    def test_split_first_and_rest_limits(self):
        chunks = fmt.split(fmt.plain("word " * 100), 50, 200)
        self.assertLessEqual(fmt.utf16_len(chunks[0].text), 50)
        self.assertTrue(all(fmt.utf16_len(c.text) <= 200 for c in chunks[1:]))

    def test_split_counts_emoji_as_two(self):
        chunks = fmt.split(fmt.plain("🚀" * 10), 7)
        self.assertEqual([len(c.text) for c in chunks], [3, 3, 3, 1])

    def test_split_keeps_formatting(self):
        r = fmt.Rich("b" * 50 + " " + "x " * 40, [fmt.Span(fmt.BOLD, 0, 50)])
        chunks = fmt.split(r, 60)
        self.assertEqual(spans(chunks[0]), [("bold", "b" * 50)])
        self.assertTrue(all(c.spans == [] for c in chunks[1:]))

    def test_utf16_offsets(self):
        self.assertEqual(fmt.utf16_offsets("a🚀b"), [0, 1, 3, 4])
        offsets = fmt.utf16_offsets("a🚀b")
        self.assertEqual([fmt.from_utf16(offsets, u) for u in range(5)], [0, 1, 1, 2, 3])


class SpanDataTests(unittest.TestCase):
    def test_data_survives_every_operation(self):
        link = fmt.Rich("see docs now", [fmt.Span(fmt.TEXT_URL, 4, 8, "https://x.com/d")])
        joined = fmt.concat(fmt.plain(">> "), link)
        cut = fmt.slice_rich(joined, 3, 11)
        replaced = fmt.replace_ranges(link, [(0, 3, "read")])
        for r in (joined, cut, replaced, fmt.strip(fmt.concat(fmt.plain("  "), link)),
                  fmt.split(joined, 100)[0]):
            (s,) = r.spans
            self.assertEqual((s.kind, s.data, r.text[s.start:s.end]), (fmt.TEXT_URL, "https://x.com/d", "docs"))

    def test_different_data_never_merges(self):
        r = fmt.concat(fmt.Rich("ab", [fmt.Span(fmt.TEXT_URL, 0, 2, "u1")]),
                       fmt.Rich("cd", [fmt.Span(fmt.TEXT_URL, 0, 2, "u2")]))
        self.assertEqual([s.data for s in r.spans], ["u1", "u2"])

    def test_atomic_spans_stay_apart(self):
        r = fmt.concat(fmt.Rich("✨", [fmt.Span(fmt.CUSTOM_EMOJI, 0, 1, "7")]),
                       fmt.Rich("✨", [fmt.Span(fmt.CUSTOM_EMOJI, 0, 1, "7")]))
        self.assertEqual(len(r.spans), 2)
        styles = fmt.concat(fmt.styled("ab", fmt.BOLD), fmt.styled("cd", fmt.BOLD))
        self.assertEqual(len(styles.spans), 1)  # plain styles still merge


if __name__ == "__main__":
    unittest.main()
