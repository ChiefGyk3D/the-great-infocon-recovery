"""
Tests for download verification on hosts that publish no Content-Length.

Neither infocon.org nor media.defcon.org sends `Content-Length`, sends
`Accept-Ranges`, or honours a `Range` request - a ranged GET answers 200 with
the whole body. Verified against both live hosts.

Everything gated on a remote size was therefore dead code:

  - `curl_head_size()` always returned None, so the HEAD issued for every file
    was a pure round trip. Across the archive that is 451,223 wasted requests
    through a four-connection budget.
  - the post-download size check never ran, so the documented atomic-write
    guarantee never actually verified anything. Across 8.5 hours and 8,775
    downloads of a real run, zero size mismatches were reported.
  - the skip-existing branch required `remote_size is not None`, so every file
    was re-downloaded on every run.
  - the free-space guard never ran.

The listing's published size replaces it, compared within the slack implied by
its printed precision.

Run with:
    python -m pytest tests/test_transfer_integrity.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infocon_scraper
from infocon_scraper import (
    CURL_RANGE_UNSUPPORTED,
    CurlError,
    download_atomic,
    host_supports_ranges,
    mark_host_without_ranges,
    parse_listing_size,
    parse_listing_size_slack,
)


class TestSizeSlack(unittest.TestCase):
    """Slack must track the listing's printed precision, not a flat percentage.

    A flat percentage is wrong in both directions: far too loose for a value
    printed to a tenth of a KiB, and far too tight to be meaningful at GiB
    scale. These expectations were checked against real downloads.
    """

    def test_byte_counts_are_exact(self):
        self.assertEqual(parse_listing_size("982 B"), 982)
        self.assertEqual(parse_listing_size_slack("982 B"), 0)

    def test_one_decimal_kib_allows_a_tenth_of_a_kib(self):
        self.assertEqual(parse_listing_size_slack("648.6 KiB"), 102)

    def test_gib_slack_scales_with_the_unit(self):
        self.assertEqual(parse_listing_size_slack("1.5 GiB"), int(0.1 * (1 << 30)))

    def test_real_listing_values_fall_within_slack(self):
        """Measured against live infocon.org downloads."""
        for listed, actual in (("982 B", 982), ("648.6 KiB", 664206),
                               ("134.5 KiB", 137751), ("16.8 KiB", 17235),
                               ("14.6 KiB", 14953)):
            with self.subTest(listed=listed):
                expected = parse_listing_size(listed)
                self.assertLessEqual(abs(actual - expected), parse_listing_size_slack(listed))

    def test_gross_truncation_is_outside_slack(self):
        """The check has to be able to fail, or it is not a check."""
        expected = parse_listing_size("648.6 KiB")
        slack = parse_listing_size_slack("648.6 KiB")
        self.assertGreater(abs(expected // 2 - expected), slack)


class TestRangeSupportRegistry(unittest.TestCase):

    def setUp(self):
        infocon_scraper._reset_range_support()
        self.addCleanup(infocon_scraper._reset_range_support)

    def test_hosts_are_assumed_to_support_ranges(self):
        self.assertTrue(host_supports_ranges("https://infocon.org/a.rar"))

    def test_marking_is_per_host_and_reported_once(self):
        self.assertTrue(mark_host_without_ranges("https://infocon.org/a.rar"))
        self.assertFalse(mark_host_without_ranges("https://infocon.org/b.rar"))
        self.assertFalse(host_supports_ranges("https://infocon.org/c.rar"))
        self.assertTrue(host_supports_ranges("https://media.defcon.org/d.rar"))


class TestDownloadAtomic(unittest.TestCase):

    def setUp(self):
        infocon_scraper._reset_range_support()
        self.addCleanup(infocon_scraper._reset_range_support)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.tmp.name, "file.bin")
        self.part = self.target + ".part"
        self.url = "https://infocon.org/file.bin"

    def stage(self, size: int):
        with open(self.part, "wb") as handle:
            handle.write(b"\0" * size)

    def test_download_within_slack_is_accepted(self):
        def fake(url, path, resume, timeout=0):
            self.stage(664206)

        with patch("infocon_scraper.curl_download", side_effect=fake):
            download_atomic(self.url, self.target, 664166, slack=102)
        self.assertEqual(os.path.getsize(self.target), 664206)
        self.assertFalse(os.path.exists(self.part))

    def test_truncated_download_is_rejected(self):
        """Previously accepted silently, because remote_size was always None."""
        def fake(url, path, resume, timeout=0):
            self.stage(1000)

        with patch("infocon_scraper.curl_download", side_effect=fake), \
                patch("infocon_scraper.time.sleep"), self.assertRaises(CurlError) as caught:
            download_atomic(self.url, self.target, 664166, slack=102)
        self.assertIn("size mismatch", str(caught.exception))
        self.assertFalse(os.path.exists(self.target))

    def test_unknown_size_still_completes(self):
        """Some listings omit a size; that must not block the transfer."""
        def fake(url, path, resume, timeout=0):
            self.stage(4321)

        with patch("infocon_scraper.curl_download", side_effect=fake):
            download_atomic(self.url, self.target, None)
        self.assertEqual(os.path.getsize(self.target), 4321)

    def test_range_refusal_discards_the_part_and_retries_clean(self):
        """curl 33 used to burn every attempt and fail the file permanently."""
        attempts = []

        def fake(url, path, resume, timeout=0):
            attempts.append(resume)
            if resume:
                raise CurlError("cannot resume", returncode=CURL_RANGE_UNSUPPORTED)
            self.stage(5000)

        self.stage(1200)
        with patch("infocon_scraper.curl_download", side_effect=fake):
            download_atomic(self.url, self.target, 5000, slack=0)

        self.assertEqual(attempts, [True, False])
        self.assertEqual(os.path.getsize(self.target), 5000)
        self.assertFalse(host_supports_ranges(self.url))

    def test_no_resume_attempted_once_a_host_is_known_to_refuse(self):
        mark_host_without_ranges(self.url)
        attempts = []

        def fake(url, path, resume, timeout=0):
            attempts.append(resume)
            self.stage(5000)

        self.stage(1200)
        with patch("infocon_scraper.curl_download", side_effect=fake):
            download_atomic(self.url, self.target, 5000, slack=0)
        self.assertEqual(attempts, [False])

    def test_transient_failure_keeps_staged_bytes(self):
        """A network blip must not throw away a partial transfer."""
        calls = []

        def fake(url, path, resume, timeout=0):
            calls.append(resume)
            if len(calls) == 1:
                self.stage(2000)
                raise CurlError("connection reset", returncode=56)
            self.stage(5000)

        with patch("infocon_scraper.curl_download", side_effect=fake), \
                patch("infocon_scraper.time.sleep"):
            download_atomic(self.url, self.target, 5000, slack=0)
        self.assertEqual(calls, [False, True], "second attempt should have resumed")

    def test_overshoot_discards_the_part(self):
        sizes = [9000, 5000]

        def fake(url, path, resume, timeout=0):
            self.stage(sizes.pop(0))

        with patch("infocon_scraper.curl_download", side_effect=fake), \
                patch("infocon_scraper.time.sleep"):
            download_atomic(self.url, self.target, 5000, slack=0)
        self.assertEqual(os.path.getsize(self.target), 5000)

    def test_in_flight_bytes_are_released_on_failure(self):
        def fake(url, path, resume, timeout=0):
            raise CurlError("dead", returncode=7)

        with patch("infocon_scraper.curl_download", side_effect=fake), \
                patch("infocon_scraper.time.sleep"), self.assertRaises(CurlError):
            download_atomic(self.url, self.target, 5000)
        self.assertEqual(infocon_scraper.inflight_bytes(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
