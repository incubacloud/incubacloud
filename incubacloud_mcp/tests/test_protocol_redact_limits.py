"""What an agent reads: no secret, no live link, no answer past 64 KB."""

import base64
import time
from urllib.parse import quote

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.limits import MAX_BYTES, cap_text, page_items
from odoo.addons.incubacloud_mcp.protocol.redact import (
    MASK,
    Scrubber,
    drop_secret_keys,
    find_secret_key,
    mask_token_urls,
)

SECRET = "s3cr3t/P@ss word+42"


class TestScrubber(BaseCase):
    def test_a_secret_is_masked_plain_url_encoded_and_in_base64(self):
        scrub = Scrubber([SECRET])
        for form in (
            SECRET,
            quote(SECRET, safe=""),
            base64.b64encode(SECRET.encode()).decode(),
        ):
            with self.subTest(form=form):
                self.assertEqual(scrub.text(f"x {form} y"), f"x {MASK} y")

    def test_short_values_are_left_alone(self):
        self.assertEqual(Scrubber(["odoo", ""]).text("odoo runs"), "odoo runs")

    def test_a_longer_secret_containing_a_shorter_one_is_masked_whole(self):
        scrub = Scrubber(["abcdefgh", "abcdefgh-ijklmnop"])
        self.assertEqual(scrub.text("abcdefgh-ijklmnop"), MASK)

    def test_credentials_in_a_url_are_masked(self):
        self.assertEqual(
            Scrubber([]).text("git clone https://bot:ghp_x1y2z3@github.com/acme/repo"),
            f"git clone https://{MASK}@github.com/acme/repo",
        )

    def test_a_token_alone_in_the_userinfo_is_masked(self):
        scrub = Scrubber([])
        for url in (
            "https://ghp_tok12345@github.com/acme/repo",
            "https://:ghp_tok12345@github.com/acme/repo",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    scrub.text(f"git clone {url}"),
                    f"git clone https://{MASK}@github.com/acme/repo",
                )
        plain = "https://example.com/?email=a@b.example"
        self.assertEqual(scrub.text(plain), plain)

    def test_nested_values_are_scrubbed(self):
        self.assertEqual(
            Scrubber([SECRET]).value({"a": [SECRET, 3], "b": None}),
            {"a": [MASK, 3], "b": None},
        )


class TestMailLinks(BaseCase):
    def test_links_that_carry_a_token_are_replaced(self):
        mail = (
            "Reset: https://acme.example/web/reset_password?token=abc&db=x\n"
            "Join: https://acme.example/web/signup?token=def\n"
            "Open: https://acme.example/my/orders/7?access_token=ghi\n"
            "Log in: https://acme.example/ic/login?t=jkl\n"
            "Plain: https://acme.example/shop"
        )
        masked = mask_token_urls({"body": mail})["body"]
        for leaked in ("abc", "def", "ghi", "jkl"):
            self.assertNotIn(leaked, masked)
        self.assertIn("https://acme.example/shop", masked)

    def test_links_in_html_mail_are_replaced_too(self):
        html = (
            '<a href="https://acme.example/my/orders/7?db=x&amp;access_token=tok111">'
            '<a href="https://acme.example/mail/view?model=x&amp;token=tok222">'
            '<a href="https://acme.example/ic/login?db=x&t=tok333">'
            '<a href="https://acme.example/shop">'
        )
        masked = mask_token_urls(html)
        for leaked in ("tok111", "tok222", "tok333"):
            self.assertNotIn(leaked, masked)
        self.assertIn("https://acme.example/shop", masked)


class TestRedactCost(BaseCase):
    def _seconds(self, function, text):
        """Return how long *function* takes on *text*."""
        start = time.monotonic()
        function(text)
        return time.monotonic() - start

    def test_hostile_text_is_scanned_in_linear_time(self):
        cases = (
            (mask_token_urls, "http://x" * 25_000),
            (Scrubber([]).text, "a." * 100_000),
        )
        for function, text in cases:
            with self.subTest(text=text[:16]):
                self.assertLess(self._seconds(function, text), 1.0)


class TestSecretKeys(BaseCase):
    NAMES = frozenset({"password", "postgres_password"})

    def test_keys_named_like_an_encrypted_field_are_dropped_has_flags_stay(self):
        self.assertEqual(
            drop_secret_keys(
                {
                    "name": "h",
                    "password": "x",
                    "has_password": True,
                    "rows": [{"postgres_password": "y", "id": 1}],
                },
                self.NAMES,
            ),
            {"name": "h", "has_password": True, "rows": [{"id": 1}]},
        )

    def test_a_secret_key_is_found_at_any_depth(self):
        self.assertEqual(
            find_secret_key({"vals": {"db": {"postgres_password": "x"}}}, self.NAMES),
            "arguments.vals.db.postgres_password",
        )
        self.assertIsNone(find_secret_key({"vals": {"name": "x"}}, self.NAMES))


class TestLimits(BaseCase):
    def test_a_long_list_comes_in_pages_under_64_kb(self):
        lines = [f"{i:06d} " + "x" * 1000 for i in range(1000)]
        first = page_items(lines)
        self.assertTrue(first["truncated"])
        self.assertLessEqual(sum(len(line) + 1 for line in first["items"]), MAX_BYTES)
        second = page_items(lines, offset=first["next_offset"])
        self.assertEqual(second["items"][0], lines[first["next_offset"]])

    def test_limit_is_honoured_and_the_last_page_says_so(self):
        page = page_items(list(range(10)), offset=8, limit=5)
        self.assertEqual(
            (page["items"], page["truncated"], page["next_offset"]),
            ([8, 9], False, None),
        )

    def test_one_huge_line_is_cut(self):
        page = page_items(["y" * (MAX_BYTES * 2)])
        self.assertLessEqual(len(page["items"][0]), MAX_BYTES)

    def test_text_is_cut_and_continues_at_next_offset(self):
        text = "z" * (MAX_BYTES + 10)
        first = cap_text(text)
        self.assertTrue(first["truncated"])
        rest = cap_text(text, first["next_offset"])
        self.assertEqual(first["text"] + rest["text"], text)
        self.assertFalse(rest["truncated"])
