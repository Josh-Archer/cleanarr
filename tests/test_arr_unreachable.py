"""Unreachable Radarr/Sonarr: retry with backoff, then skip without failing."""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)
os.environ["CLEANARR_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "cleanarr_test.log")

from cleanarr import cleanup as cleanarr  # noqa: E402
from cleanarr.adapters.servarr import ServarrClient  # noqa: E402


def _ok(payload):
    res = requests.Response()
    res.status_code = 200
    res._content = payload
    return res


def _client(side_effect):
    session = MagicMock()
    session.get.side_effect = side_effect
    client = ServarrClient("Radarr", {"baseurl": "http://r", "apikey": "k"}, session=session)
    return client, session


class TestServarrRetry(unittest.TestCase):
    def test_connection_refused_retries_with_backoff_then_flags_unreachable(self):
        client, session = _client(requests.exceptions.ConnectionError("Connection refused"))
        with patch("cleanarr.adapters.servarr.time.sleep") as sleep:
            self.assertIsNone(client.request("tag"))
        self.assertEqual(session.get.call_count, 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.5, 3.0, 6.0, 12.0])
        self.assertTrue(client.last_failure_unreachable)

    def test_timeout_flags_unreachable(self):
        client, session = _client(requests.exceptions.ReadTimeout("timed out"))
        with patch("cleanarr.adapters.servarr.time.sleep"):
            self.assertIsNone(client.request("tag"))
        self.assertEqual(session.get.call_count, 5)
        self.assertTrue(client.last_failure_unreachable)

    def test_recovers_after_transient_failures(self):
        client, session = _client([
            requests.exceptions.ConnectionError("refused"),
            requests.exceptions.ReadTimeout("timeout"),
            _ok(b'[{"id": 1, "label": "x"}]'),
        ])
        with patch("cleanarr.adapters.servarr.time.sleep"):
            self.assertEqual(client.request("tag"), [{"id": 1, "label": "x"}])
        self.assertEqual(session.get.call_count, 3)
        self.assertFalse(client.last_failure_unreachable)

    def test_bad_request_is_not_unreachable_and_not_retried(self):
        res = requests.Response()
        res.status_code = 401
        res._content = b"nope"
        client, session = _client([res])
        with patch("cleanarr.adapters.servarr.time.sleep"):
            self.assertIsNone(client.request("tag"))
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(client.last_failure_unreachable)

    def test_retry_attempts_env_override(self):
        client, session = _client(requests.exceptions.ConnectionError("refused"))
        with patch.dict(os.environ, {"ARR_RETRY_ATTEMPTS": "2"}), \
                patch("cleanarr.adapters.servarr.time.sleep"):
            client.request("tag")
        self.assertEqual(session.get.call_count, 2)


class TestCleanupSkipsWhenArrUnreachable(unittest.TestCase):
    def setUp(self):
        self.config_patcher = patch.dict(cleanarr.CONFIG, {
            "plex": {"baseurl": "http://p", "token": "t"},
            "sonarr": {"baseurl": "http://s", "apikey": "k"},
            "radarr": {"baseurl": "http://r", "apikey": "k"},
            "transmission": {"host": "t", "port": 9091, "username": "u", "password": "p", "rpc_timeout_seconds": 90},
            "dry_run": False,
        })
        self.config_patcher.start()
        self.p1 = patch("cleanarr.cleanup.PlexServer")
        self.p2 = patch("cleanarr.cleanup.TransmissionClient")
        self.p1.start()
        self.p2.start()
        self.cleanup = cleanarr.MediaCleanup()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.config_patcher.stop()

    def _down(self, client):
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectionError("Connection refused")
        client._session = session
        return session

    def test_radarr_down_skips_movies_without_deleting_or_failing(self):
        self._down(self.cleanup.radarr_client)
        self.cleanup.get_watched_movies = MagicMock(
            return_value=[{"title": "M", "year": 2020, "file": "/m.mkv", "watched_by": {"u": True}}]
        )
        self.cleanup.delete_radarr_movie = MagicMock()
        self.cleanup.delete_radarr_movie_file = MagicMock()
        with patch("cleanarr.adapters.servarr.time.sleep"):
            self.cleanup.process_watched_movies()
        self.cleanup.delete_radarr_movie.assert_not_called()
        self.cleanup.delete_radarr_movie_file.assert_not_called()
        self.assertEqual(self.cleanup.run_summary.get("errors", []), [])
        self.assertEqual(len(self.cleanup.run_summary["arr_unavailable_skips"]), 1)
        result = self.cleanup._build_result()
        self.assertEqual(result.exit_code(), 0)

    def test_sonarr_down_skips_episodes_without_failing(self):
        self._down(self.cleanup.sonarr_client)
        self.cleanup.get_watched_episodes = MagicMock(return_value=[])
        self.cleanup.delete_sonarr_episode_file = MagicMock()
        with patch("cleanarr.adapters.servarr.time.sleep"):
            self.cleanup.process_watched_episodes()
        self.cleanup.delete_sonarr_episode_file.assert_not_called()
        self.assertEqual(self.cleanup.run_summary.get("errors", []), [])
        self.assertEqual(self.cleanup._build_result().exit_code(), 0)

    def test_non_connectivity_tag_failure_still_fails_closed(self):
        res = requests.Response()
        res.status_code = 401
        res._content = b"unauthorized"
        session = MagicMock()
        session.get.return_value = res
        self.cleanup.radarr_client._session = session
        self.cleanup.get_watched_movies = MagicMock(return_value=[])
        self.cleanup.process_watched_movies()
        self.assertEqual(len(self.cleanup.run_summary["errors"]), 1)
        self.assertEqual(self.cleanup._build_result().exit_code(), 1)

    def test_recovery_after_refusals_processes_normally(self):
        session = MagicMock()
        session.get.side_effect = [
            requests.exceptions.ConnectionError("refused"),
            requests.exceptions.ConnectionError("refused"),
            _ok(b"[]"),
        ]
        self.cleanup.radarr_client._session = session
        self.cleanup.get_watched_movies = MagicMock(return_value=[])
        self.cleanup._radarr_managed_movie_roots = MagicMock(return_value=[])
        with patch("cleanarr.adapters.servarr.time.sleep"):
            self.cleanup.process_watched_movies()
        self.assertNotIn("arr_unavailable_skips", self.cleanup.run_summary)
        self.assertEqual(self.cleanup.run_summary.get("errors", []), [])


class TestUnreachableSkipPreservesGraceState(unittest.TestCase):
    """Issue #95: with CLEANARR_GRACE_DAYS>0, an unreachable skip must not prune grace state."""

    def setUp(self):
        self.state_file = os.path.join(
            tempfile.mkdtemp(prefix="cleanarr-grace-"), "leaving-soon-state.json"
        )
        self.config_patcher = patch.dict(cleanarr.CONFIG, {
            "plex": {"baseurl": "http://p", "token": "t"},
            "sonarr": {"baseurl": "http://s", "apikey": "k"},
            "radarr": {"baseurl": "http://r", "apikey": "k"},
            "transmission": {"host": "t", "port": 9091, "username": "u", "password": "p", "rpc_timeout_seconds": 90},
            "dry_run": False,
            "grace_days": 7,
            "leaving_soon_state_file": self.state_file,
            "leaving_soon_collection": None,
        })
        self.config_patcher.start()
        self.p1 = patch("cleanarr.cleanup.PlexServer")
        self.p2 = patch("cleanarr.cleanup.TransmissionClient")
        self.p1.start()
        self.p2.start()
        self.cleanup = cleanarr.MediaCleanup()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.config_patcher.stop()

    def _down(self, client):
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ConnectionError("Connection refused")
        client._session = session
        return session

    def _seed_state(self, entry):
        import json as _json

        with open(self.state_file, "w", encoding="utf-8") as state_file:
            _json.dump(entry, state_file)
        return entry

    def _read_state(self):
        import json as _json

        with open(self.state_file, encoding="utf-8") as state_file:
            return _json.load(state_file)

    def test_radarr_down_with_grace_days_keeps_grace_clock(self):
        from datetime import datetime, timezone

        seeded = self._seed_state(
            {"plex:123": (datetime.now(timezone.utc)).isoformat()}
        )
        self._down(self.cleanup.radarr_client)
        self.cleanup.get_watched_movies = MagicMock(
            return_value=[{"title": "M", "year": 2020, "file": "/m.mkv", "watched_by": {"u": True}}]
        )
        self.cleanup.delete_radarr_movie = MagicMock()
        self.cleanup.delete_radarr_movie_file = MagicMock()
        with patch("cleanarr.adapters.servarr.time.sleep"):
            self.cleanup.process_watched_movies()
            self.cleanup._sync_leaving_soon()
        self.cleanup.delete_radarr_movie.assert_not_called()
        self.cleanup.delete_radarr_movie_file.assert_not_called()
        self.assertEqual(self.cleanup.run_summary.get("errors", []), [])
        self.assertEqual(len(self.cleanup.run_summary["arr_unavailable_skips"]), 1)
        self.assertEqual(self._read_state(), seeded)
        self.assertEqual(self.cleanup._build_result().exit_code(), 0)

    def test_sonarr_down_with_grace_days_keeps_grace_clock(self):
        from datetime import datetime, timezone

        seeded = self._seed_state(
            {"plex:456": (datetime.now(timezone.utc)).isoformat()}
        )
        self._down(self.cleanup.sonarr_client)
        self.cleanup.get_watched_episodes = MagicMock(return_value=[])
        self.cleanup.delete_sonarr_episode_file = MagicMock()
        with patch("cleanarr.adapters.servarr.time.sleep"):
            self.cleanup.process_watched_episodes()
            self.cleanup._sync_leaving_soon()
        self.cleanup.delete_sonarr_episode_file.assert_not_called()
        self.assertEqual(self.cleanup.run_summary.get("errors", []), [])
        self.assertEqual(len(self.cleanup.run_summary["arr_unavailable_skips"]), 1)
        self.assertEqual(self._read_state(), seeded)
        self.assertEqual(self.cleanup._build_result().exit_code(), 0)


if __name__ == "__main__":
    unittest.main()
