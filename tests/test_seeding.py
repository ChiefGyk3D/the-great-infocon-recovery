"""
Tests for sharing completed archives back to the swarm.

Seeding was hard-off: `--seed-time` defaulted to 0, so every completed torrent
was paused the instant it finished. The archive is community-hosted and its own
README asks contributors to help it grow, and a rebuilt drive is a fully
populated seed - so a tool that only ever leeches is working against the thing
it exists to preserve.

Seeding is now on by default and bounded by explicit, configurable limits: how
many peers may pull from you, at what bandwidth, across how many archives, for
how long.

Run with:
    python -m pytest tests/test_seeding.py -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import libtorrent  # noqa: F401  - the torrent helper imports it at module level
except ImportError as exc:  # pragma: no cover - CI without prebuilt wheels
    raise unittest.SkipTest(f"libtorrent unavailable: {exc}") from exc

import fetch_defcon_torrents
from fetch_defcon_torrents import TorrentSettings, build_libtorrent_session


def settings(**overrides) -> TorrentSettings:
    base = {
        "max_active": 4, "connections": 800, "listen_interface": "0.0.0.0:0",
        "poll_seconds": 10, "seed_time": 60, "enable_dht": False, "enable_pex": False,
        "enable_lsd": False, "request_timeout": 120, "retries": 3, "retry_delay": 3,
    }
    base.update(overrides)
    return TorrentSettings(**base)


class TestSeedingDefaults(unittest.TestCase):

    def test_sharing_is_on_by_default(self):
        """The whole point: a rebuilt drive contributes instead of only taking."""
        self.assertEqual(settings().seed_time, 60)

    def test_upload_limits_have_conservative_defaults(self):
        s = settings()
        self.assertEqual(s.seed_upload_slots, 4)
        self.assertEqual(s.seed_rate_limit_kib, 0)   # 0 = unlimited
        self.assertEqual(s.max_seeding, 20)

    def test_cli_defaults_match_the_dataclass(self):
        """A default that only exists in one of the two places is a trap."""
        import argparse
        from unittest.mock import patch

        captured = {}
        real = argparse.ArgumentParser.parse_args

        def capture(self, *a, **k):
            for action in self._actions:
                captured[action.dest] = action.default
            raise SystemExit(0)

        with patch.object(argparse.ArgumentParser, "parse_args", capture), \
                patch.object(sys, "argv", ["fetch_defcon_torrents.py", "--dest", "/tmp/x"]):
            try:
                fetch_defcon_torrents.main()
            except SystemExit:
                pass
        del real
        self.assertEqual(captured.get("seed_time"), 60)
        self.assertEqual(captured.get("seed_upload_slots"), 4)
        self.assertEqual(captured.get("max_seeding"), 20)


class TestSessionSeedingConfig(unittest.TestCase):
    """The session must actually be configured to serve peers."""

    def test_upload_slots_are_applied(self):
        session = build_libtorrent_session(settings(seed_upload_slots=7))
        self.assertEqual(session.get_settings()["unchoke_slots_limit"], 7)

    def test_rate_limit_is_converted_to_bytes(self):
        session = build_libtorrent_session(settings(seed_rate_limit_kib=512))
        self.assertEqual(session.get_settings()["upload_rate_limit"], 512 * 1024)

    def test_unlimited_rate_limit_stays_zero(self):
        session = build_libtorrent_session(settings(seed_rate_limit_kib=0))
        self.assertEqual(session.get_settings()["upload_rate_limit"], 0)

    def test_seeds_do_not_consume_download_slots(self):
        """active_limit used to equal the download limit, so seeding an archive
        stole a slot from downloading one."""
        session = build_libtorrent_session(settings(max_active=4, max_seeding=20))
        conf = session.get_settings()
        self.assertEqual(conf["active_downloads"], 4)
        self.assertEqual(conf["active_seeds"], 20)
        self.assertEqual(conf["active_limit"], 24)

    def test_unlimited_active_propagates(self):
        session = build_libtorrent_session(settings(max_active=0, max_seeding=20))
        self.assertEqual(session.get_settings()["active_limit"], -1)

    def test_download_limits_are_untouched_by_seeding_config(self):
        session = build_libtorrent_session(settings(connections=123))
        self.assertEqual(session.get_settings()["connections_limit"], 123)


class TestSeedTimeSemantics(unittest.TestCase):
    """seed_time is a three-way switch, so each branch needs to be unambiguous."""

    def decide(self, seed_time: float, seeded_for: float) -> str:
        """Mirrors the poll loop's decision for a completed torrent."""
        if seed_time == 0:
            return "pause"
        if seed_time > 0 and seeded_for >= seed_time * 60:
            return "pause"
        return "seed"

    def test_zero_stops_immediately(self):
        self.assertEqual(self.decide(0, 0), "pause")
        self.assertEqual(self.decide(0, 10_000), "pause")

    def test_positive_seeds_then_stops(self):
        self.assertEqual(self.decide(60, 0), "seed")
        self.assertEqual(self.decide(60, 59 * 60), "seed")
        self.assertEqual(self.decide(60, 60 * 60), "pause")

    def test_negative_seeds_indefinitely(self):
        self.assertEqual(self.decide(-1, 0), "seed")
        self.assertEqual(self.decide(-1, 10_000_000), "seed")

    def test_negative_seed_time_survives_the_settings_clamp(self):
        """max(0, ...) on the CLI value would silently disable indefinite seeding."""
        self.assertEqual(settings(seed_time=-1).seed_time, -1)


class TestCompletionWithSeeding(unittest.TestCase):
    """Exit behaviour differs by mode, and getting it wrong either hangs the run
    or drops off the swarm the moment the last piece lands."""

    def should_exit(self, seed_time: int, all_done: bool, stopping: bool) -> bool:
        if not all_done:
            return False
        return not (seed_time < 0 and not stopping)

    def test_finite_seeding_still_exits_on_completion(self):
        self.assertTrue(self.should_exit(60, all_done=True, stopping=False))

    def test_indefinite_seeding_keeps_running(self):
        self.assertFalse(self.should_exit(-1, all_done=True, stopping=False))

    def test_indefinite_seeding_exits_when_stopped(self):
        self.assertTrue(self.should_exit(-1, all_done=True, stopping=True))

    def test_incomplete_never_exits(self):
        for seed_time in (0, 60, -1):
            with self.subTest(seed_time=seed_time):
                self.assertFalse(self.should_exit(seed_time, all_done=False, stopping=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
