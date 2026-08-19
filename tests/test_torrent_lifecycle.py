"""
Tests for torrent admission, fast-resume checkpoints, and progress reporting.

Covers four defects:

  1. No resume data was ever written, so every restart re-hash-checked the
     whole torrent set - terabytes on a populated drive - before a single byte
     could transfer.
  2. All torrents were added to the session at once, so all of them
     hash-checked at once against one disk.
  3. The status block printed every incomplete torrent every poll: ~2,500 log
     lines a minute at 293 torrents on a 10s interval.
  4. Progress counted bytes only when a file completed, so a run pulling
     multi-gigabyte archives reported 0 B/s for hours.

Run with:
    python -m pytest tests/test_torrent_lifecycle.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infocon_scraper
import fetch_defcon_torrents
from fetch_defcon_torrents import (
    TorrentSpec,
    checkpoint_before_exit,
    handle_key,
    load_add_params,
    resume_file_path,
    save_resume_alerts,
)


SPEC = TorrentSpec(name="DEF CON 30",
                   url="https://media.defcon.org/DEF%20CON%20Torrents/DEF%20CON%2030%20v2.torrent",
                   save_path="/mnt/archive/cons/DEF CON")


# ---------------------------------------------------------------------------
# 1. Fast-resume checkpoints
# ---------------------------------------------------------------------------

class TestResumePaths(unittest.TestCase):

    def test_path_is_stable_for_a_spec(self):
        first = resume_file_path("/cache/resume", SPEC)
        second = resume_file_path("/cache/resume", SPEC)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("/cache/resume"))

    def test_specs_differing_only_by_save_path_do_not_collide(self):
        other = TorrentSpec(name=SPEC.name, url=SPEC.url, save_path="/mnt/other/cons/DEF CON")
        self.assertNotEqual(resume_file_path("/cache/resume", SPEC),
                            resume_file_path("/cache/resume", other))

    def test_path_has_no_separators_from_the_name(self):
        spec = TorrentSpec(name="a/b", url="u", save_path="/mnt/x")
        self.assertEqual(os.path.dirname(resume_file_path("/cache/resume", spec)), "/cache/resume")


class TestLoadAddParams(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.info = object()

    def test_missing_resume_data_starts_fresh(self):
        with patch.object(fetch_defcon_torrents.lt, "read_resume_data") as read, \
                patch.object(fetch_defcon_torrents.lt, "add_torrent_params", MagicMock):
            atp = load_add_params(SPEC, self.info, self.tmp.name)
        read.assert_not_called()
        self.assertIs(atp.ti, self.info)
        self.assertEqual(atp.save_path, SPEC.save_path)

    def test_existing_resume_data_is_used(self):
        path = resume_file_path(self.tmp.name, SPEC)
        with open(path, "wb") as handle:
            handle.write(b"resume-blob")
        with patch.object(fetch_defcon_torrents.lt, "read_resume_data",
                          return_value=MagicMock()) as read:
            atp = load_add_params(SPEC, self.info, self.tmp.name)
        read.assert_called_once_with(b"resume-blob")
        self.assertIs(atp.ti, self.info)
        self.assertEqual(atp.save_path, SPEC.save_path)

    def test_corrupt_resume_data_falls_back_instead_of_failing(self):
        path = resume_file_path(self.tmp.name, SPEC)
        with open(path, "wb") as handle:
            handle.write(b"not a bencoded structure")
        with patch.object(fetch_defcon_torrents.lt, "read_resume_data",
                          side_effect=RuntimeError("invalid")), \
                patch.object(fetch_defcon_torrents.lt, "add_torrent_params", MagicMock):
            atp = load_add_params(SPEC, self.info, self.tmp.name)
        self.assertEqual(atp.save_path, SPEC.save_path)


class TestSaveResumeAlerts(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.tmp.name, "dc30.resume")

    def alert(self, key="hash-1"):
        alert = MagicMock(spec=fetch_defcon_torrents.lt.save_resume_data_alert)
        alert.handle.info_hash.return_value = key
        alert.params = object()
        return alert

    def test_alert_is_written_to_its_torrents_file(self):
        session = MagicMock()
        session.pop_alerts.return_value = [self.alert()]
        with patch.object(fetch_defcon_torrents.lt, "write_resume_data_buf", return_value=b"blob"):
            saved = save_resume_alerts(session, {"hash-1": self.target})
        self.assertEqual(saved, 1)
        with open(self.target, "rb") as handle:
            self.assertEqual(handle.read(), b"blob")

    def test_write_is_atomic(self):
        """A crash mid-write must not leave a truncated resume file behind."""
        session = MagicMock()
        session.pop_alerts.return_value = [self.alert()]
        with patch.object(fetch_defcon_torrents.lt, "write_resume_data_buf", return_value=b"blob"):
            save_resume_alerts(session, {"hash-1": self.target})
        self.assertFalse(os.path.exists(self.target + ".tmp"))

    def test_unknown_handle_is_ignored(self):
        session = MagicMock()
        session.pop_alerts.return_value = [self.alert(key="unmapped")]
        with patch.object(fetch_defcon_torrents.lt, "write_resume_data_buf", return_value=b"blob"):
            self.assertEqual(save_resume_alerts(session, {"hash-1": self.target}), 0)
        self.assertFalse(os.path.exists(self.target))

    def test_unrelated_alerts_are_skipped(self):
        session = MagicMock()
        session.pop_alerts.return_value = [MagicMock()]
        self.assertEqual(save_resume_alerts(session, {"hash-1": self.target}), 0)

    def test_invalid_handle_yields_an_empty_key(self):
        handle = MagicMock()
        handle.info_hash.side_effect = RuntimeError("detached")
        self.assertEqual(handle_key(handle), "")


class TestCheckpointBeforeExit(unittest.TestCase):

    def test_all_torrents_are_paused_and_checkpointed(self):
        handles = {"a": MagicMock(), "b": MagicMock()}
        session = MagicMock()
        requested = []
        with patch("fetch_defcon_torrents.save_resume_alerts", return_value=2) as saver:
            checkpoint_before_exit(session, handles, {}, lambda: requested.append(True))
        for handle in handles.values():
            handle.pause.assert_called_once()
        self.assertEqual(requested, [True])
        saver.assert_called()

    def test_no_handles_is_a_no_op(self):
        session = MagicMock()
        with patch("fetch_defcon_torrents.save_resume_alerts") as saver:
            checkpoint_before_exit(session, {}, {}, lambda: None)
        saver.assert_not_called()

    def test_gives_up_rather_than_hanging(self):
        """A torrent that never answers must not block shutdown forever."""
        handles = {"a": MagicMock()}
        with patch("fetch_defcon_torrents.save_resume_alerts", return_value=0):
            start = __import__("time").monotonic()
            checkpoint_before_exit(MagicMock(), handles, {}, lambda: None, timeout=0.5)
            self.assertLess(__import__("time").monotonic() - start, 5.0)


# ---------------------------------------------------------------------------
# 4. In-flight byte accounting
# ---------------------------------------------------------------------------

class TestInflightBytes(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.part = os.path.join(self.tmp.name, "big.rar.part")
        self.addCleanup(infocon_scraper.unregister_part, self.part)

    def write(self, size: int):
        with open(self.part, "wb") as handle:
            handle.write(b"\0" * size)

    def test_partial_transfer_counts_towards_progress(self):
        """This is what used to report 0 B/s while gigabytes were moving."""
        self.write(0)
        infocon_scraper.register_part(self.part, 0)
        self.write(5000)
        self.assertEqual(infocon_scraper.inflight_bytes(), 5000)

    def test_resumed_transfer_only_counts_new_bytes(self):
        self.write(1000)
        infocon_scraper.register_part(self.part, 1000)
        self.write(2500)
        self.assertEqual(infocon_scraper.inflight_bytes(), 1500)

    def test_completed_transfer_stops_counting(self):
        self.write(4000)
        infocon_scraper.register_part(self.part, 0)
        infocon_scraper.unregister_part(self.part)
        self.assertEqual(infocon_scraper.inflight_bytes(), 0)

    def test_vanished_part_file_is_tolerated(self):
        infocon_scraper.register_part(self.part, 0)
        self.assertEqual(infocon_scraper.inflight_bytes(), 0)

    def test_truncated_part_never_reports_negative(self):
        self.write(4000)
        infocon_scraper.register_part(self.part, 8000)
        self.assertEqual(infocon_scraper.inflight_bytes(), 0)

    def test_reporter_line_includes_in_flight_bytes(self):
        self.write(0)
        infocon_scraper.register_part(self.part, 0)
        self.write(2_000_000)
        stats = infocon_scraper.ProgressStats()
        stats.add_discovered(10)
        reporter = infocon_scraper.StatusReporter(stats, interval=1.0)
        reporter.is_tty = False
        with patch.object(infocon_scraper.log, "info") as logged:
            reporter._emit()
        line = logged.call_args[0][0]
        self.assertIn("2.0 MB", line)
        self.assertNotIn("@ 0 B/s", line)


# ---------------------------------------------------------------------------
# 3. Status output volume
# ---------------------------------------------------------------------------

class TestStatusLineBudget(unittest.TestCase):

    def test_setting_defaults_to_a_bounded_number_of_lines(self):
        settings = fetch_defcon_torrents.TorrentSettings(
            max_active=4, connections=800, listen_interface="0.0.0.0:6881", poll_seconds=10,
            seed_time=0, enable_dht=True, enable_pex=True, enable_lsd=True,
            request_timeout=120, retries=3, retry_delay=3,
        )
        self.assertEqual(settings.status_lines, 10)
        self.assertEqual(settings.resume_save_minutes, 5)

    def test_status_lines_is_configurable(self):
        settings = fetch_defcon_torrents.TorrentSettings(
            max_active=4, connections=800, listen_interface="0.0.0.0:6881", poll_seconds=10,
            seed_time=0, enable_dht=True, enable_pex=True, enable_lsd=True,
            request_timeout=120, retries=3, retry_delay=3, status_lines=0,
        )
        self.assertEqual(settings.status_lines, 0)


# ---------------------------------------------------------------------------
# 2/5. Stop event
# ---------------------------------------------------------------------------

class TestStopEventPlumbing(unittest.TestCase):

    def test_fetch_all_accepts_a_stop_event(self):
        """SIGTERM used to be unable to reach the torrent poll loop at all."""
        import inspect

        signature = inspect.signature(fetch_defcon_torrents.fetch_all)
        self.assertIn("stop_event", signature.parameters)

    def test_scraper_passes_its_stop_event_through(self):
        import inspect

        signature = inspect.signature(infocon_scraper.run_defcon_torrent_step)
        self.assertIn("stop_event", signature.parameters)
        source = inspect.getsource(infocon_scraper.main)
        self.assertIn("stop_event=stop_requested", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
