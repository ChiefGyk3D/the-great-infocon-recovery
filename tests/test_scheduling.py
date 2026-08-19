"""
Tests for connection budgeting, download scheduling, and hashing.

Covers four throughput defects:

  1. One semaphore per host covered both metadata and transfers, so a few
     multi-gigabyte downloads held every slot for hours and starved directory
     listings on the same host.
  2. Every file got a HEAD request before the manifest was consulted - one
     request per file on a refresh, to learn nothing.
  3. Nothing limited concurrent large transfers, so queue order alone decided
     whether a run gave all its slots to multi-gigabyte archives.
  4. A completed listing future left in `pending` made wait(FIRST_COMPLETED)
     return instantly on every iteration, spinning the loop at full CPU
     whenever the download queue was full.

Run with:
    python -m pytest tests/test_scheduling.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infocon_scraper
from infocon_scraper import (
    METADATA,
    TRANSFER,
    Manifest,
    RemoteFile,
    _listing_matches_manifest,
    host_limit,
    host_semaphore,
    is_large_transfer,
    run_sync,
)


class RunConfigGuard(unittest.TestCase):
    """Restores the global RunConfig and semaphore cache around each test."""

    def setUp(self):
        self._saved = {
            field: getattr(infocon_scraper.RUN, field)
            for field in ("metadata_connections", "transfer_connections", "hash_workers",
                          "large_file_bytes", "max_large_downloads", "content_order")
        }
        infocon_scraper._reset_host_semaphores()
        infocon_scraper._reset_path_registry()
        self.addCleanup(self._restore)

    def _restore(self):
        for field, value in self._saved.items():
            setattr(infocon_scraper.RUN, field, value)
        infocon_scraper._reset_host_semaphores()
        infocon_scraper._reset_path_registry()


# ---------------------------------------------------------------------------
# 1. Separate metadata and transfer budgets
# ---------------------------------------------------------------------------

class TestHostBudget(RunConfigGuard):

    def test_metadata_and_transfer_are_distinct_semaphores(self):
        """A saturated download budget must leave listings able to proceed."""
        meta = host_semaphore("https://infocon.org/cons/", METADATA)
        transfer = host_semaphore("https://infocon.org/cons/x.rar", TRANSFER)
        self.assertIsNot(meta, transfer)

    def test_exhausting_transfers_does_not_block_metadata(self):
        transfer = host_semaphore("https://infocon.org/x.rar", TRANSFER)
        for _ in range(host_limit("infocon.org", TRANSFER)):
            self.assertTrue(transfer.acquire(blocking=False))
        self.addCleanup(lambda: [transfer.release()
                                 for _ in range(host_limit("infocon.org", TRANSFER))])
        meta = host_semaphore("https://infocon.org/cons/", METADATA)
        self.assertTrue(meta.acquire(blocking=False), "listings blocked by busy downloads")
        meta.release()

    def test_same_kind_returns_the_same_semaphore(self):
        self.assertIs(host_semaphore("https://infocon.org/a", METADATA),
                      host_semaphore("https://infocon.org/b", METADATA))

    def test_uncapped_host_has_no_semaphore(self):
        self.assertIsNone(host_semaphore("https://example.com/thing", METADATA))

    def test_cli_override_replaces_the_default_limit(self):
        infocon_scraper.RUN.transfer_connections = 9
        self.assertEqual(host_limit("infocon.org", TRANSFER), 9)
        self.assertEqual(host_limit("infocon.org", METADATA),
                         infocon_scraper.HOST_CONCURRENCY_LIMITS["infocon.org"][METADATA])


# ---------------------------------------------------------------------------
# 2. Manifest-first: no HEAD when the listing agrees with what we recorded
# ---------------------------------------------------------------------------

class TestManifestFastPath(RunConfigGuard):

    def item(self, modified=1_700_000_000.0, size=1000):
        return RemoteFile(url="https://infocon.org/cons/x.pdf", rel_path="cons/x.pdf",
                          modified=modified, size=size)

    def test_matching_size_and_mtime_is_a_match(self):
        entry = {"size": 1000, "mtime": 1_700_000_000.0, "sha256": "abc"}
        self.assertTrue(_listing_matches_manifest(self.item(), entry, 1000))

    def test_size_change_is_not_a_match(self):
        entry = {"size": 999, "mtime": 1_700_000_000.0}
        self.assertFalse(_listing_matches_manifest(self.item(), entry, 1000))

    def test_mtime_change_is_not_a_match(self):
        entry = {"size": 1000, "mtime": 1_600_000_000.0}
        self.assertFalse(_listing_matches_manifest(self.item(), entry, 1000))

    def test_small_clock_skew_is_tolerated(self):
        entry = {"size": 1000, "mtime": 1_700_000_001.0}
        self.assertTrue(_listing_matches_manifest(self.item(), entry, 1000))

    def test_entry_without_mtime_falls_back_to_a_head(self):
        """Manifests written before mtimes were recorded must not be trusted blindly."""
        entry = {"size": 1000, "sha256": "abc"}
        self.assertFalse(_listing_matches_manifest(self.item(), entry, 1000))

    def test_listing_without_mtime_falls_back_to_a_head(self):
        entry = {"size": 1000, "mtime": 1_700_000_000.0}
        self.assertFalse(_listing_matches_manifest(self.item(modified=0.0), entry, 1000))

    def test_missing_entry_falls_back_to_a_head(self):
        self.assertFalse(_listing_matches_manifest(self.item(), None, 1000))

    def test_known_good_file_issues_no_network_request(self):
        """The refresh case: one request per file used to be spent to learn nothing."""
        manifest = Manifest(":memory:")
        manifest.set("cons/x.pdf", {"size": 1000, "mtime": 1_700_000_000.0, "sha256": "abc"})
        with patch("infocon_scraper.curl_head_size", side_effect=AssertionError("HEAD issued")), \
                patch("infocon_scraper.os.path.exists", return_value=True), \
                patch("infocon_scraper.os.path.getsize", return_value=1000), \
                patch("infocon_scraper.is_unpacked_archive_duplicate", return_value=False):
            result, _ = infocon_scraper.sync_file(self.item(), "/mnt/archive", manifest,
                                                  verify_all=False, dry_run=False)
        self.assertEqual(result, "skip-known-good")

    def test_verify_all_still_forces_the_network_check(self):
        manifest = Manifest(":memory:")
        manifest.set("cons/x.pdf", {"size": 1000, "mtime": 1_700_000_000.0, "sha256": "abc"})
        with patch("infocon_scraper.curl_head_size", return_value=1000) as head, \
                patch("infocon_scraper.os.path.exists", return_value=True), \
                patch("infocon_scraper.os.path.getsize", return_value=1000), \
                patch("infocon_scraper.sha256_file", return_value="abc"), \
                patch("infocon_scraper.is_unpacked_archive_duplicate", return_value=False):
            result, _ = infocon_scraper.sync_file(self.item(), "/mnt/archive", manifest,
                                                  verify_all=True, dry_run=False)
        head.assert_called_once()
        self.assertEqual(result, "skip-verified")


# ---------------------------------------------------------------------------
# 3. Large-transfer budget
# ---------------------------------------------------------------------------

class TestLargeTransferBudget(RunConfigGuard):

    def test_classification_uses_the_listing_size(self):
        infocon_scraper.RUN.large_file_bytes = 1 << 30
        self.assertTrue(is_large_transfer(RemoteFile("u", "r", size=2 << 30)))
        self.assertFalse(is_large_transfer(RemoteFile("u", "r", size=1024)))

    def test_unknown_size_counts_as_small(self):
        """Otherwise a theme without size cells would stall the whole queue."""
        self.assertFalse(is_large_transfer(RemoteFile("u", "r", size=None)))

    def test_concurrent_large_transfers_are_capped(self):
        """Five huge files, a cap of two: at most two may be in flight at once."""
        infocon_scraper.RUN.large_file_bytes = 1 << 30
        infocon_scraper.RUN.max_large_downloads = 2
        infocon_scraper.RUN.hash_workers = 0

        in_flight = 0
        peak = 0
        guard = threading.Lock()
        release = threading.Event()

        def fake_sync(item, dest_root, manifest, verify_all, dry_run):
            nonlocal in_flight, peak
            with guard:
                in_flight += 1
                peak = max(peak, in_flight)
            release.wait(timeout=2)
            with guard:
                in_flight -= 1
            return "downloaded", item.size or 0

        files = [RemoteFile(url=f"https://infocon.org/big{i}.rar",
                            rel_path=f"big{i}.rar", size=4 << 30) for i in range(5)]

        def unblock():
            # Let the workers pile up first, then drain.
            threading.Timer(0.3, release.set).start()

        with patch("infocon_scraper.sync_file", side_effect=fake_sync):
            unblock()
            counts = run_sync([], "/mnt/archive", Manifest(":memory:"),
                              crawl_workers=2, download_workers=8, verify_all=False,
                              dry_run=False, stop_requested=threading.Event(),
                              initial_files=files, max_pending_downloads=8)

        self.assertEqual(counts.get("downloaded"), 5)
        self.assertLessEqual(peak, 2, f"{peak} large transfers ran at once, cap is 2")

    def test_small_files_are_not_capped(self):
        infocon_scraper.RUN.large_file_bytes = 1 << 30
        infocon_scraper.RUN.max_large_downloads = 1
        infocon_scraper.RUN.hash_workers = 0

        files = [RemoteFile(url=f"https://infocon.org/s{i}.txt",
                            rel_path=f"s{i}.txt", size=1024) for i in range(6)]
        with patch("infocon_scraper.sync_file",
                   side_effect=lambda *a, **k: ("downloaded", 1024)):
            counts = run_sync([], "/mnt/archive", Manifest(":memory:"),
                              crawl_workers=2, download_workers=4, verify_all=False,
                              dry_run=False, stop_requested=threading.Event(),
                              initial_files=files, max_pending_downloads=8)
        self.assertEqual(counts.get("downloaded"), 6)


# ---------------------------------------------------------------------------
# 5. A pre-built file list must actually be transferred
# ---------------------------------------------------------------------------

class TestPrebuiltFileList(RunConfigGuard):
    """Combined mode crawls once into a shared inventory, then calls
    run_sync(roots=[], initial_files=inventory). Driving the loop off `pending`
    meant that call returned instantly having downloaded nothing, so the entire
    non-DEF CON HTTP phase was a no-op while still reporting every file as
    discovered."""

    def sync_files(self, count: int, **kwargs):
        infocon_scraper.RUN.hash_workers = 0
        files = [RemoteFile(url=f"https://infocon.org/f{i}.txt", rel_path=f"f{i}.txt",
                            modified=1_700_000_000.0, size=10) for i in range(count)]
        synced = []
        with patch("infocon_scraper.sync_file",
                   side_effect=lambda item, *a, **k: (synced.append(item.rel_path),
                                                      ("downloaded", 10))[1]):
            counts = run_sync([], "/mnt/archive", Manifest(":memory:"),
                              crawl_workers=2, download_workers=4, verify_all=False,
                              dry_run=False, stop_requested=threading.Event(),
                              initial_files=files, **kwargs)
        return counts, synced

    def test_initial_files_without_roots_are_downloaded(self):
        counts, synced = self.sync_files(6, max_pending_downloads=8)
        self.assertEqual(counts.get("downloaded"), 6)
        self.assertEqual(len(synced), 6)

    def test_queue_larger_than_the_pending_limit_still_drains(self):
        counts, synced = self.sync_files(25, max_pending_downloads=4)
        self.assertEqual(counts.get("downloaded"), 25)
        self.assertEqual(len(set(synced)), 25)

    def test_empty_input_returns_cleanly(self):
        infocon_scraper.RUN.hash_workers = 0
        counts = run_sync([], "/mnt/archive", Manifest(":memory:"),
                          crawl_workers=2, download_workers=2, verify_all=False,
                          dry_run=False, stop_requested=threading.Event(),
                          initial_files=[], max_pending_downloads=4)
        self.assertEqual(counts, {})


# ---------------------------------------------------------------------------
# 4. The crawl loop must not spin when the download queue is full
# ---------------------------------------------------------------------------

class TestNoBusyLoop(RunConfigGuard):

    def test_saturated_downloads_do_not_spin_on_completed_listings(self):
        """A finished listing must be parked, not re-waited on every iteration.

        The loop is instrumented through wait(): with the bug, wait() returns
        immediately over and over while downloads are busy, so the call count
        explodes far beyond the number of real events.
        """
        infocon_scraper.RUN.hash_workers = 0
        infocon_scraper.RUN.large_file_bytes = 1 << 40

        entries = [{"href": f"f{i}", "name": f"f{i}", "is_dir": False,
                    "modified": 1_700_000_000.0, "size": 10} for i in range(6)]
        blocked = threading.Event()
        wait_calls = 0
        real_wait = infocon_scraper.wait

        def counting_wait(*args, **kwargs):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls > 400:
                blocked.set()  # bail out rather than hang the suite
            return real_wait(*args, **kwargs)

        def slow_sync(item, *a, **k):
            blocked.wait(timeout=1.5)
            return "downloaded", 10

        with patch("infocon_scraper.list_directory", return_value=entries), \
                patch("infocon_scraper.sync_file", side_effect=slow_sync), \
                patch("infocon_scraper.wait", side_effect=counting_wait):
            threading.Timer(0.5, blocked.set).start()
            run_sync([("https://infocon.org/cons/", "cons")], "/mnt/archive",
                     Manifest(":memory:"), crawl_workers=2, download_workers=2,
                     verify_all=False, dry_run=False, stop_requested=threading.Event(),
                     max_pending_downloads=2)

        self.assertLess(wait_calls, 100,
                        f"wait() called {wait_calls} times; the loop is spinning")


if __name__ == "__main__":
    unittest.main(verbosity=2)
