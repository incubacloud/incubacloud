"""A job's log as an agent reads it: clean, folded, in pages that fit."""

import json

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.limits import page_lines, size_of
from odoo.addons.incubacloud_mcp.protocol.log_view import (
    clean_lines,
    fold_similar,
    strip_ansi,
)


class TestLogView(BaseCase):
    """Escape codes go, runs of progress fold, other lines stay."""

    def test_escape_sequences_are_taken_out(self):
        """CSI, OSC (BEL or ST) and the short escapes leave the text alone."""
        for raw, clean in (
            ("\x1b[32m\x1b[1mcreate\x1b[0m", "create"),
            ("\x1b[2K\x1b[1Gdone", "done"),
            ("\x1b]0;a title\x07after", "after"),
            ("\x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\", "link"),
            ("\x1b(B\x1b[mplain\x1b=", "plain"),
            ("\x9b31mred", "red"),
            ("no codes [1m here", "no codes [1m here"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(strip_ansi(raw), clean)

    def test_runs_that_differ_only_in_numbers_fold_into_their_last(self):
        """Download, extraction and checkout progress fold; the count says
        how many lines the run hid."""
        lines = [
            *(f"6e00e0164b46 Extracting {n}B" for n in range(1, 6)),
            *(f"Downloading 1{n}.5MB/30.1MB" for n in range(3)),
            *(f"Updating files: {n}% ({n * 12}/1200)" for n in (10, 50, 100)),
            "Receiving objects: 1,234 of 5,678 at 00:01:23",
            "Receiving objects: 2,000 of 5,678 at 00:01:24",
            "single 7",
        ]
        self.assertEqual(
            list(fold_similar(lines)),
            [
                "6e00e0164b46 Extracting 5B (+4 similar lines)",
                "Downloading 12.5MB/30.1MB (+2 similar lines)",
                "Updating files: 100% (1200/1200) (+2 similar lines)",
                "Receiving objects: 2,000 of 5,678 at 00:01:24 (+1 similar line)",
                "single 7",
            ],
        )

    def test_lines_that_differ_in_more_than_numbers_stay(self):
        """Different words, layers, versions or units are not folded, nor
        are lines that are only alike apart from each other."""
        lines = [
            "6e00e0164b46 Pull complete",
            "a1b2c3d4e5f6 Pull complete",
            "a9b8c7d6e5f4 Pull complete",
            "Installing odoo-v17",
            "Installing odoo-v18",
            "Extracting 900B",
            "Extracting 1.2kB",
            "Requirement already satisfied: lxml",
            "Requirement already satisfied: babel",
            "same",
            "other",
            "same",
        ]
        self.assertEqual(list(fold_similar(lines)), lines)

    def test_identical_lines_fold_and_escape_codes_do_not_split_a_run(self):
        """The same line repeated folds, coloured or not."""
        self.assertEqual(
            list(clean_lines(["\x1b[33mWaiting\x1b[0m", "Waiting", "Waiting"])),
            ["Waiting (+2 similar lines)"],
        )

    def test_a_shown_line_keeps_its_place_as_the_log_grows(self):
        """Folding joins a line only to the run before it: the lines before
        the last stay where they were when more is written."""
        start = ["a", "step 1", "step 2"]
        more = [*start, "step 3", "b"]
        before, after = list(fold_similar(start)), list(fold_similar(more))
        self.assertEqual(after[: len(before) - 1], before[:-1])
        self.assertEqual(after[1], "step 3 (+2 similar lines)")


class TestPageLines(BaseCase):
    """Pages of lines fit, say how many there are and follow each other."""

    @staticmethod
    def _lines(count):
        """Lines of different lengths, accents and characters JSON escapes."""
        piece = 'é" '
        return [f"{index} {piece * (index % 37)}" for index in range(count)]

    def test_every_page_fits_and_the_pages_cover_every_line_once(self):
        """Following next_offset reaches every line; each page fits."""
        lines = self._lines(500)
        seen, offset, numbers = [], 0, []
        while offset is not None:
            page = page_lines(iter(lines), offset, None, None, 2048)
            self.assertLessEqual(size_of(page["items"]), 2048)
            self.assertEqual((page["total"], page["offset"]), (500, offset))
            seen.extend(page["items"])
            numbers.append(page["page"])
            offset = page["next_offset"]
        self.assertEqual(seen, lines)
        self.assertEqual(numbers, list(range(1, page["total_pages"] + 1)))

    def test_a_page_by_number_is_the_page_the_offsets_reach(self):
        """page N shows the lines the N-th answer from offset 0 shows; the
        last one too."""
        lines = self._lines(300)
        offset, by_offset = 0, []
        while offset is not None:
            page = page_lines(iter(lines), offset, 7, None, 1500)
            by_offset.append(page["items"])
            offset = page["next_offset"]
        total_pages = page_lines(iter(lines), 0, 7, None, 1500)["total_pages"]
        self.assertEqual(total_pages, len(by_offset))
        for number in (1, 2, total_pages):
            with self.subTest(page=number):
                page = page_lines(iter(lines), None, 7, number, 1500)
                self.assertEqual(page["items"], by_offset[number - 1])
                self.assertEqual(page["page"], number)
        last = page_lines(iter(lines), None, 7, total_pages, 1500)
        self.assertEqual((last["next_offset"], last["truncated"]), (None, False))
        self.assertEqual(last["items"][-1], lines[-1])

    def test_limit_counts_lines(self):
        """limit makes pages of that many lines at most."""
        page = page_lines(iter(self._lines(25)), 0, 10)
        self.assertEqual(
            (len(page["items"]), page["total_pages"], page["next_offset"]), (10, 3, 10)
        )

    def test_a_line_longer_than_a_page_is_cut_alone(self):
        """A line past the page is cut to fit, alone, and the next follows."""
        lines = ["short", "x" * 5000, "", "after"]
        page = page_lines(iter(lines), 1, None, None, 1024)
        self.assertEqual(len(page["items"]), 1)
        self.assertLessEqual(size_of(page["items"]), 1024)
        self.assertTrue(page["truncated"])
        self.assertEqual((page["next_offset"], page["page"]), (2, 2))
        self.assertEqual(page_lines(iter(lines), 0)["total_pages"], 1)
        self.assertEqual(page_lines(iter(lines), 0, None, None, 1024)["total_pages"], 3)

    def test_an_empty_log_and_an_offset_past_its_end(self):
        """No lines is no page; an offset past the end shows nothing."""
        empty = page_lines(iter(()), 0)
        self.assertEqual(
            (empty["items"], empty["total"], empty["total_pages"], empty["page"]),
            ([], 0, 0, None),
        )
        past = page_lines(iter(self._lines(5)), 9)
        self.assertEqual(
            (past["items"], past["page"], past["next_offset"]), ([], None, None)
        )

    def test_the_page_is_measured_as_the_answer_serialises_it(self):
        """The size counted is the JSON of the list, escapes included."""
        lines = ["\x07" * 100] * 50
        page = page_lines(iter(lines), 0, None, None, 4096)
        self.assertLessEqual(
            len(json.dumps(page["items"], ensure_ascii=False).encode()), 4096
        )
