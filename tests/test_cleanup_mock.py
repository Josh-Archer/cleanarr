import unittest
from unittest.mock import MagicMock, patch, ANY
import sys
import os
import tempfile

# Ensure we can import the runtime package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set env var for log file to avoid permission error
os.environ['CLEANARR_LOG_FILE'] = os.path.join(tempfile.gettempdir(), 'cleanarr_test.log')

from cleanarr_runtime import cleanup as cleanarr

class TestMediaCleanup(unittest.TestCase):

    def setUp(self):
        # Mock configuration
        self.config_patcher = patch.dict(cleanarr.CONFIG, {
            "plex": {"baseurl": "http://mock-plex:32400", "token": "mock-token"},
            "sonarr": {"baseurl": "http://mock-sonarr:8989", "apikey": "mock-api"},
            "radarr": {"baseurl": "http://mock-radarr:7878", "apikey": "mock-api"},
            "transmission": {
                "host": "mock-transmission",
                "port": 9091,
                "username": "user",
                "password": "pass"
            },
            "debug": True,
            "dry_run": False,
            "disable_torrent_cleanup": False,
            "transmission_io_error_cleanup_enabled": True,
            "transmission_io_error_threshold": 2,
            "transmission_io_error_state_file": os.path.join(
                tempfile.gettempdir(), "cleanarr-io-error-test.json"
            ),
        })
        self.config_patcher.start()

        # Mock external libraries
        self.plex_patcher = patch('cleanarr_runtime.cleanup.PlexServer')
        self.MockPlex = self.plex_patcher.start()

        self.trans_patcher = patch('cleanarr_runtime.cleanup.TransmissionClient')
        self.MockTransmission = self.trans_patcher.start()

        self.requests_patcher = patch('cleanarr_runtime.cleanup.requests')
        self.MockRequests = self.requests_patcher.start()
        
        # Mock Session and its methods
        self.mock_session = MagicMock()
        self.MockRequests.Session.return_value = self.mock_session

        # Instantiate MediaCleanup
        self.cleanup = cleanarr.MediaCleanup()

    def tearDown(self):
        self.config_patcher.stop()
        self.plex_patcher.stop()
        self.trans_patcher.stop()
        self.requests_patcher.stop()

    def test_should_delete_media_no_tags(self):
        """Test deletion logic when no user tags are present."""
        media = {'title': 'Test Movie', 'show_title': 'Test Show', 'season': 1, 'episode': 1}
        user_tags = []
        watched_by = {'user1': True}

        # Should delete if no user tags are restricting it
        self.assertTrue(self.cleanup.should_delete_media(media, user_tags, watched_by))

    def test_should_delete_media_tags_all_watched(self):
        """Test deletion when tags exist and all tagged users watched."""
        media = {'title': 'Test Movie'}
        user_tags = ['user1', 'user2']
        watched_by = {'user1': True, 'user2': True, 'user3': False}

        self.assertTrue(self.cleanup.should_delete_media(media, user_tags, watched_by))

    def test_should_delete_media_tags_not_all_watched(self):
        """Test deletion prevention when a tagged user hasn't watched."""
        media = {'title': 'Test Movie'}
        user_tags = ['user1', 'user2']
        watched_by = {'user1': True, 'user2': False}

        self.assertFalse(self.cleanup.should_delete_media(media, user_tags, watched_by))

    def test_should_delete_media_case_insensitive(self):
        """Test that user tag matching is case-insensitive."""
        media = {'title': 'Test Movie'}
        # get_user_tags returns lowercase
        user_tags = ['user1', 'user2']
        # Plex might return mixed case usernames
        watched_by = {'User1': True, 'User2': True}

        self.assertTrue(self.cleanup.should_delete_media(media, user_tags, watched_by))

    def test_sonarr_request_success(self):
        """Test successful Sonarr API call."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'data': 'test'}
        self.mock_session.get.return_value = mock_response

        result = self.cleanup._sonarr_request('test-endpoint')
        self.assertEqual(result, {'data': 'test'})
        self.mock_session.get.assert_called_with(
            'http://mock-sonarr:8989/test-endpoint'
        )

    def test_match_episode_to_sonarr(self):
        """Test matching Plex episode to Sonarr episode."""
        # Mock Sonarr series list
        self.mock_session.get.side_effect = [
            # First call: get series
            MagicMock(status_code=200, json=lambda: [{'title': 'Test Show', 'id': 100}]),
            # Second call: get episodes for series 100
            MagicMock(status_code=200, json=lambda: [
                {'seasonNumber': 1, 'episodeNumber': 1, 'id': 500, 'episodeFileId': 999}
            ])
        ]

        plex_episode = {
            'show_title': 'Test Show',
            'season': 1,
            'episode': 1
        }

        match = self.cleanup.match_episode_to_sonarr(plex_episode)

        self.assertIsNotNone(match)
        self.assertEqual(match['series']['id'], 100)
        self.assertEqual(match['episode']['id'], 500)
        self.assertEqual(match['file_id'], 999)

    def test_match_movie_to_radarr_fuzzy(self):
        """Test fuzzy matching for movies."""
        # Mock Radarr movie list
        self.mock_session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {'title': 'The Avengers', 'year': 2012, 'id': 1, 'movieFile': {'id': 10}}
            ]
        )

        plex_movie = {'title': 'Avengers', 'year': 2012}
        match = self.cleanup.match_movie_to_radarr(plex_movie)

        self.assertIsNotNone(match)
        self.assertEqual(match['movie']['title'], 'The Avengers')

    def test_get_user_tags(self):
        """Test extracting user tags from Sonarr/Radarr tags."""
        tags = [
            {'id': 1, 'label': 'safe'},
            {'id': 2, 'label': 'kids'},
            {'id': 3, 'label': 'user1'},
            {'id': 4, 'label': ' 10 - user2 '}, # Cleanarr cleans this format
        ]
        tag_ids = {3, 4}

        user_tags = self.cleanup.get_user_tags(tags, tag_ids)
        self.assertIn('user1', user_tags)
        self.assertIn('user2', user_tags)
        self.assertNotIn('safe', user_tags)

    def test_remove_torrent_by_file_path(self):
        """Test removing torrent by file path."""
        mock_torrent = MagicMock()
        mock_torrent.id = 123
        mock_torrent.name = "Test.Torrent"
        mock_torrent.rate_download = 0
        mock_torrent.files.return_value = [{'name': 'Test.Torrent/Test.Movie.mkv'}]
        mock_torrent.status = 6  # Seeding

        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]

        # Matches suffix
        file_path = "/downloads/Test.Torrent/Test.Movie.mkv"

        result = self.cleanup.remove_torrent_by_file_path(file_path)

        self.assertTrue(result)
        self.cleanup.transmission.remove_torrent.assert_called_with(123, delete_data=True)

    def test_normalize_tag_label(self):
        """Test the tag normalization helper."""
        self.assertEqual(cleanarr._normalize_tag_label("  User1 "), "user1")
        self.assertEqual(cleanarr._normalize_tag_label("10-User2"), "user2")
        self.assertEqual(cleanarr._normalize_tag_label("10 - User3"), "user3")

    def test_repeated_io_error_cleanup_waits_for_threshold(self):
        """I/O error cleanup should observe a torrent before removing it."""
        mock_torrent = MagicMock()
        mock_torrent.id = 10
        mock_torrent.name = "Broken Torrent"
        mock_torrent.error = 1
        mock_torrent.error_string = "Input/output error (5)"
        mock_torrent.hashString = "abc123"
        mock_torrent.download_dir = "/media/downloads"
        mock_torrent.rate_download = 0
        mock_torrent.rate_upload = 0
        mock_torrent.status = 0
        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]

        with patch.object(self.cleanup, "_load_io_error_state", return_value={}), \
             patch.object(self.cleanup, "_save_io_error_state") as mock_save_state:
            self.cleanup.clean_repeated_io_error_torrents()

        self.cleanup.transmission.remove_torrent.assert_not_called()
        mock_save_state.assert_called_once()
        saved_state = mock_save_state.call_args.args[0]
        self.assertEqual(saved_state["abc123"]["count"], 1)

    def test_repeated_io_error_cleanup_removes_metadata_only_after_threshold(self):
        """Repeated I/O failures should remove torrent metadata without deleting data."""
        mock_torrent = MagicMock()
        mock_torrent.id = 11
        mock_torrent.name = "Still Broken"
        mock_torrent.error = 1
        mock_torrent.error_string = "Input/output error (5)"
        mock_torrent.hashString = "def456"
        mock_torrent.download_dir = "/media/downloads"
        mock_torrent.rate_download = 0
        mock_torrent.rate_upload = 0
        mock_torrent.status = 0
        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]

        with patch.object(
            self.cleanup,
            "_load_io_error_state",
            return_value={"def456": {"count": 1, "first_seen": "2026-03-07T00:00:00+00:00"}},
        ), patch.object(self.cleanup, "_save_io_error_state") as mock_save_state:
            self.cleanup.clean_repeated_io_error_torrents()

        self.cleanup.transmission.remove_torrent.assert_called_once_with(11, delete_data=False)
        mock_save_state.assert_called_once_with({})

    def test_process_watched_episodes_does_not_delete_unwatched_episodes_just_because_later_episodes_exist(self):
        watched_episode = {
            "show_title": "The Beast in Me",
            "season": 1,
            "episode": 2,
            "title": "Just Don't Want to Be Lonely",
            "file": "/media/tv/The Beast in Me/Season 1/S01E02.mkv",
            "watched_by": {"owner-user": True},
            "watch_evidence": {"owner-user": "history"},
            "rating_key": "rk2",
        }
        sonarr_series = [{"id": 118, "title": "The Beast in Me", "tags": [1]}]
        sonarr_episodes = [
            {"id": 100 + ep, "seasonNumber": 1, "episodeNumber": ep, "episodeFileId": ep, "tags": []}
            for ep in range(1, 9)
        ]
        episode_match = {
            "series": sonarr_series[0],
            "episode": sonarr_episodes[1],
            "file_id": 2,
        }

        with patch.object(self.cleanup, "get_watched_episodes", return_value=[watched_episode]), \
             patch.object(self.cleanup, "get_sonarr_tags", return_value=[{"id": 1, "label": "1-owner-user"}]), \
             patch.object(self.cleanup, "get_sonarr_series", return_value=sonarr_series), \
             patch.object(self.cleanup, "_sonarr_request", return_value=sonarr_episodes), \
             patch.object(self.cleanup, "match_episode_to_sonarr", return_value=episode_match), \
             patch.object(self.cleanup, "delete_sonarr_episode_file", return_value=True) as mock_delete, \
             patch.object(self.cleanup, "unmonitor_sonarr_episode") as mock_unmonitor, \
             patch.object(self.cleanup, "remove_torrent_by_file_path"), \
             patch.object(self.cleanup, "remove_from_plex_watchlist"):
            self.cleanup.process_watched_episodes()

        mock_delete.assert_called_once_with(2)
        mock_unmonitor.assert_called_once_with(102)

    def test_process_watched_episodes_ignores_iswatched_fallback_for_watched_ahead(self):
        watched_episode = {
            "show_title": "The Beast in Me",
            "season": 1,
            "episode": 2,
            "title": "Just Don't Want to Be Lonely",
            "file": "/media/tv/The Beast in Me/Season 1/S01E02.mkv",
            "watched_by": {"owner-user": True},
            "watch_evidence": {"owner-user": "isWatched_fallback"},
            "rating_key": "rk2",
        }
        sonarr_series = [{"id": 118, "title": "The Beast in Me", "tags": [1]}]
        sonarr_episodes = [
            {"id": 200 + ep, "seasonNumber": 1, "episodeNumber": ep, "episodeFileId": ep, "tags": []}
            for ep in range(1, 9)
        ]
        episode_match = {
            "series": sonarr_series[0],
            "episode": sonarr_episodes[1],
            "file_id": 2,
        }

        with patch.object(self.cleanup, "get_watched_episodes", return_value=[watched_episode]), \
             patch.object(self.cleanup, "get_sonarr_tags", return_value=[{"id": 1, "label": "1-owner-user"}]), \
             patch.object(self.cleanup, "get_sonarr_series", return_value=sonarr_series), \
             patch.object(self.cleanup, "_sonarr_request", return_value=sonarr_episodes), \
             patch.object(self.cleanup, "match_episode_to_sonarr", return_value=episode_match), \
             patch.object(self.cleanup, "delete_sonarr_episode_file", return_value=True) as mock_delete, \
             patch.object(self.cleanup, "unmonitor_sonarr_episode"), \
             patch.object(self.cleanup, "remove_torrent_by_file_path"), \
             patch.object(self.cleanup, "remove_from_plex_watchlist"):
            self.cleanup.process_watched_episodes()

        mock_delete.assert_called_once_with(2)

    def test_process_watched_episodes_respects_owner_tags_after_actual_watch(self):
        watched_episode = {
            "show_title": "The Beast in Me",
            "season": 1,
            "episode": 2,
            "title": "Just Don't Want to Be Lonely",
            "file": "/media/tv/The Beast in Me/Season 1/S01E02.mkv",
            "watched_by": {"owner-user": True},
            "watch_evidence": {"owner-user": "history"},
            "rating_key": "rk2",
        }
        sonarr_match = {
            "series": {"id": 118, "title": "The Beast in Me", "tags": [1]},
            "episode": {"id": 202, "episodeFileId": 2, "tags": []},
            "file_id": 2,
        }

        with patch.object(self.cleanup, "get_watched_episodes", return_value=[watched_episode]), \
             patch.object(self.cleanup, "get_sonarr_tags", return_value=[{"id": 1, "label": "1-owner-user"}]), \
             patch.object(self.cleanup, "get_sonarr_series", return_value=[]), \
             patch.object(self.cleanup, "match_episode_to_sonarr", return_value=sonarr_match), \
             patch.object(self.cleanup, "delete_sonarr_episode_file", return_value=True) as mock_delete, \
             patch.object(self.cleanup, "unmonitor_sonarr_episode"), \
             patch.object(self.cleanup, "remove_torrent_by_file_path"), \
             patch.object(self.cleanup, "remove_from_plex_watchlist"):
            self.cleanup.process_watched_episodes()

        mock_delete.assert_called_once_with(2)

    def test_process_watched_episodes_skips_protected_series(self):
        watched_episode = {
            "show_title": "Bluey",
            "season": 1,
            "episode": 2,
            "title": "Hospital",
            "file": "/media/tv/Bluey/Season 1/S01E02.mkv",
            "watched_by": {"owner-user": True},
            "watch_evidence": {"owner-user": "history"},
            "rating_key": "rk-bluey",
        }
        sonarr_match = {
            "series": {"id": 1, "title": "Bluey", "tags": [2]},
            "episode": {"id": 12, "episodeFileId": 22, "tags": []},
            "file_id": 22,
        }

        with patch.object(self.cleanup, "get_watched_episodes", return_value=[watched_episode]), \
             patch.object(self.cleanup, "get_sonarr_tags", return_value=[{"id": 2, "label": "kids"}]), \
             patch.object(self.cleanup, "get_sonarr_series", return_value=[]), \
             patch.object(self.cleanup, "match_episode_to_sonarr", return_value=sonarr_match), \
             patch.object(self.cleanup, "delete_sonarr_episode_file") as mock_delete:
            self.cleanup.process_watched_episodes()

        mock_delete.assert_not_called()

if __name__ == '__main__':
    unittest.main()
