"""
Tests for keeping the HTTP sync out of directories a torrent is writing.

libtorrent stores files sparsely: an interrupted torrent leaves a file at its
FINAL size containing holes. The HTTP sync decides "is this file complete?" by
comparing size against the directory listing, because neither archive host
sends Content-Length. A half-downloaded torrent file passes that check, gets
SHA-256'd, and is recorded as good permanently - and --verify-all would only
ever re-confirm the hash of the corrupt copy.

Measured on a live run: 206 sparse files sat in conference folders that both
engines were writing into, while HTTP had already fetched 261 files into
cons/BlueHat as the BlueHat torrent passed 88% on the same content. None had
been mis-catalogued yet, but every one was an opportunity.

Two defences, tested here:
  1. While a torrent owns a directory, HTTP does not touch it at all - which
     also stops the two engines duplicating each other's transfers.
  2. A file that claims the right size but is not actually allocated is treated
     as incomplete regardless, catching abandoned torrents and truncation from
     any other cause.

Run with:
    python -m pytest tests/test_torrent_ownership.py -v
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
    Manifest,
    RemoteFile,
    claim_torrent_path,
    is_torrent_owned,
    looks_incomplete,
    release_torrent_path,
)


class TestOwnershipRegistry(unittest.TestCase):

    def setUp(self):
        infocon_scraper._reset_torrent_paths()
        self.addCleanup(infocon_scraper._reset_torrent_paths)

    def test_nothing_is_owned_by_default(self):
        self.assertFalse(is_torrent_owned("/mnt/archive/cons/BlueHat/talk.mp4"))

    def test_files_under_a_claimed_directory_are_owned(self):
        claim_torrent_path("/mnt/archive/cons/BlueHat")
        self.assertTrue(is_torrent_owned("/mnt/archive/cons/BlueHat/talk.mp4"))
        self.assertTrue(is_torrent_owned("/mnt/archive/cons/BlueHat/2023/deep/talk.mp4"))

    def test_the_directory_itself_is_owned(self):
        claim_torrent_path("/mnt/archive/cons/BlueHat")
        self.assertTrue(is_torrent_owned("/mnt/archive/cons/BlueHat"))

    def test_sibling_directories_are_not_owned(self):
        claim_torrent_path("/mnt/archive/cons/BlueHat")
        self.assertFalse(is_torrent_owned("/mnt/archive/cons/BlueHatExtra/talk.mp4"))
        self.assertFalse(is_torrent_owned("/mnt/archive/cons/44CON/talk.mp4"))

    def test_release_returns_the_directory_to_http(self):
        """A completed or stalled torrent must hand its folder back."""
        claim_torrent_path("/mnt/archive/cons/BlueHat")
        release_torrent_path("/mnt/archive/cons/BlueHat")
        self.assertFalse(is_torrent_owned("/mnt/archive/cons/BlueHat/talk.mp4"))

    def test_paths_are_normalised(self):
        claim_torrent_path("/mnt/archive/cons/BlueHat/")
        self.assertTrue(is_torrent_owned("/mnt/archive/cons/./BlueHat/talk.mp4"))

    def test_multiple_torrents_tracked_independently(self):
        claim_torrent_path("/mnt/archive/cons/BlueHat")
        claim_torrent_path("/mnt/archive/cons/AusCERT")
        release_torrent_path("/mnt/archive/cons/BlueHat")
        self.assertFalse(is_torrent_owned("/mnt/archive/cons/BlueHat/a.mp4"))
        self.assertTrue(is_torrent_owned("/mnt/archive/cons/AusCERT/a.mp4"))

    def test_releasing_an_unclaimed_path_is_harmless(self):
        release_torrent_path("/mnt/archive/cons/Nothing")


class TestSparseDetection(unittest.TestCase):
    """A sparse file is the exact shape of an interrupted torrent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "archive.mp4")

    def make_sparse(self, apparent: int, written: int) -> None:
        with open(self.path, "wb") as handle:
            handle.truncate(apparent)       # full size, no blocks allocated
            handle.seek(0)
            handle.write(b"\xff" * written)  # only the first pieces present

    def make_solid(self, size: int) -> None:
        with open(self.path, "wb") as handle:
            handle.write(b"\xff" * size)

    def test_mostly_unallocated_file_is_incomplete(self):
        self.make_sparse(8 << 20, 64 << 10)
        self.assertTrue(looks_incomplete(self.path, 8 << 20))

    def test_fully_written_file_is_complete(self):
        self.make_solid(4 << 20)
        self.assertFalse(looks_incomplete(self.path, 4 << 20))

    def test_size_mismatch_is_left_to_the_size_check(self):
        """Only files claiming the right size need the allocation test."""
        self.make_sparse(8 << 20, 64 << 10)
        self.assertFalse(looks_incomplete(self.path, 999))

    def test_unknown_expected_size_is_not_judged(self):
        self.make_sparse(8 << 20, 64 << 10)
        self.assertFalse(looks_incomplete(self.path, None))

    def test_missing_file_is_not_judged(self):
        self.assertFalse(looks_incomplete(os.path.join(self.tmp.name, "gone"), 100))


class TestSyncFileBehaviour(unittest.TestCase):

    def setUp(self):
        infocon_scraper._reset_torrent_paths()
        infocon_scraper._reset_path_registry()
        self.addCleanup(infocon_scraper._reset_torrent_paths)
        self.addCleanup(infocon_scraper._reset_path_registry)
        self.item = RemoteFile(url="https://infocon.org/cons/BlueHat/talk.mp4",
                               rel_path="cons/BlueHat/talk.mp4",
                               modified=1_700_000_000.0, size=5_000_000)

    def test_owned_paths_are_skipped_without_any_network_use(self):
        """This is the duplicate-transfer fix as much as the corruption fix."""
        claim_torrent_path("/mnt/archive/cons/BlueHat")
        with patch("infocon_scraper.curl_head_size", side_effect=AssertionError("HEAD issued")), \
                patch("infocon_scraper.download_atomic", side_effect=AssertionError("downloaded")), \
                patch("infocon_scraper.sha256_file", side_effect=AssertionError("hashed")):
            result, nbytes = infocon_scraper.sync_file(
                self.item, "/mnt/archive", Manifest(":memory:"),
                verify_all=False, dry_run=False)
        self.assertEqual(result, "skip-torrent-owned")
        self.assertEqual(nbytes, 0)

    def test_released_paths_are_processed_normally(self):
        claim_torrent_path("/mnt/archive/cons/BlueHat")
        release_torrent_path("/mnt/archive/cons/BlueHat")
        with patch("infocon_scraper.os.path.exists", return_value=False), \
                patch("infocon_scraper.os.makedirs"), \
                patch("infocon_scraper.os.path.getsize", return_value=5_000_000), \
                patch("infocon_scraper.has_free_space", return_value=True), \
                patch("infocon_scraper.download_atomic") as dl, \
                patch("infocon_scraper.schedule_hash"):
            result, _ = infocon_scraper.sync_file(
                self.item, "/mnt/archive", Manifest(":memory:"),
                verify_all=False, dry_run=False)
        dl.assert_called_once()
        self.assertEqual(result, "downloaded")

    def test_sparse_leftover_is_discarded_rather_than_catalogued(self):
        """The silent-corruption path: full size, mostly holes, recorded as good."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = tmp.name
        target = os.path.join(dest, "cons", "BlueHat")
        os.makedirs(target)
        path = os.path.join(target, "talk.mp4")
        with open(path, "wb") as handle:
            handle.truncate(5_000_000)
            handle.write(b"\xff" * 4096)

        def fake_download(url, local_path, expected_size, slack=0):
            with open(local_path, "wb") as handle:
                handle.write(b"\xff" * expected_size)

        with patch("infocon_scraper.has_free_space", return_value=True), \
                patch("infocon_scraper.download_atomic", side_effect=fake_download) as dl, \
                patch("infocon_scraper.schedule_hash"), \
                patch("infocon_scraper.sha256_file", side_effect=AssertionError("hashed a sparse file")):
            result, _ = infocon_scraper.sync_file(
                self.item, dest, Manifest(":memory:"), verify_all=False, dry_run=False)

        dl.assert_called_once()
        self.assertEqual(result, "downloaded")
        # Replaced with a genuinely allocated file, not the sparse husk.
        st = os.stat(path)
        self.assertGreaterEqual(st.st_blocks * 512, st.st_size * 0.95)


if __name__ == "__main__":
    unittest.main(verbosity=2)
