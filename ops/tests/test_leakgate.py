import tempfile
import unittest
from pathlib import Path

from ops.leakgate import LeakGateError, Term, TermMatcher, check, load_terms, parse_terms, request_text, scan

TERMS = parse_terms("# comment\nJane Example\nAcme\n\nProject Bluebird\n")


class TermTests(unittest.TestCase):
    def test_case_insensitive_whole_words_and_flexible_spaces(self):
        matcher = TermMatcher(TERMS)
        self.assertEqual([t.text for t in matcher.find("a note from JANE\n  example today")], ["Jane Example"])
        self.assertEqual(matcher.find("Acmes and Acmeville are fine"), [])
        self.assertEqual(matcher.term_lines("acme, project bluebird"), [3, 5])

    def test_check_reports_terms_and_lines_but_not_the_text(self):
        seed = "Line one is fine.\nThe secret partner is Acme Holdings and more private context."
        with self.assertRaises(LeakGateError) as caught:
            check({"seed (variant v0)": seed, "requirement Q1": "clean"}, TERMS)
        message = str(caught.exception)
        self.assertIn("term 'Acme' (terms file line 3)", message)
        self.assertIn("seed (variant v0)", message)
        self.assertIn("line(s) 2", message)
        self.assertNotIn("private context", message)
        check({"seed": "nothing sensitive here"}, TERMS)


class PatternTests(unittest.TestCase):
    def kinds(self, text):
        return sorted({m.kind for m in scan(text, "seed", TermMatcher([]))})

    def test_emails_phones_and_long_digit_strings(self):
        self.assertEqual(self.kinds("write to someone.name@example.co.uk"), ["email"])
        self.assertEqual(self.kinds("call +44 20 7946 0958 now"), ["phone"])
        self.assertEqual(self.kinds("account 12345678"), ["long_digits"])

    def test_dates_years_and_amounts_are_not_flagged(self):
        self.assertEqual(self.kinds("on 2026-11-15, in 2020-2025, revenue 1,250,000 and 12.5%"), [])


class TermsFileTests(unittest.TestCase):
    def test_missing_empty_or_example_terms_block_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            terms, example = Path(tmp) / "sensitive_terms.txt", Path(tmp) / "sensitive_terms.example.txt"
            example.write_text("# x\nJane Example\n", encoding="utf-8")
            with self.assertRaises(LeakGateError):
                load_terms(terms, example)
            terms.write_text("# only comments\n", encoding="utf-8")
            with self.assertRaises(LeakGateError):
                load_terms(terms, example)
            terms.write_text("Jane Example\n", encoding="utf-8")
            with self.assertRaises(LeakGateError):
                load_terms(terms, example)
            terms.write_text("Real Name\n", encoding="utf-8")
            self.assertEqual(load_terms(terms, example), [Term("Real Name", 1)])

    def test_request_text_covers_messages_and_tools(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi Acme"}],
                "tools": [{"function": {"description": "post about Project Bluebird"}}]}
        self.assertEqual(TermMatcher(TERMS).term_lines(request_text(body)), [3, 5])


if __name__ == "__main__":
    unittest.main()
