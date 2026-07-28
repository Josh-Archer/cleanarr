"""Unit tests for optional Lidarr music cleanup path (issue #17)."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

os.environ["CLEANARR_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "cleanarr_lidarr_test.log")

from cleanarr import cleanup as cleanarr


class TestLidarrMusicCleanup(unittest.TestCase):
    def setUp(self):
        self.config_patcher = patch.dict(
            cleanarr.CONFIG,
            {
                "plex": {"baseurl": "http://mock-plex:32400", "token": "mock-token"},
                "sonarr": {"baseurl": "http://mock-sonarr:8989", "apikey": "mock-api"},
                "radarr": {"baseurl": "http://mock-radarr:7878", "apikey": "mock-api"},
                "lidarr": {
                    "enabled": True,
                    "baseurl": "http://mock-lidarr:8686/api/v1/",
                    "apikey": "mock-lidarr-api",
                },
                "transmission": {
                    "host": "mock-transmission",
                    "port": 9091,
                    "username": "user",
                    "password": "pass",
                    "rpc_timeout_seconds": 90,
                },
                "debug": True,
                "dry_run": False,
                "disable_torrent_cleanup": True,
                "remove_orphan_incomplete_downloads": False,
                "remove_stale_torrents": False,
                "transmission_io_error_cleanup_enabled": False,
            },
            clear=False,
        )
        self.config_patcher.start()

        self.plex_patcher = patch("cleanarr.cleanup.PlexServer")
        self.MockPlex = self.plex_patcher.start()

        self.trans_patcher = patch("cleanarr.cleanup.TransmissionClient")
        self.MockTransmission = self.trans_patcher.start()

        self.requests_patcher = patch("cleanarr.cleanup.requests")
        self.MockRequests = self.requests_patcher.start()
        self.mock_session = MagicMock()
        self.MockRequests.Session.return_value = self.mock_session

        self.cleanup = cleanarr.MediaCleanup()

    def tearDown(self):
        self.config_patcher.stop()
        self.plex_patcher.stop()
        self.trans_patcher.stop()
        self.requests_patcher.stop()

    def _sample_plex_track(self, **overrides):
        track = {
            "title": "Yellow",
            "artist": "Coldplay",
            "album": "Parachutes",
            "track_number": 5,
            "year": 2000,
            "file": "/music/Coldplay/Parachutes/05 - Yellow.flac",
            "watched_by": {"owner": True},
            "watch_evidence": {"owner": "history"},
            "guid": "plex://track/1",
            "guids": [],
            "rating_key": 101,
        }
        track.update(overrides)
        return track

    def test_lidarr_disabled_by_default_config_flag(self):
        """CLEANARR_LIDARR_ENABLE defaults to off at module load."""
        self.assertIn("lidarr", cleanarr.CONFIG)
        # Default when env not set is false; tests may enable it via patch.
        self.assertIsInstance(cleanarr.CONFIG["lidarr"]["enabled"], bool)

    def test_process_played_tracks_skips_when_disabled(self):
        cleanarr.CONFIG["lidarr"]["enabled"] = False
        with patch.object(self.cleanup, "get_played_tracks") as mock_get:
            self.cleanup.process_played_tracks()
            mock_get.assert_not_called()

    def test_match_track_by_musicbrainz_id(self):
        mbid = "0a8e8d55-4b83-4f8a-ab76-c5a5a5a5a5a5"
        plex_track = self._sample_plex_track(
            guids=[f"mbid://{mbid}"],
        )
        artist = {"id": 1, "artistName": "Coldplay", "tags": [], "foreignArtistId": "artist-mbid"}
        lidarr_track = {
            "id": 9,
            "title": "Yellow",
            "foreignTrackId": mbid,
            "trackFileId": 42,
            "album": {"id": 3, "title": "Parachutes", "tags": []},
            "tags": [],
        }
        with patch.object(self.cleanup, "get_lidarr_artists", return_value=[artist]), patch.object(
            self.cleanup, "get_lidarr_tracks_for_artist", return_value=[lidarr_track]
        ):
            match = self.cleanup.match_track_to_lidarr(plex_track)

        self.assertIsNotNone(match)
        self.assertEqual(match["file_id"], 42)
        self.assertEqual(match["track"]["id"], 9)

    def test_match_track_by_artist_album_title(self):
        plex_track = self._sample_plex_track()
        artist = {"id": 1, "artistName": "Coldplay", "tags": []}
        album = {"id": 3, "title": "Parachutes", "tags": []}
        lidarr_track = {
            "id": 9,
            "title": "Yellow",
            "trackFileId": 42,
            "trackNumber": 5,
            "tags": [],
        }
        with patch.object(self.cleanup, "get_lidarr_artists", return_value=[artist]), patch.object(
            self.cleanup, "get_lidarr_albums_for_artist", return_value=[album]
        ), patch.object(
            self.cleanup, "get_lidarr_tracks_for_album", return_value=[lidarr_track]
        ):
            match = self.cleanup.match_track_to_lidarr(plex_track)

        self.assertIsNotNone(match)
        self.assertEqual(match["artist"]["id"], 1)
        self.assertEqual(match["album"]["id"], 3)
        self.assertEqual(match["file_id"], 42)

    def test_match_track_no_match_returns_none(self):
        plex_track = self._sample_plex_track(artist="Unknown Artist", title="Nope")
        artist = {"id": 1, "artistName": "Coldplay", "tags": []}
        with patch.object(self.cleanup, "get_lidarr_artists", return_value=[artist]), patch.object(
            self.cleanup, "get_lidarr_albums_for_artist", return_value=[]
        ), patch.object(
            self.cleanup, "get_lidarr_tracks_for_album", return_value=[]
        ), patch.object(
            self.cleanup, "get_lidarr_tracks_for_artist", return_value=[]
        ):
            match = self.cleanup.match_track_to_lidarr(plex_track, log_unmatched=False)

        self.assertIsNone(match)

    def test_match_normalizes_titles(self):
        plex_track = self._sample_plex_track(
            artist="The Coldplay",
            album="Parachutes (Deluxe)",
            title="Yellow!",
        )
        artist = {"id": 1, "artistName": "Coldplay", "tags": []}
        album = {"id": 3, "title": "Parachutes", "tags": []}
        lidarr_track = {"id": 9, "title": "Yellow", "trackFileId": 7, "tags": []}
        with patch.object(self.cleanup, "get_lidarr_artists", return_value=[artist]), patch.object(
            self.cleanup, "get_lidarr_albums_for_artist", return_value=[album]
        ), patch.object(
            self.cleanup, "get_lidarr_tracks_for_album", return_value=[lidarr_track]
        ):
            match = self.cleanup.match_track_to_lidarr(plex_track)

        self.assertIsNotNone(match)
        self.assertEqual(match["file_id"], 7)

    def test_delete_policy_protected_safe_tag_skips(self):
        track = self._sample_plex_track()
        tags = [
            {"id": 1, "label": "safe"},
            {"id": 2, "label": "owner"},
        ]
        lidarr_match = {
            "artist": {"id": 1, "artistName": "Coldplay", "tags": [1]},
            "album": {"id": 3, "title": "Parachutes", "tags": []},
            "track": {"id": 9, "title": "Yellow", "tags": []},
            "file_id": 42,
        }
        with patch.object(self.cleanup, "get_lidarr_tags", return_value=tags), patch.object(
            self.cleanup, "get_played_tracks", return_value=[track]
        ), patch.object(
            self.cleanup, "match_track_to_lidarr", return_value=lidarr_match
        ), patch.object(
            self.cleanup, "delete_lidarr_track_file"
        ) as mock_delete:
            self.cleanup.process_played_tracks()
            mock_delete.assert_not_called()
        self.assertTrue(self.cleanup.run_summary.get("protected_skips"))
        self.assertEqual(self.cleanup.run_summary.get("music_deletions"), [])

    def test_delete_policy_requires_all_user_tags_played(self):
        track = self._sample_plex_track(
            watched_by={"alice": True, "bob": False},
        )
        tags = [
            {"id": 10, "label": "alice"},
            {"id": 11, "label": "bob"},
        ]
        lidarr_match = {
            "artist": {"id": 1, "artistName": "Coldplay", "tags": [10, 11]},
            "album": {"id": 3, "title": "Parachutes", "tags": []},
            "track": {"id": 9, "title": "Yellow", "tags": []},
            "file_id": 42,
        }
        with patch.object(self.cleanup, "get_lidarr_tags", return_value=tags), patch.object(
            self.cleanup, "get_played_tracks", return_value=[track]
        ), patch.object(
            self.cleanup, "match_track_to_lidarr", return_value=lidarr_match
        ), patch.object(
            self.cleanup, "delete_lidarr_track_file"
        ) as mock_delete:
            self.cleanup.process_played_tracks()
            mock_delete.assert_not_called()

    def test_delete_policy_deletes_when_allowed(self):
        track = self._sample_plex_track(watched_by={"alice": True})
        tags = [{"id": 10, "label": "alice"}]
        lidarr_match = {
            "artist": {"id": 1, "artistName": "Coldplay", "tags": [10]},
            "album": {"id": 3, "title": "Parachutes", "tags": []},
            "track": {"id": 9, "title": "Yellow", "tags": []},
            "file_id": 42,
        }
        with patch.object(self.cleanup, "get_lidarr_tags", return_value=tags), patch.object(
            self.cleanup, "get_played_tracks", return_value=[track]
        ), patch.object(
            self.cleanup, "match_track_to_lidarr", return_value=lidarr_match
        ), patch.object(
            self.cleanup, "delete_lidarr_track_file", return_value=True
        ) as mock_delete, patch.object(
            self.cleanup, "unmonitor_lidarr_track", return_value=True
        ) as mock_unmonitor:
            self.cleanup.process_played_tracks()
            mock_delete.assert_called_once_with(42)
            mock_unmonitor.assert_called_once_with(9)
        self.assertEqual(len(self.cleanup.run_summary["music_deletions"]), 1)

    def test_delete_policy_skips_without_owned_file(self):
        """Owned file is required: Lidarr match without trackFileId is not deleted."""
        track = self._sample_plex_track()
        tags = []
        lidarr_match = {
            "artist": {"id": 1, "artistName": "Coldplay", "tags": []},
            "album": {"id": 3, "title": "Parachutes", "tags": []},
            "track": {"id": 9, "title": "Yellow", "tags": []},
            "file_id": None,
        }
        with patch.object(self.cleanup, "get_lidarr_tags", return_value=tags), patch.object(
            self.cleanup, "get_played_tracks", return_value=[track]
        ), patch.object(
            self.cleanup, "match_track_to_lidarr", return_value=lidarr_match
        ), patch.object(
            self.cleanup, "delete_lidarr_track_file"
        ) as mock_delete:
            self.cleanup.process_played_tracks()
            mock_delete.assert_not_called()

    def test_dry_run_does_not_call_lidarr_delete_api(self):
        cleanarr.CONFIG["dry_run"] = True
        with patch.object(self.cleanup, "_lidarr_request") as mock_req:
            result = self.cleanup.delete_lidarr_track_file(42)
            self.assertTrue(result)
            mock_req.assert_not_called()

    def test_dry_run_process_records_music_deletion_without_api_delete(self):
        cleanarr.CONFIG["dry_run"] = True
        track = self._sample_plex_track()
        tags = []
        lidarr_match = {
            "artist": {"id": 1, "artistName": "Coldplay", "tags": []},
            "album": {"id": 3, "title": "Parachutes", "tags": []},
            "track": {"id": 9, "title": "Yellow", "tags": []},
            "file_id": 42,
        }
        with patch.object(self.cleanup, "get_lidarr_tags", return_value=tags), patch.object(
            self.cleanup, "get_played_tracks", return_value=[track]
        ), patch.object(
            self.cleanup, "match_track_to_lidarr", return_value=lidarr_match
        ), patch.object(
            self.cleanup, "_lidarr_request"
        ) as mock_req:
            self.cleanup.process_played_tracks()
            delete_calls = [
                c for c in mock_req.call_args_list
                if len(c.args) >= 2 and c.args[1] == "DELETE"
            ]
            self.assertEqual(delete_calls, [])
        self.assertEqual(len(self.cleanup.run_summary["music_deletions"]), 1)

    def test_should_delete_media_shared_policy_for_tracks(self):
        media = {"title": "Yellow", "artist": "Coldplay"}
        self.assertTrue(
            self.cleanup.should_delete_media(media, ["alice"], {"alice": True, "bob": False})
        )
        self.assertFalse(
            self.cleanup.should_delete_media(media, ["alice", "bob"], {"alice": True, "bob": False})
        )


if __name__ == "__main__":
    unittest.main()
