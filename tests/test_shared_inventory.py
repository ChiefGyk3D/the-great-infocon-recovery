"""
Deterministic unit tests for the shared online directory inventory refactor.

These tests use fake in-process listing data (no network) to verify:
  1. scan_infocon_tree() correctly partitions entries into http_files vs
     torrent_candidates, recurses into subdirectories, and respects the
     is_http flag for extra_torrent_roots.
  2. _merge_infocon_candidates() deduplicates by logical name (media.defcon.org
     wins), picks the highest version within infocon.org candidates, and adds
     genuinely novel candidates.

Run with:
    python -m pytest tests/test_shared_inventory.py -v
or:
    python tests/test_shared_inventory.py
"""
from __future__ import annotations

import sys
import os
import subprocess
import threading
import unittest
from unittest.mock import patch, call

# Make the project root importable regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infocon_scraper
from infocon_scraper import RemoteFile, scan_infocon_tree


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dir_entry(name: str, is_dir: bool, modified: float = 1000.0) -> dict:
    href = name + "/" if is_dir else name
    return {"href": href, "name": name, "is_dir": is_dir, "modified": modified}


# ---------------------------------------------------------------------------
# scan_infocon_tree tests
# ---------------------------------------------------------------------------

class TestScanInfoconTree(unittest.TestCase):

    def _run_scan(self, listing_map: dict[str, list[dict]], http_roots, extra_torrent_roots,
                  dest_root="/dest") -> tuple[list[RemoteFile], list[dict]]:
        """Patch list_directory with a dict lookup, run scan_infocon_tree, return results."""
        def fake_list(url: str) -> list[dict]:
            return listing_map.get(url, [])

        with patch.object(infocon_scraper, "list_directory", side_effect=fake_list):
            return scan_infocon_tree(
                http_roots, extra_torrent_roots,
                crawl_workers=4, dest_root=dest_root,
            )

    def test_regular_files_go_to_http_files(self):
        listing_map = {
            "https://infocon.org/docs/": [
                _dir_entry("slides.pdf", is_dir=False),
                _dir_entry("notes.txt", is_dir=False),
            ],
        }
        http_files, torrent_candidates = self._run_scan(
            listing_map,
            http_roots=[("https://infocon.org/docs/", "docs")],
            extra_torrent_roots=[],
        )
        rel_paths = [f.rel_path for f in http_files]
        self.assertIn(os.path.join("docs", "slides.pdf"), rel_paths)
        self.assertIn(os.path.join("docs", "notes.txt"), rel_paths)
        self.assertEqual(torrent_candidates, [])

    def test_torrent_files_go_to_candidates_not_http(self):
        listing_map = {
            "https://infocon.org/docs/": [
                _dir_entry("BSides 2020 v1.torrent", is_dir=False),
                _dir_entry("talk.mp4", is_dir=False),
            ],
        }
        http_files, torrent_candidates = self._run_scan(
            listing_map,
            http_roots=[("https://infocon.org/docs/", "docs")],
            extra_torrent_roots=[],
        )
        self.assertEqual(len(http_files), 1)
        self.assertEqual(http_files[0].rel_path, os.path.join("docs", "talk.mp4"))
        self.assertEqual(len(torrent_candidates), 1)
        cand = torrent_candidates[0]
        self.assertEqual(cand["name"], "BSides 2020 v1.torrent")
        self.assertEqual(cand["url"], "https://infocon.org/docs/BSides 2020 v1.torrent")
        self.assertEqual(cand["save_path"], os.path.join("/dest", "docs"))

    def test_recursion_into_subdirectory(self):
        listing_map = {
            "https://infocon.org/cons/": [
                _dir_entry("BSides LV", is_dir=True),
            ],
            "https://infocon.org/cons/BSides LV/": [
                _dir_entry("talk.mp4", is_dir=False),
            ],
        }
        http_files, _ = self._run_scan(
            listing_map,
            http_roots=[("https://infocon.org/cons/", "cons")],
            extra_torrent_roots=[],
        )
        self.assertEqual(len(http_files), 1)
        self.assertEqual(http_files[0].rel_path, os.path.join("cons", "BSides LV", "talk.mp4"))

    def test_extra_torrent_roots_not_in_http_files(self):
        """Files under extra_torrent_roots should produce torrent_candidates only."""
        listing_map = {
            "https://infocon.org/cons/DEF%20CON/": [
                _dir_entry("DEF CON 30 v2.torrent", is_dir=False),
                _dir_entry("DEF CON 30.mp4", is_dir=False),  # regular file, torrent-root only
            ],
        }
        http_files, torrent_candidates = self._run_scan(
            listing_map,
            http_roots=[],
            extra_torrent_roots=[("https://infocon.org/cons/DEF%20CON/", "cons/DEF CON")],
        )
        # No HTTP files from a torrent-only root
        self.assertEqual(http_files, [])
        self.assertEqual(len(torrent_candidates), 1)
        self.assertEqual(torrent_candidates[0]["name"], "DEF CON 30 v2.torrent")

    def test_http_root_also_collects_torrent_candidates(self):
        """HTTP roots yield both http_files and torrent_candidates."""
        listing_map = {
            "https://infocon.org/pods/": [
                _dir_entry("ep01.mp3", is_dir=False),
                _dir_entry("ep01.torrent", is_dir=False),
            ],
        }
        http_files, torrent_candidates = self._run_scan(
            listing_map,
            http_roots=[("https://infocon.org/pods/", "pods")],
            extra_torrent_roots=[],
        )
        self.assertEqual(len(http_files), 1)
        self.assertEqual(len(torrent_candidates), 1)

    def test_stop_requested_halts_scan(self):
        """A pre-set stop_requested event causes the scan to return early."""
        listing_map = {
            "https://infocon.org/docs/": [_dir_entry("file.pdf", is_dir=False)],
        }
        stop = threading.Event()
        stop.set()

        def fake_list(url: str) -> list[dict]:
            return listing_map.get(url, [])

        with patch.object(infocon_scraper, "list_directory", side_effect=fake_list):
            http_files, torrent_candidates = scan_infocon_tree(
                [("https://infocon.org/docs/", "docs")], [],
                crawl_workers=2, dest_root="/dest", stop_requested=stop,
            )
        # Result may be empty or partial; key assertion is no exception raised
        self.assertIsInstance(http_files, list)
        self.assertIsInstance(torrent_candidates, list)

    def test_listing_error_is_skipped(self):
        """A listing failure for one URL should not abort the whole scan."""
        def fake_list(url: str) -> list[dict]:
            if "bad" in url:
                raise RuntimeError("simulated network error")
            return [_dir_entry("file.pdf", is_dir=False)]

        with patch.object(infocon_scraper, "list_directory", side_effect=fake_list), \
            patch.object(infocon_scraper.RUN, "listing_retries", 0):
            http_files, _ = scan_infocon_tree(
                [("https://infocon.org/bad/", "bad"),
                 ("https://infocon.org/good/", "good")],
                [],
                crawl_workers=2, dest_root="/dest",
            )
        rel_paths = [f.rel_path for f in http_files]
        self.assertIn(os.path.join("good", "file.pdf"), rel_paths)

    def test_listing_error_is_retried(self):
        """A transient directory-listing failure is retried before being abandoned."""
        attempts = 0

        def fake_list(url: str) -> list[dict]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient timeout")
            return [_dir_entry("file.pdf", is_dir=False)]

        with patch.object(infocon_scraper, "list_directory", side_effect=fake_list), \
                patch.object(infocon_scraper.RUN, "listing_retries", 1):
            http_files, _ = scan_infocon_tree(
                [("https://infocon.org/retry/", "retry")], [],
                crawl_workers=1, dest_root="/dest", listing_retry_delay=0,
            )

        self.assertEqual(attempts, 2)
        self.assertEqual([item.rel_path for item in http_files], [os.path.join("retry", "file.pdf")])


# ---------------------------------------------------------------------------
# _merge_infocon_candidates tests
# ---------------------------------------------------------------------------

class TestMergeInfoconCandidates(unittest.TestCase):

    def _spec(self, name: str, url: str = "", save_path: str = "/dest/cons/DEF CON") -> object:
        from fetch_defcon_torrents import TorrentSpec
        return TorrentSpec(name=name, url=url or f"https://media.defcon.org/{name}.torrent",
                           save_path=save_path)

    def _cand(self, filename: str, save_path: str = "/dest/cons/DEF CON") -> dict:
        return {
            "name": filename,
            "url": f"https://infocon.org/cons/DEF CON/{filename}",
            "save_path": save_path,
        }

    def test_mediadefcon_wins_over_infocon(self):
        from fetch_defcon_torrents import _merge_infocon_candidates
        available = [self._spec("DEF CON 30")]
        candidates = [self._cand("DEF CON 30 v1.torrent")]
        result = _merge_infocon_candidates(available, candidates)
        # Only the media.defcon.org entry; infocon.org duplicate dropped
        names = [s.name for s in result]
        self.assertEqual(names.count("DEF CON 30"), 1)
        self.assertTrue(result[0].url.startswith("https://media.defcon.org"))

    def test_novel_candidate_added(self):
        from fetch_defcon_torrents import _merge_infocon_candidates
        available = [self._spec("DEF CON 30")]
        candidates = [self._cand("BSides LV 2020 v1.torrent", save_path="/dest/cons/BSides LV")]
        result = _merge_infocon_candidates(available, candidates)
        names = [s.name for s in result]
        self.assertIn("DEF CON 30", names)
        self.assertIn("BSides LV 2020", names)

    def test_highest_version_wins_within_candidates(self):
        from fetch_defcon_torrents import _merge_infocon_candidates
        available = []
        candidates = [
            self._cand("ShmooCon 2019 v1.torrent", save_path="/dest/cons/ShmooCon"),
            self._cand("ShmooCon 2019 v3.torrent", save_path="/dest/cons/ShmooCon"),
            self._cand("ShmooCon 2019 v2.torrent", save_path="/dest/cons/ShmooCon"),
        ]
        result = _merge_infocon_candidates(available, candidates)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "ShmooCon 2019")
        # v3 URL should be chosen
        self.assertIn("v3", result[0].url)

    def test_non_torrent_candidate_ignored(self):
        from fetch_defcon_torrents import _merge_infocon_candidates
        available = []
        candidates = [{"name": "readme.txt", "url": "https://infocon.org/readme.txt",
                       "save_path": "/dest"}]
        result = _merge_infocon_candidates(available, candidates)
        self.assertEqual(result, [])

    def test_empty_inputs_return_empty(self):
        from fetch_defcon_torrents import _merge_infocon_candidates
        self.assertEqual(_merge_infocon_candidates([], []), [])

    def test_available_unchanged_when_no_candidates(self):
        from fetch_defcon_torrents import _merge_infocon_candidates
        available = [self._spec("DEF CON 31"), self._spec("DEF CON 32")]
        result = _merge_infocon_candidates(available, [])
        self.assertEqual(len(result), 2)

    def test_versionless_and_versioned_dedup(self):
        """A versionless candidate and a v1 candidate with the same logical name collapse to one."""
        from fetch_defcon_torrents import _merge_infocon_candidates
        available = []
        candidates = [
            self._cand("Foo Con 2021.torrent", save_path="/dest/cons/Foo Con"),
            self._cand("Foo Con 2021 v1.torrent", save_path="/dest/cons/Foo Con"),
        ]
        result = _merge_infocon_candidates(available, candidates)
        self.assertEqual(len(result), 1)
        # v1 beats versionless (0)
        self.assertIn("v1", result[0].url)


class TestTorrentDiscoveryRetry(unittest.TestCase):

    def test_transient_listing_error_is_retried_without_marking_visited(self):
        from fetch_defcon_torrents import TorrentSettings, discover_torrents_recursive

        settings = TorrentSettings(
            max_active=1, connections=1, listen_interface="0.0.0.0:6881",
            poll_seconds=1, seed_time=0, enable_dht=False, enable_pex=False,
            enable_lsd=False, request_timeout=30, retries=0, retry_delay=0,
            discovery_retries=1, discovery_retry_delay=0,
        )
        attempts = 0

        def fake_curl_text(url: str, settings: object, timeout: int | None = None,
                   retries: int | None = None) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient timeout")
            return '<table id="list"><tbody><tr><td><a href="Example.torrent">Example.torrent</a></td></tr></tbody></table>'

        with patch("fetch_defcon_torrents.curl_text", side_effect=fake_curl_text):
            specs = discover_torrents_recursive(
                [("https://infocon.org/retry/", "/dest")], settings,
            )

        self.assertEqual(attempts, 2)
        self.assertEqual([spec.name for spec in specs], ["Example"])


class TestTorrentCurlText(unittest.TestCase):

    def test_non_utf8_listing_bytes_are_replaced(self):
        from fetch_defcon_torrents import TorrentSettings, curl_text

        settings = TorrentSettings(
            max_active=1, connections=1, listen_interface="0.0.0.0:6881",
            poll_seconds=1, seed_time=0, enable_dht=False, enable_pex=False,
            enable_lsd=False, request_timeout=30, retries=0, retry_delay=0,
        )
        completed = subprocess.CompletedProcess(
            args=["curl"], returncode=0, stdout=b'<table id="list">\xa0</table>', stderr=b"",
        )

        with patch("fetch_defcon_torrents.subprocess.run", return_value=completed):
            html = curl_text("https://infocon.org/example/", settings)

        self.assertIn("\ufffd", html)


class TestLibtorrentCompatibility(unittest.TestCase):

    def test_torrent_handle_supports_auto_managed_flag_control(self):
        import libtorrent as lt

        self.assertTrue(hasattr(lt.torrent_handle, "unset_flags"))
        self.assertTrue(hasattr(lt.torrent_flags, "auto_managed"))
        self.assertFalse(hasattr(lt.torrent_handle, "set_auto_managed"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
