"""
Deterministic unit tests for the stalled-torrent fallback path.

These cover the four failure modes that turned a healthy combined run into a
runaway HTTP crawl:

  1. Torrents the scheduler itself paused (queued behind --max-active) were
     reported as stalled, so every queued archive was handed to HTTP within
     `stalled_minutes` of checking finishing.
  2. A stalled torrent's fallback root was the .torrent file's parent
     directory, which for cons/*.torrent is the entire conference tree.
  3. Fallbacks whose content the ordinary sync already covers were crawled
     anyway.
  4. Two workers could target the same local path, corrupting one `.part` and
     failing its rename.

Run with:
    python -m pytest tests/test_stalled_fallback.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infocon_scraper
from infocon_scraper import RemoteFile, resolve_stalled_fallback_root, root_is_covered
from fetch_defcon_torrents import (
    TorrentSpec,
    _status_is_paused,
    _wants_to_download,
    torrent_content_folder,
)


class FakeStatus:
    """Minimal stand-in for libtorrent's torrent_status."""

    def __init__(self, flags: int = 0, num_peers: int = 0, download_rate: int = 0):
        self.flags = flags
        self.paused = bool(flags)
        self.num_peers = num_peers
        self.download_rate = download_rate


# ---------------------------------------------------------------------------
# 1. Stall detection must ignore torrents we paused ourselves
# ---------------------------------------------------------------------------

class TestStallDetection(unittest.TestCase):

    def setUp(self):
        import libtorrent as lt

        self.paused_flag = int(lt.torrent_flags.paused)

    def test_queued_torrent_is_not_a_stall_candidate(self):
        """A torrent outside the active set has zero peers by construction."""
        status = FakeStatus(flags=self.paused_flag)
        self.assertFalse(_wants_to_download("2600 archive", status, selected_names=set()))

    def test_paused_but_selected_torrent_is_not_a_stall_candidate(self):
        """Selected yet still paused: libtorrent has not started it, so it is not stalled."""
        status = FakeStatus(flags=self.paused_flag)
        self.assertFalse(
            _wants_to_download("2600 archive", status, selected_names={"2600 archive"})
        )

    def test_running_selected_torrent_is_a_stall_candidate(self):
        """Actively scheduled and unpaused with no peers: a genuine stall."""
        status = FakeStatus(flags=0)
        self.assertTrue(
            _wants_to_download("2600 archive", status, selected_names={"2600 archive"})
        )

    def test_status_paused_falls_back_to_legacy_attribute(self):
        """Builds without torrent_status.flags still report paused correctly."""

        class LegacyStatus:
            paused = True

        self.assertTrue(_status_is_paused(LegacyStatus()))

    def test_status_flags_take_priority(self):
        self.assertFalse(_status_is_paused(FakeStatus(flags=0)))
        self.assertTrue(_status_is_paused(FakeStatus(flags=self.paused_flag)))


# ---------------------------------------------------------------------------
# 2. Fallback roots must be the torrent's own content, never its parent tree
# ---------------------------------------------------------------------------

class TestFallbackRootResolution(unittest.TestCase):

    dest = "/mnt/archive"

    def test_infocon_archive_maps_to_its_own_folder(self):
        spec = TorrentSpec(
            name="2600 archive",
            url="https://infocon.org/cons/2600%20archive%20v1%20-%20infocon.org.torrent",
            save_path="/mnt/archive/cons",
        )
        url, rel = resolve_stalled_fallback_root(spec, self.dest, "https://media.defcon.org/")
        self.assertEqual(url, "https://infocon.org/cons/2600/")
        self.assertEqual(rel, os.path.join("cons", "2600"))

    def test_fallback_root_is_never_the_whole_cons_tree(self):
        """The regression that multiplied cons/ by the number of stalled torrents."""
        spec = TorrentSpec(
            name="BlueHat archive",
            url="https://infocon.org/cons/BlueHat%20archive%20v2%20-%20infocon.org.torrent",
            save_path="/mnt/archive/cons",
        )
        _, rel = resolve_stalled_fallback_root(spec, self.dest, "https://media.defcon.org/")
        self.assertNotEqual(rel.rstrip("/").lower(), "cons")
        self.assertTrue(rel.startswith("cons" + os.sep))

    def test_names_with_spaces_are_percent_encoded(self):
        spec = TorrentSpec(
            name="Wild West Hackin Fest archive",
            url="https://infocon.org/cons/Wild%20West%20Hackin%20Fest%20archive%20v1.torrent",
            save_path="/mnt/archive/cons",
        )
        url, rel = resolve_stalled_fallback_root(spec, self.dest, "https://media.defcon.org/")
        self.assertEqual(url, "https://infocon.org/cons/Wild%20West%20Hackin%20Fest/")
        self.assertEqual(rel, os.path.join("cons", "Wild West Hackin Fest"))

    def test_nested_save_path_keeps_its_prefix(self):
        spec = TorrentSpec(
            name="Word Lists archive",
            url="https://infocon.org/word%20lists/Word%20Lists%20archive%20v1.torrent",
            save_path="/mnt/archive/word lists",
        )
        _, rel = resolve_stalled_fallback_root(spec, self.dest, "https://media.defcon.org/")
        self.assertEqual(rel, os.path.join("word lists", "Word Lists"))

    def test_defcon_torrent_uses_the_media_server_root(self):
        spec = TorrentSpec(
            name="DEF CON 30",
            url="https://media.defcon.org/DEF%20CON%20Torrents/DEF%20CON%2030%20v2.torrent",
            save_path="/mnt/archive/cons/DEF CON",
        )
        url, rel = resolve_stalled_fallback_root(spec, self.dest, "https://media.defcon.org/")
        self.assertEqual(url, "https://media.defcon.org/DEF%20CON%2030/")
        self.assertEqual(rel, "cons/DEF CON/DEF CON 30")

    def test_underivable_folder_yields_no_fallback(self):
        """Better to skip than to crawl something broader than the torrent."""
        spec = TorrentSpec(
            name=" archive",
            url="https://infocon.org/cons/archive%20v1.torrent",
            save_path="/mnt/archive/cons",
        )
        self.assertIsNone(
            resolve_stalled_fallback_root(spec, self.dest, "https://media.defcon.org/")
        )

    def test_traversing_folder_name_is_refused(self):
        """A name resolving outside its own folder would crawl the site root."""
        spec = TorrentSpec(
            name=".. archive",
            url="https://infocon.org/cons/..%20archive%20v1.torrent",
            save_path="/mnt/archive/cons",
        )
        self.assertIsNone(
            resolve_stalled_fallback_root(spec, self.dest, "https://media.defcon.org/")
        )

    def test_nested_folder_name_is_refused(self):
        spec = TorrentSpec(
            name="a/b archive",
            url="https://infocon.org/cons/a%2Fb%20archive%20v1.torrent",
            save_path="/mnt/archive/cons",
        )
        self.assertIsNone(
            resolve_stalled_fallback_root(spec, self.dest, "https://media.defcon.org/")
        )

    def test_content_folder_strips_archive_suffixes(self):
        self.assertEqual(torrent_content_folder("2600 archive v1 - infocon.org"), "2600")
        self.assertEqual(torrent_content_folder("USENIX Security archive"), "USENIX Security")
        self.assertEqual(torrent_content_folder("DEF CON 30"), "DEF CON 30")


# ---------------------------------------------------------------------------
# 3. Fallbacks already covered by the sync plan must not run
# ---------------------------------------------------------------------------

class TestPlanCoverage(unittest.TestCase):

    planned = ["cons/2600", "cons/DEF CON/DEF CON 34", "word lists", "documentaries"]

    def test_exact_root_is_covered(self):
        self.assertTrue(root_is_covered("cons/2600", self.planned))

    def test_nested_path_is_covered_by_its_root(self):
        self.assertTrue(root_is_covered("word lists/Word Lists", self.planned))

    def test_unplanned_path_is_not_covered(self):
        self.assertFalse(root_is_covered("cons/BlueHat", self.planned))

    def test_sibling_prefix_is_not_treated_as_covered(self):
        """'cons/2600 Magazine' must not match the 'cons/2600' root."""
        self.assertFalse(root_is_covered("cons/2600 Magazine", self.planned))

    def test_comparison_is_case_and_separator_insensitive(self):
        self.assertTrue(root_is_covered(os.path.join("CONS", "2600"), self.planned))

    def test_empty_planned_root_covers_everything(self):
        self.assertTrue(root_is_covered("cons/anything", [""]))


# ---------------------------------------------------------------------------
# 4. No two workers may ever target the same local path
# ---------------------------------------------------------------------------

class TestPathClaims(unittest.TestCase):

    def setUp(self):
        infocon_scraper._reset_path_registry()
        self.addCleanup(infocon_scraper._reset_path_registry)

    def test_second_claim_on_the_same_path_is_refused(self):
        self.assertIsNotNone(infocon_scraper._claim_path("/mnt/archive/cons/x.pdf"))
        self.assertIsNone(infocon_scraper._claim_path("/mnt/archive/cons/x.pdf"))

    def test_claim_is_reusable_after_a_failed_release(self):
        key = infocon_scraper._claim_path("/mnt/archive/cons/x.pdf")
        infocon_scraper._release_path(key, finished=False)
        self.assertIsNotNone(infocon_scraper._claim_path("/mnt/archive/cons/x.pdf"))

    def test_completed_path_is_not_synced_twice(self):
        key = infocon_scraper._claim_path("/mnt/archive/cons/x.pdf")
        infocon_scraper._release_path(key, finished=True)
        self.assertIsNone(infocon_scraper._claim_path("/mnt/archive/cons/x.pdf"))

    def test_equivalent_paths_collide(self):
        self.assertIsNotNone(infocon_scraper._claim_path("/mnt/archive/cons/x.pdf"))
        self.assertIsNone(infocon_scraper._claim_path("/mnt/archive/cons/./x.pdf"))

    def test_concurrent_sync_file_downloads_the_path_once(self):
        """The exact race behind the '.part -> file: No such file or directory' errors."""
        started = threading.Barrier(2, timeout=5)
        downloads: list[str] = []
        downloads_lock = threading.Lock()
        results: list[str] = []
        results_lock = threading.Lock()

        def fake_download(url, local_path, remote_size):
            with downloads_lock:
                downloads.append(local_path)
            started.wait()

        item = RemoteFile(url="https://infocon.org/cons/x.pdf", rel_path="cons/x.pdf")

        def worker():
            result, _ = infocon_scraper.sync_file(
                item, "/mnt/archive", infocon_scraper.Manifest("/nonexistent/manifest.json"),
                verify_all=False, dry_run=False,
            )
            with results_lock:
                results.append(result)

        with patch("infocon_scraper.curl_head_size", return_value=None), \
                patch("infocon_scraper.os.path.exists", return_value=False), \
                patch("infocon_scraper.os.makedirs"), \
                patch("infocon_scraper.os.path.getsize", return_value=0), \
                patch("infocon_scraper.sha256_file", return_value="deadbeef"), \
                patch("infocon_scraper.download_atomic", side_effect=fake_download):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            # The first worker parks inside download_atomic; the second must be
            # turned away rather than joining it on the same .part file.
            started.wait()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(len(downloads), 1, "the same path was downloaded twice")
        self.assertIn("skip-duplicate-path", results)


if __name__ == "__main__":
    unittest.main(verbosity=2)
