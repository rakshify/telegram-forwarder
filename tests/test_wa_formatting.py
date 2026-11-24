import unittest

from src.adapters.whatsapp import formatting as fmt


def spans(rich):
    return sorted((s.kind, rich.text[s.start:s.end]) for s in rich.spans)


class ParseWhatsAppTests(unittest.TestCase):
    def test_basic_styles(self):
        r = fmt.parse_whatsapp("*bold* _it_ ~gone~")
        self.assertEqual(r.text, "bold it gone")
        self.assertEqual(spans(r), [("bold", "bold"), ("italic", "it"), ("strike", "gone")])

    def test_nested_styles(self):
        r = fmt.parse_whatsapp("*_both_* and *bold _inner_ bold*")
        self.assertEqual(r.text, "both and bold inner bold")
        self.assertEqual(spans(r), sorted([("bold", "both"), ("bold", "bold inner bold"),
                                           ("italic", "both"), ("italic", "inner")]))

    def test_text_that_is_not_formatting(self):
        for s in ("snake_case_name", "2*3*4", "a * b * c", "* list item", "file_name.txt",
                  "under_score_", "**", "_ _", "price*", "~ approx ~"):
            r = fmt.parse_whatsapp(s)
            self.assertEqual((r.text, r.spans), (s, []), s)

    def test_markers_inside_links_are_kept(self):
        s = "see https://x.com/_a_b_ and www.y.in/*z* now"
        r = fmt.parse_whatsapp(s)
        self.assertEqual((r.text, r.spans), (s, []))

    def test_bold_around_a_link(self):
        r = fmt.parse_whatsapp("*see https://x.com/a_b now*")
        self.assertEqual(r.text, "see https://x.com/a_b now")
        self.assertEqual(spans(r), [("bold", r.text)])

    def test_code_and_monospace(self):
        r = fmt.parse_whatsapp("run `ls -la` and ```x = *1*\ny = 2```")
        self.assertEqual(r.text, "run ls -la and x = *1*\ny = 2")
        self.assertEqual(spans(r), [("code", "ls -la"), ("pre", "x = *1*\ny = 2")])

    def test_single_line_monospace_is_inline_code(self):
        self.assertEqual(spans(fmt.parse_whatsapp("```mono```")), [("code", "mono")])

    def test_quotes(self):
        r = fmt.parse_whatsapp("> first\n> *second*\nafter")
        self.assertEqual(r.text, "first\nsecond\nafter")
        self.assertEqual(spans(r), [("bold", "second"), ("quote", "first\nsecond")])

    def test_greater_than_inside_a_line_is_text(self):
        r = fmt.parse_whatsapp("a > b\nx>y")
        self.assertEqual((r.text, r.spans), ("a > b\nx>y", []))

    def test_styles_do_not_span_lines(self):
        s = "*not\nbold*"
        r = fmt.parse_whatsapp(s)
        self.assertEqual((r.text, r.spans), (s, []))

    def test_non_latin_text(self):
        r = fmt.parse_whatsapp("नमस्ते *दुनिया* 🚀 _ok_")
        self.assertEqual(r.text, "नमस्ते दुनिया 🚀 ok")
        self.assertEqual(spans(r), [("bold", "दुनिया"), ("italic", "ok")])


if __name__ == "__main__":
    unittest.main()
