"""
Tests for directory-listing metadata parsing.

`--content-order newest` was inert: infocon.org and media.defcon.org render
dates as "2025 Dec 25 09:30", which matched none of the formats the parser
tried, so every entry scored modified=0.0 and ordering silently degraded to a
reverse-alphabetical name sort. That is why a run would open on the largest
word lists on the site.

These tests parse listings captured verbatim from both hosts (see
tests/fixtures/), so a future markup change fails here rather than silently
disabling ordering again.

Run with:
    python -m pytest tests/test_listing_metadata.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infocon_scraper
from infocon_scraper import (
    list_directory,
    parse_listing_date,
    parse_listing_size,
    safe_child_name,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
INFOCON_LISTING = os.path.join(FIXTURES, "infocon_org_cons_WOOT.html")
DEFCON_LISTING = os.path.join(FIXTURES, "media_defcon_org_DEF_20CON_2030.html")


def load(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def listing(fixture: str, order: str = "newest") -> list[dict]:
    previous = infocon_scraper.RUN.content_order
    infocon_scraper.RUN.content_order = order
    try:
        with patch("infocon_scraper.curl_get_text", return_value=load(fixture)):
            return list_directory("https://infocon.org/cons/WOOT/")
    finally:
        infocon_scraper.RUN.content_order = previous


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

class TestDateParsing(unittest.TestCase):

    def test_published_format_is_understood(self):
        """The exact format both archive hosts emit."""
        parsed = parse_listing_date("2025 Dec 25 09:30")
        self.assertEqual(datetime.fromtimestamp(parsed).strftime("%Y-%m-%d %H:%M"), "2025-12-25 09:30")

    def test_alternate_fancyindex_formats(self):
        for text in ("2022-05-09 02:27", "2022-05-09 02:27:31", "09-May-2022 02:27"):
            with self.subTest(text=text):
                self.assertGreater(parse_listing_date(text), 0.0)

    def test_epoch_sort_value_is_used_directly(self):
        self.assertEqual(parse_listing_date("1766673060"), 1766673060.0)

    def test_small_numbers_are_not_epochs(self):
        """A bare size or column index must not be mistaken for a timestamp."""
        self.assertEqual(parse_listing_date("982"), 0.0)

    def test_missing_and_placeholder_dates(self):
        self.assertEqual(parse_listing_date("-"), 0.0)
        self.assertEqual(parse_listing_date(""), 0.0)
        self.assertEqual(parse_listing_date("not a date"), 0.0)


# ---------------------------------------------------------------------------
# Size parsing
# ---------------------------------------------------------------------------

class TestSizeParsing(unittest.TestCase):

    def test_binary_units(self):
        self.assertEqual(parse_listing_size("982 B"), 982)
        self.assertEqual(parse_listing_size("16.8 KiB"), int(16.8 * 1024))
        self.assertEqual(parse_listing_size("1.5 GiB"), int(1.5 * (1 << 30)))

    def test_decimal_units(self):
        self.assertEqual(parse_listing_size("2 MB"), 2_000_000)

    def test_unitless_value_is_bytes(self):
        self.assertEqual(parse_listing_size("4096"), 4096)

    def test_directories_and_junk_have_no_size(self):
        for text in ("-", "", "   ", "unknown", "12 furlongs"):
            with self.subTest(text=text):
                self.assertIsNone(parse_listing_size(text))


# ---------------------------------------------------------------------------
# Path-segment safety
# ---------------------------------------------------------------------------

class TestSafeChildName(unittest.TestCase):

    def test_ordinary_names_pass(self):
        self.assertEqual(safe_child_name("DEF CON 30 logo.png"), "DEF CON 30 logo.png")

    def test_traversal_and_absolute_names_are_rejected(self):
        for name in ("..", ".", "../etc/passwd", "/etc/passwd", "a/b", "a\\b", ""):
            with self.subTest(name=name):
                self.assertIsNone(safe_child_name(name))


# ---------------------------------------------------------------------------
# End-to-end parsing of real captured listings
# ---------------------------------------------------------------------------

class TestInfoconListing(unittest.TestCase):

    def setUp(self):
        self.entries = listing(INFOCON_LISTING)

    def test_entries_are_parsed(self):
        self.assertGreater(len(self.entries), 10)

    def test_parent_link_is_excluded(self):
        self.assertNotIn("..", [entry["name"] for entry in self.entries])

    def test_every_entry_has_a_real_timestamp(self):
        """The regression: all of these used to be 0.0."""
        self.assertTrue(all(entry["modified"] > 0 for entry in self.entries))

    def test_files_report_a_size_and_directories_do_not(self):
        files = [entry for entry in self.entries if not entry["is_dir"]]
        directories = [entry for entry in self.entries if entry["is_dir"]]
        self.assertTrue(files and directories)
        self.assertTrue(all(entry["size"] is not None for entry in files))
        self.assertTrue(all(entry["size"] is None for entry in directories))

    def test_newest_first_is_actually_by_date(self):
        stamps = [entry["modified"] for entry in self.entries]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_oldest_first_reverses_the_order(self):
        stamps = [entry["modified"] for entry in listing(INFOCON_LISTING, order="oldest")]
        self.assertEqual(stamps, sorted(stamps))

    def test_ordering_is_not_merely_alphabetical(self):
        """Guards against the parser silently reverting to a name sort."""
        names = [entry["name"].lower() for entry in self.entries]
        self.assertNotEqual(names, sorted(names))
        self.assertNotEqual(names, sorted(names, reverse=True))


class TestDefconMediaListing(unittest.TestCase):

    def setUp(self):
        self.entries = listing(DEFCON_LISTING)

    def test_same_date_format_on_the_media_server(self):
        self.assertTrue(all(entry["modified"] > 0 for entry in self.entries))

    def test_large_archive_size_is_parsed(self):
        sizes = [entry["size"] for entry in self.entries if entry["size"]]
        self.assertTrue(any(size > 100_000_000 for size in sizes),
                        "expected at least one large archive in the DEF CON 30 listing")

    def test_newest_entry_is_newer_than_the_oldest(self):
        stamps = [entry["modified"] for entry in self.entries]
        self.assertGreater(stamps[0], stamps[-1])


# ---------------------------------------------------------------------------
# Opt-in top-level sections
# ---------------------------------------------------------------------------

class TestTopLevelSections(unittest.TestCase):

    def matched(self, probe: str) -> list[str]:
        filters = [f.strip().lower() for f in probe.split(",") if f.strip()]
        return [s for s in infocon_scraper.ALL_TOP_LEVEL_SECTIONS
                if any(f in s.lower() for f in filters)]

    def test_rainbow_tables_is_reachable(self):
        """The README documents this as the way to opt in; it used to match nothing."""
        self.assertEqual(self.matched("rainbow tables"), ["rainbow tables"])

    def test_mirrors_is_reachable(self):
        self.assertEqual(self.matched("mirrors"), ["mirrors"])

    def test_opt_in_sections_stay_out_of_the_default_crawl(self):
        for section in infocon_scraper.OPT_IN_TOP_LEVEL_SECTIONS:
            with self.subTest(section=section):
                self.assertNotIn(section, infocon_scraper.TOP_LEVEL_SECTIONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
