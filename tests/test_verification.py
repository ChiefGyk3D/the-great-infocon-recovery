"""
Tests for verifying local files against the publisher's piece hashes.

A SHA-256 in the manifest only proves a file has not changed since it was first
seen. It cannot prove the file matches what was published, because neither
archive host sends Content-Length and the directory listing's size is rounded -
at gigabyte scale that tolerance is about 107 MB, so a materially short file
passes every check the HTTP sync is able to make.

The torrents carry the publisher's own piece hashes, which settles it.

These are integration tests against real torrents built on the fly rather than
mocks: libtorrent is a Boost.Python extension that rejects duck-typed stand-ins,
and faking it would test the fake rather than the behaviour that matters.

Run with:
    python -m pytest tests/test_verification.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import libtorrent as lt
except ImportError as exc:  # pragma: no cover - CI without prebuilt wheels
    raise unittest.SkipTest(f"libtorrent unavailable: {exc}") from exc

from fetch_defcon_torrents import TorrentSettings, TorrentSpec, _torrent_file_for, verify_archives


def settings() -> TorrentSettings:
    return TorrentSettings(
        max_active=1, connections=20, listen_interface="0.0.0.0:0", poll_seconds=1,
        seed_time=0, enable_dht=False, enable_pex=False, enable_lsd=False,
        request_timeout=60, retries=1, retry_delay=1,
    )


SPEC = TorrentSpec(name="2600 archive",
                   url="https://infocon.org/cons/2600%20archive%20v1.torrent",
                   save_path="/mnt/archive/cons")


class TestTorrentFileResolution(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def cache_path(self) -> str:
        with patch("fetch_defcon_torrents.curl_download"):
            return _torrent_file_for(SPEC, self.tmp.name, settings())

    def test_existing_cache_entry_is_reused_without_fetching(self):
        path = self.cache_path()
        with open(path, "wb") as handle:
            handle.write(b"d4:infod")
        with patch("fetch_defcon_torrents.curl_download",
                   side_effect=AssertionError("refetched a cached torrent")):
            self.assertEqual(_torrent_file_for(SPEC, self.tmp.name, settings()), path)

    def test_missing_cache_entry_is_fetched(self):
        with patch("fetch_defcon_torrents.curl_download") as dl:
            _torrent_file_for(SPEC, self.tmp.name, settings())
        dl.assert_called_once()

    def test_unfetchable_torrent_yields_none_rather_than_raising(self):
        with patch("fetch_defcon_torrents.curl_download", side_effect=RuntimeError("404")):
            self.assertIsNone(_torrent_file_for(SPEC, self.tmp.name, settings()))

    def test_cache_name_contains_no_path_separators(self):
        self.assertEqual(os.path.dirname(self.cache_path()), self.tmp.name)


class VerificationFixture(unittest.TestCase):
    """Builds a real torrent over real files, then verifies them."""

    PIECE = 16 * 1024

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.drive = os.path.join(self.tmp.name, "drive")
        self.save_path = os.path.join(self.drive, "cons")
        self.content = os.path.join(self.save_path, "payload")
        os.makedirs(self.content)
        self.cache = os.path.join(self.tmp.name, "cache")
        os.makedirs(self.cache)

        self.files = {"talk0.mp4": b"A" * 40000, "talk1.mp4": b"B" * 70000}
        for name, blob in self.files.items():
            with open(os.path.join(self.content, name), "wb") as handle:
                handle.write(blob)

        storage = lt.file_storage()
        lt.add_files(storage, self.content)
        builder = lt.create_torrent(storage, piece_size=self.PIECE)
        lt.set_piece_hashes(builder, self.save_path)
        self.torrent_path = os.path.join(self.cache, "payload.torrent")
        with open(self.torrent_path, "wb") as handle:
            handle.write(lt.bencode(builder.generate()))

        self.spec = TorrentSpec(name="payload", url="https://example.invalid/x.torrent",
                                save_path=self.save_path)

    def verify(self, **kwargs):
        with patch("fetch_defcon_torrents._torrent_file_for", return_value=self.torrent_path):
            return verify_archives([self.spec], self.cache, settings(), **kwargs)

    def truncate(self, name: str, size: int) -> None:
        with open(os.path.join(self.content, name), "r+b") as handle:
            handle.truncate(size)


class TestVerifyIntact(VerificationFixture):

    def test_intact_archive_verifies_completely(self):
        report = self.verify()
        archive = report["archives"][0]
        self.assertTrue(archive["complete"])
        self.assertEqual(archive["incomplete_files"], [])
        # A v2 torrent's total_size includes the pad files libtorrent inserts
        # to align files to piece boundaries, so compare against the torrent.
        self.assertEqual(archive["verified_bytes"], archive["total_bytes"])
        self.assertGreaterEqual(archive["verified_bytes"],
                                sum(len(b) for b in self.files.values()))

    def test_totals_are_accumulated(self):
        report = self.verify()
        self.assertEqual(report["totals"]["archives"], 1)
        self.assertEqual(report["totals"]["incomplete_files"], 0)
        self.assertEqual(report["totals"]["falsely_catalogued"], 0)

    def test_nothing_is_deleted_by_verifying(self):
        self.verify()
        for name, blob in self.files.items():
            self.assertEqual(os.path.getsize(os.path.join(self.content, name)), len(blob))


class TestVerifyDamaged(VerificationFixture):

    def test_truncated_file_is_detected(self):
        """Size-with-slack cannot catch this; piece hashes can."""
        self.truncate("talk1.mp4", 20000)
        report = self.verify()
        archive = report["archives"][0]
        self.assertFalse(archive["complete"])
        names = [entry["file"] for entry in archive["incomplete_files"]]
        self.assertTrue(any("talk1.mp4" in n for n in names), names)

    def test_corrupted_content_is_detected_even_at_the_right_size(self):
        """The case a size check can never see: right length, wrong bytes."""
        path = os.path.join(self.content, "talk0.mp4")
        original = os.path.getsize(path)
        with open(path, "r+b") as handle:
            handle.seek(0)
            handle.write(b"Z" * 30000)
        self.assertEqual(os.path.getsize(path), original)
        report = self.verify()
        self.assertFalse(report["archives"][0]["complete"])

    def test_missing_file_is_detected(self):
        os.remove(os.path.join(self.content, "talk0.mp4"))
        report = self.verify()
        self.assertGreaterEqual(len(report["archives"][0]["incomplete_files"]), 1)

    def test_report_is_written_as_json(self):
        self.truncate("talk1.mp4", 20000)
        path = os.path.join(self.tmp.name, "report.json")
        self.verify(report_path=path)
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertGreaterEqual(saved["totals"]["incomplete_files"], 1)


class TestManifestCrossReference(VerificationFixture):

    def manifest_with(self, rel: str) -> str:
        path = os.path.join(self.drive, ".infocon_manifest.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS entries (rel TEXT PRIMARY KEY, size INTEGER, "
                     "sha256 TEXT, url TEXT, mtime REAL, verified REAL)")
        conn.execute("INSERT OR REPLACE INTO entries (rel, size, sha256) VALUES (?,?,?)",
                     (rel, 70000, "deadbeef"))
        conn.commit()
        conn.close()
        return path

    def test_incomplete_file_recorded_as_verified_is_flagged(self):
        """The exact failure this mode exists to catch: the manifest says good,
        the publisher's piece hashes say incomplete."""
        self.truncate("talk1.mp4", 20000)
        manifest = self.manifest_with(os.path.join("cons", "payload", "talk1.mp4"))
        report = self.verify(manifest_path=manifest)
        self.assertEqual(report["totals"]["falsely_catalogued"], 1)
        flagged = [e for e in report["archives"][0]["incomplete_files"]
                   if e.get("catalogued_as_good")]
        self.assertEqual(len(flagged), 1)

    def test_incomplete_file_absent_from_the_manifest_is_not_flagged(self):
        self.truncate("talk1.mp4", 20000)
        manifest = self.manifest_with("cons/payload/something-else.mp4")
        report = self.verify(manifest_path=manifest)
        self.assertEqual(report["totals"]["falsely_catalogued"], 0)
        self.assertGreaterEqual(report["totals"]["incomplete_files"], 1)

    def test_missing_manifest_is_not_fatal(self):
        self.truncate("talk1.mp4", 20000)
        report = self.verify(manifest_path=os.path.join(self.tmp.name, "nope.db"))
        self.assertGreaterEqual(report["totals"]["incomplete_files"], 1)


class TestVerificationControl(VerificationFixture):

    def test_stop_event_ends_verification_early(self):
        stop = threading.Event()
        stop.set()
        report = self.verify(stop_event=stop)
        self.assertEqual(report["totals"]["archives"], 0)

    def test_unloadable_torrent_is_skipped_not_fatal(self):
        bad = os.path.join(self.cache, "broken.torrent")
        with open(bad, "wb") as handle:
            handle.write(b"not a torrent")
        with patch("fetch_defcon_torrents._torrent_file_for", return_value=bad):
            report = verify_archives([self.spec], self.cache, settings())
        self.assertEqual(report["totals"]["archives"], 0)

    def test_unavailable_torrent_metadata_is_skipped(self):
        with patch("fetch_defcon_torrents._torrent_file_for", return_value=None):
            report = verify_archives([self.spec], self.cache, settings())
        self.assertEqual(report["totals"]["archives"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
