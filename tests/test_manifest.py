"""
Tests for the verification manifest.

It used to be a single JSON document held in memory and rewritten in full every
200 completions. Across the archive's ~450k files that means serialising tens of
megabytes thousands of times, under the same lock every worker needs to read
through - so the whole download pool stalled on each save, and the run wrote far
more to disk than it downloaded.

These tests cover the SQLite replacement and, importantly, that an established
drive's existing JSON manifest is imported rather than discarded.

Run with:
    python -m pytest tests/test_manifest.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infocon_scraper import Manifest, manifest_db_path


ENTRY = {"size": 4096, "sha256": "abc123", "url": "https://infocon.org/a.pdf",
         "mtime": 1_700_000_000.0, "verified": 1_700_000_500.0}


class TestManifestPath(unittest.TestCase):

    def test_json_path_maps_to_a_database(self):
        self.assertEqual(manifest_db_path("/mnt/x/.infocon_manifest.json"),
                         "/mnt/x/.infocon_manifest.db")

    def test_database_path_is_left_alone(self):
        self.assertEqual(manifest_db_path("/mnt/x/.infocon_manifest.db"),
                         "/mnt/x/.infocon_manifest.db")

    def test_memory_database_is_passed_through(self):
        self.assertEqual(manifest_db_path(":memory:"), ":memory:")


class TestManifestRoundTrip(unittest.TestCase):

    def setUp(self):
        self.manifest = Manifest(":memory:")
        self.addCleanup(self.manifest.close)

    def test_entry_survives_a_flush(self):
        self.manifest.set("cons/a.pdf", ENTRY)
        self.manifest.save()
        self.assertEqual(self.manifest.get("cons/a.pdf"), ENTRY)

    def test_entry_is_readable_before_being_flushed(self):
        """Workers must see their own writes without waiting for a save."""
        self.manifest.set("cons/a.pdf", ENTRY)
        self.assertEqual(self.manifest.get("cons/a.pdf"), ENTRY)

    def test_missing_entry_is_none(self):
        self.assertIsNone(self.manifest.get("cons/nope.pdf"))

    def test_entry_can_be_replaced(self):
        self.manifest.set("cons/a.pdf", ENTRY)
        self.manifest.save()
        self.manifest.set("cons/a.pdf", {**ENTRY, "size": 8192})
        self.manifest.save()
        self.assertEqual(self.manifest.get("cons/a.pdf")["size"], 8192)
        self.assertEqual(self.manifest.count(), 1)

    def test_partial_entry_omits_absent_fields(self):
        """A download records size and mtime before its hash is computed."""
        self.manifest.set("cons/b.bin", {"size": 10, "url": "u", "mtime": 5.0})
        self.manifest.save()
        stored = self.manifest.get("cons/b.bin")
        self.assertNotIn("sha256", stored)
        self.assertEqual(stored["size"], 10)

    def test_unicode_paths_round_trip(self):
        rel = "cons/Beyond Root/ünïcode – name.pdf"
        self.manifest.set(rel, ENTRY)
        self.manifest.save()
        self.assertEqual(self.manifest.get(rel), ENTRY)

    def test_returned_entry_is_a_copy(self):
        self.manifest.set("cons/a.pdf", dict(ENTRY))
        fetched = self.manifest.get("cons/a.pdf")
        fetched["size"] = 1
        self.assertEqual(self.manifest.get("cons/a.pdf")["size"], ENTRY["size"])


class TestManifestPersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, ".infocon_manifest.db")

    def test_entries_survive_reopening(self):
        manifest = Manifest(self.path)
        manifest.set("cons/a.pdf", ENTRY)
        manifest.close()

        reopened = Manifest(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get("cons/a.pdf"), ENTRY)

    def test_large_batches_flush_without_an_explicit_save(self):
        """Buffered writes must not grow without bound on a long run."""
        manifest = Manifest(self.path)
        self.addCleanup(manifest.close)
        for index in range(1200):
            manifest.set(f"cons/f{index}.bin", {**ENTRY, "size": index})
        self.assertLess(len(manifest._pending), 500)
        self.assertEqual(manifest.count(), 1200)

    def test_writes_are_incremental_not_a_full_rewrite(self):
        """The whole point: adding one entry must not rewrite every other."""
        manifest = Manifest(self.path)
        for index in range(2000):
            manifest.set(f"cons/f{index}.bin", ENTRY)
        manifest.save()
        manifest.close()

        before = os.path.getsize(self.path)
        reopened = Manifest(self.path)
        self.addCleanup(reopened.close)
        reopened.set("cons/one-more.bin", ENTRY)
        reopened.save()
        self.assertEqual(reopened.count(), 2001)
        # A full rewrite of 2000 entries would move far more than a few pages.
        self.assertLess(abs(os.path.getsize(self.path) - before), before / 2 + 65536)

    def test_concurrent_writers_do_not_corrupt_the_manifest(self):
        manifest = Manifest(self.path)
        self.addCleanup(manifest.close)

        def worker(base: int) -> None:
            for index in range(200):
                manifest.set(f"cons/w{base}-{index}.bin", {**ENTRY, "size": index})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(manifest.count(), 800)


class TestLegacyJsonMigration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.json_path = os.path.join(self.tmp.name, ".infocon_manifest.json")
        self.db_path = os.path.join(self.tmp.name, ".infocon_manifest.db")

    def write_legacy(self, data: dict) -> None:
        with open(self.json_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_existing_json_manifest_is_imported(self):
        """An established drive must not lose the hashes it already recorded."""
        self.write_legacy({"cons/a.pdf": ENTRY, "cons/b.pdf": {**ENTRY, "size": 1}})
        manifest = Manifest(self.db_path)
        self.addCleanup(manifest.close)
        self.assertEqual(manifest.count(), 2)
        self.assertEqual(manifest.get("cons/a.pdf"), ENTRY)

    def test_import_happens_once(self):
        self.write_legacy({"cons/a.pdf": ENTRY})
        first = Manifest(self.db_path)
        first.set("cons/new.pdf", ENTRY)
        first.close()

        # Re-opening must not re-import over newer records.
        second = Manifest(self.db_path)
        self.addCleanup(second.close)
        self.assertEqual(second.count(), 2)

    def test_json_path_argument_is_accepted_and_redirected(self):
        self.write_legacy({"cons/a.pdf": ENTRY})
        manifest = Manifest(self.json_path)
        self.addCleanup(manifest.close)
        self.assertEqual(manifest.path, self.db_path)
        self.assertTrue(os.path.exists(self.db_path))
        self.assertEqual(manifest.get("cons/a.pdf"), ENTRY)

    def test_corrupt_json_does_not_prevent_startup(self):
        with open(self.json_path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        manifest = Manifest(self.db_path)
        self.addCleanup(manifest.close)
        self.assertEqual(manifest.count(), 0)

    def test_no_legacy_file_is_fine(self):
        manifest = Manifest(self.db_path)
        self.addCleanup(manifest.close)
        self.assertEqual(manifest.count(), 0)

    def test_database_is_valid_sqlite(self):
        self.write_legacy({"cons/a.pdf": ENTRY})
        manifest = Manifest(self.db_path)
        manifest.close()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT rel, size, sha256 FROM entries").fetchall()
        self.assertEqual(rows, [("cons/a.pdf", ENTRY["size"], ENTRY["sha256"])])


if __name__ == "__main__":
    unittest.main(verbosity=2)
