"""What an agent reads: no secret, no live link, no answer past 64 KB."""

import base64
import json
import random
import string
import time
from urllib.parse import quote

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.limits import (
    MAX_BYTES,
    cap_text,
    fit_answer,
    page_items,
    page_record,
)
from odoo.addons.incubacloud_mcp.protocol.redact import (
    MASK,
    MAX_PARTS,
    Scrubber,
    drop_secret_keys,
    find_secret_key,
    mask_token_urls,
    secret_parts,
)

SECRET = "s3cr3t/P@ss word+42"


class TestScrubber(BaseCase):
    """Known secret values and URL credentials are masked in text."""

    def test_a_secret_is_masked_plain_url_encoded_and_in_base64(self):
        """A secret is masked as is, URL-encoded and in base64."""
        scrub = Scrubber([SECRET])
        for form in (
            SECRET,
            quote(SECRET, safe=""),
            base64.b64encode(SECRET.encode()).decode(),
        ):
            with self.subTest(form=form):
                self.assertEqual(scrub.text(f"x {form} y"), f"x {MASK} y")

    def test_short_values_are_left_alone(self):
        """Values too short to be told apart from text are not masked."""
        self.assertEqual(Scrubber(["odoo", ""]).text("odoo runs"), "odoo runs")

    def test_a_longer_secret_containing_a_shorter_one_is_masked_whole(self):
        """The longest secret wins: no half-masked value is left."""
        scrub = Scrubber(["abcdefgh", "abcdefgh-ijklmnop"])
        self.assertEqual(scrub.text("abcdefgh-ijklmnop"), MASK)

    def test_credentials_in_a_url_are_masked(self):
        """user:password in a repository URL is masked."""
        self.assertEqual(
            Scrubber([]).text("git clone https://bot:ghp_x1y2z3@github.com/acme/repo"),
            f"git clone https://{MASK}@github.com/acme/repo",
        )

    def test_a_token_alone_in_the_userinfo_is_masked(self):
        """A bare token or :token before the host is masked; a query email is not."""
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
        """Strings inside lists and dicts are scrubbed; other values stay."""
        self.assertEqual(
            Scrubber([SECRET]).value({"a": [SECRET, 3], "b": None}),
            {"a": [MASK, 3], "b": None},
        )

    def test_a_secret_used_as_a_key_is_masked(self):
        """A secret that is a dict's key is masked too."""
        self.assertEqual(
            Scrubber([SECRET]).value({f"by {SECRET}": 1, 2: "x"}),
            {f"by {MASK}": 1, 2: "x"},
        )


class TestSecretParts(BaseCase):
    """What one stored secret hides: the value, JSON leaves, PEM lines."""

    def test_a_json_document_gives_its_secret_leaves_not_machine_nor_login(self):
        """The text leaves under secret keys count, at any depth; others do not."""
        stored = json.dumps(
            {
                "machine": "backup.example.test",
                "login": "backup",
                "password": "bk-pass-5531",
                "new_usage_token": "usage-tok-77812",
                "icp": {"incubacloud.oidc.client_secret": "oidc-secret-90210"},
                "port": 2222,
            }
        )
        parts = secret_parts(stored, structured=True)
        self.assertIn(stored, parts)
        for leaf in ("bk-pass-5531", "usage-tok-77812", "oidc-secret-90210"):
            self.assertIn(leaf, parts)
        for public in ("backup.example.test", "backup", "2222"):
            self.assertNotIn(public, parts)

    def test_each_base64_line_of_a_pem_block_counts(self):
        """Each line of a PEM body counts, so a key printed line by line is masked."""
        body = [base64.b64encode(bytes([i]) * 48).decode() for i in range(3)]
        pem = "\n".join(
            ["-----BEGIN PRIVATE KEY-----", *body, "Zm9v", "-----END PRIVATE KEY-----"]
        )
        parts = secret_parts(pem, structured=True)
        for line in body:
            self.assertIn(line, parts)
        self.assertNotIn("Zm9v", parts)
        self.assertNotIn("-----END PRIVATE KEY-----", parts)

    def test_a_plain_value_is_itself(self):
        """A value that is neither JSON nor PEM is just itself."""
        self.assertEqual(
            secret_parts("plain-pass-123", structured=True), {"plain-pass-123"}
        )

    def test_a_value_of_a_field_that_stores_no_document_is_never_split(self):
        """JSON or a PEM-looking block in any other field is one secret: its
        words do not become secrets that would mask everyone's text."""
        pem = "-----BEGIN X-----\ndatabase\npostgres\ninstance\n-----END X-----"
        stored = json.dumps({"password": "bk-pass-5531"})
        self.assertEqual(secret_parts(pem), {pem})
        self.assertEqual(secret_parts(stored), {stored})

    def test_a_document_nested_past_the_stack_is_no_crash(self):
        """A document nested ~990 levels deep, read with a deep stack as a
        request has, is no crash."""
        value = '{"password":' * 990 + '"abcdefgh1234"' + "}" * 990

        def deep(levels):
            """Call secret_parts *levels* frames down."""
            if levels:
                return deep(levels - 1)
            return secret_parts(value, structured=True)

        self.assertIn(value, deep(150))

    def test_a_big_document_hides_a_bounded_number_of_secrets(self):
        """A document of ten thousand secret leaves gives at most MAX_PARTS
        pieces, quickly."""
        rng = random.Random(3)
        words = [
            "".join(rng.choices(string.ascii_lowercase + string.digits, k=10))
            for _ in range(10_000)
        ]
        stored = json.dumps({"password": words})
        started = time.perf_counter()
        parts = secret_parts(stored, structured=True)
        self.assertLessEqual(len(parts), MAX_PARTS + 1)
        self.assertLess(time.perf_counter() - started, 1)


class TestScrubberReuse(BaseCase):
    """The same secrets compile their pattern once per process."""

    def test_the_same_values_reuse_the_compiled_pattern(self):
        """Two scrubbers of the same values share one compiled pattern."""
        values = [f"reuse-secret-{index:04d}" for index in range(50)]
        first = Scrubber(values)
        self.assertIs(Scrubber(set(reversed(values)))._pattern, first._pattern)
        self.assertIsNot(Scrubber(values[1:])._pattern, first._pattern)


class TestMailLinks(BaseCase):
    """Links that sign someone in, in captured mail, are replaced."""

    def test_links_that_carry_a_token_are_replaced(self):
        """Reset, signup, access-token and panel login links go; plain links stay."""
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
        """In HTML, &amp;token=, &amp;access_token= and any /ic/login? query go too."""
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
    """Masking stays linear on hostile text."""

    def _seconds(self, function, text):
        """Return how long *function* takes on *text*."""
        start = time.monotonic()
        function(text)
        return time.monotonic() - start

    def test_hostile_text_is_scanned_in_linear_time(self):
        """200 KB built to make a regex backtrack takes under a second."""
        cases = (
            (mask_token_urls, "http://x" * 25_000),
            (Scrubber([]).text, "a." * 100_000),
        )
        for function, text in cases:
            with self.subTest(text=text[:16]):
                self.assertLess(self._seconds(function, text), 1.0)

    def test_a_thousand_secrets_scan_64_kb_well_under_100_ms(self):
        """Once compiled, 1000 secrets scan 64 KB of output in a few ms."""
        rng = random.Random(1)
        alphabet = string.ascii_letters + string.digits + "-_"
        values = ["".join(rng.choices(alphabet, k=32)) for _i in range(1000)]
        text = "".join(rng.choices(alphabet + " \n:/.", k=MAX_BYTES))
        Scrubber(values).text(text)
        self.assertLess(self._seconds(Scrubber(values).text, text), 0.1)
        self.assertEqual(
            Scrubber(values).text(f"a {values[7]} b {values[700]}"),
            f"a {MASK} b {MASK}",
        )

    def test_secrets_that_share_long_prefixes_still_mask(self):
        """Secrets nested a thousand levels deep in a prefix tree still mask."""
        values = ["x" * length + "y" for length in range(8, 1200)]
        self.assertEqual(Scrubber(values).text(f"<{values[900]}>"), f"<{MASK}>")


class TestSecretKeys(BaseCase):
    """Keys named like an encrypted field, in and out."""

    NAMES = frozenset({"password", "postgres_password"})

    def test_keys_named_like_an_encrypted_field_are_dropped_has_flags_stay(self):
        """Secret keys are dropped at any depth; has_* flags stay."""
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
        """A secret key in the arguments is found, with its path."""
        self.assertEqual(
            find_secret_key({"vals": {"db": {"postgres_password": "x"}}}, self.NAMES),
            "arguments.vals.db.postgres_password",
        )
        self.assertIsNone(find_secret_key({"vals": {"name": "x"}}, self.NAMES))


def _answer_size(value):
    """Return the bytes *value* takes in a tool's answer."""
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


class TestLimits(BaseCase):
    """No answer carries more than 64 KB, and the rest can be asked for."""

    def test_a_long_list_comes_in_pages_under_64_kb(self):
        """A long list comes in pages under 64 KB that follow on."""
        lines = [f"{i:06d} " + "x" * 1000 for i in range(1000)]
        first = page_items(lines)
        self.assertTrue(first["truncated"])
        self.assertLessEqual(sum(len(line) + 1 for line in first["items"]), MAX_BYTES)
        second = page_items(lines, offset=first["next_offset"])
        self.assertEqual(second["items"][0], lines[first["next_offset"]])

    def test_limit_is_honoured_and_the_last_page_says_so(self):
        """limit is honoured and the last page has no next_offset."""
        page = page_items(list(range(10)), offset=8, limit=5)
        self.assertEqual(
            (page["items"], page["truncated"], page["next_offset"]),
            ([8, 9], False, None),
        )

    def test_one_huge_line_is_cut(self):
        """A single line over the cap is cut to fit."""
        page = page_items(["y" * (MAX_BYTES * 2)])
        self.assertLessEqual(len(page["items"][0]), MAX_BYTES)

    def test_a_single_entry_that_was_cut_says_so(self):
        """A page whose only entry was cut says truncated."""
        self.assertTrue(page_items(["y" * (MAX_BYTES * 2)])["truncated"])

    def test_a_big_dict_is_cut_too(self):
        """An entry that is not a string is cut too, as its JSON text."""
        page = page_items([{"log": "x" * (MAX_BYTES * 2)}])
        self.assertTrue(page["truncated"])
        self.assertLessEqual(_answer_size(page["items"]), MAX_BYTES)

    def test_a_page_is_measured_as_the_answer_serialises_it(self):
        """Escaped quotes count, accented letters count as UTF-8, no more."""
        quotes = page_items(['"' * MAX_BYTES])
        self.assertLessEqual(_answer_size(quotes["items"]), MAX_BYTES)
        accented = page_items(["é" * 500] * 200)
        size = _answer_size(accented["items"])
        self.assertLessEqual(size, MAX_BYTES)
        self.assertGreater(size, MAX_BYTES - 1100)

    def test_a_character_across_the_cut_is_not_broken(self):
        """A character across the cut goes whole to the next slice."""
        text = "a" * (MAX_BYTES - 1) + "é" * 3
        first = cap_text(text)
        self.assertLessEqual(len(first["text"].encode("utf-8")), MAX_BYTES)
        self.assertNotIn("\ufffd", first["text"])
        rest = cap_text(text, first["next_offset"])
        self.assertEqual(first["text"] + rest["text"], text)

    def test_an_answer_too_big_is_cut_lists_first(self):
        """Lists are cut before strings; what fits is left alone."""
        small = {"a": [1, 2], "b": "x"}
        self.assertEqual(fit_answer(small), (small, False))
        long_list = {"rows": list(range(100_000)), "name": "kept", "none": []}
        cut, was_cut = fit_answer(long_list)
        self.assertTrue(was_cut)
        self.assertEqual(cut["name"], "kept")
        self.assertEqual(cut["rows"][:3], [0, 1, 2])
        self.assertLessEqual(len(json.dumps(cut).encode()), MAX_BYTES)
        one_string = {"note": "z" * 200_000}
        cut, was_cut = fit_answer(one_string)
        self.assertTrue(was_cut)
        self.assertLessEqual(len(json.dumps(cut).encode()), MAX_BYTES)

    def test_a_record_with_huge_fields_still_fits_its_page(self):
        """Huge metadata is cut too: the whole record fits, and says so."""
        record = {
            "subject": "s" * 100_000,
            "attachments": [{"filename": "f" * 900}] * 200,
            "text": "t" * 10,
            "html": "",
        }
        page = page_record(record, ("text", "html"))
        size = len(json.dumps(page, ensure_ascii=False).encode())
        self.assertLessEqual(size, MAX_BYTES)
        self.assertTrue(page["truncated"])
        self.assertEqual((page["text"], page["next_offset"]), ("t" * 10, None))
        self.assertLessEqual(len(page["subject"]), 1024)

    def test_text_is_cut_and_continues_at_next_offset(self):
        """Text cut at the cap continues at next_offset, nothing lost."""
        text = "z" * (MAX_BYTES + 10)
        first = cap_text(text)
        self.assertTrue(first["truncated"])
        rest = cap_text(text, first["next_offset"])
        self.assertEqual(first["text"] + rest["text"], text)
        self.assertFalse(rest["truncated"])
