import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import sys
import os
import tempfile
import requests

# Ensure we can import the local package
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

# Set env var for log file to avoid permission error
os.environ['CLEANARR_LOG_FILE'] = os.path.join(tempfile.gettempdir(), 'cleanarr_test.log')

from cleanarr import cleanup as cleanarr

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
                "password": "pass",
                "rpc_timeout_seconds": 90,
            },
            "debug": True,
            "dry_run": False,
            "disable_torrent_cleanup": False,
            "remove_orphan_incomplete_downloads": True,
            "remove_stale_torrents": True,
            "torrent_cleanup_allowed_categories": set(),
            "transmission_io_error_cleanup_enabled": True,
            "transmission_io_error_threshold": 2,
            "transmission_io_error_state_file": os.path.join(
                tempfile.gettempdir(), "cleanarr-io-error-test.json"
            ),
        })
        self.config_patcher.start()

        # Mock external libraries
        self.plex_patcher = patch('cleanarr.cleanup.PlexServer')
        self.MockPlex = self.plex_patcher.start()

        self.trans_patcher = patch('cleanarr.cleanup.TransmissionClient')
        self.MockTransmission = self.trans_patcher.start()

        self.requests_patcher = patch('cleanarr.cleanup.requests')
        self.MockRequests = self.requests_patcher.start()
        
        # Mock Session and its methods
        self.mock_session = MagicMock()
        self.MockRequests.Session.return_value = self.mock_session

        # Instantiate MediaCleanup
        self.cleanup = cleanarr.MediaCleanup()
        self.MockTransmission.assert_called_with(
            host="mock-transmission",
            port=9091,
            username="user",
            password="pass",
            timeout=90,
        )

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
        mock_response.text = '{"data":"test"}'
        mock_response.json.return_value = {'data': 'test'}
        self.mock_session.get.return_value = mock_response

        result = self.cleanup._sonarr_request('test-endpoint')
        self.assertEqual(result, {'data': 'test'})
        self.mock_session.get.assert_called_with('http://mock-sonarr:8989/test-endpoint', timeout=30)

    def test_arr_request_retries_transient_http_error(self):
        """Transient 5xx errors should retry before failing the request."""
        error_response = MagicMock()
        error_response.status_code = 502
        error_response.text = "<html>bad gateway</html>"
        error_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=error_response)

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.text = '{"ok":true}'
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {"ok": True}

        self.mock_session.get.side_effect = [error_response, success_response]

        with patch("cleanarr.cleanup.time.sleep") as mock_sleep:
            result = self.cleanup._radarr_request("tag")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.mock_session.get.call_count, 2)
        mock_sleep.assert_called_once()

    def test_arr_request_treats_empty_delete_response_as_success(self):
        """DELETE endpoints with empty 2xx bodies should still count as success."""
        empty_success = MagicMock()
        empty_success.status_code = 202
        empty_success.text = ""
        empty_success.raise_for_status.return_value = None

        self.mock_session.delete.return_value = empty_success

        result = self.cleanup._sonarr_request("episodefile/123", method="DELETE")

        self.assertEqual(result, {})

    def test_unmonitor_sonarr_episode_updates_episode_specific_endpoint(self):
        """Sonarr unmonitor should target the episode-specific PUT route."""
        episode_payload = {"id": 16823, "seriesId": 169, "episodeFileId": 6602, "monitored": True}

        with patch.object(self.cleanup, "_sonarr_request", side_effect=[episode_payload.copy(), {}]) as mock_sonarr:
            self.cleanup.unmonitor_sonarr_episode(16823)

        self.assertEqual(mock_sonarr.call_args_list[0].args[0], "episode/16823")
        self.assertEqual(mock_sonarr.call_args_list[1].args[0], "episode/16823")
        self.assertEqual(mock_sonarr.call_args_list[1].kwargs["method"], "PUT")
        self.assertFalse(mock_sonarr.call_args_list[1].kwargs["data"]["monitored"])
        self.assertEqual(mock_sonarr.call_args_list[1].kwargs["data"]["id"], 16823)

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
            text='[{"title":"The Avengers","year":2012,"id":1,"movieFile":{"id":10}}]',
            json=lambda: [
                {'title': 'The Avengers', 'year': 2012, 'id': 1, 'movieFile': {'id': 10}}
            ]
        )

        plex_movie = {'title': 'Avengers', 'year': 2012}
        match = self.cleanup.match_movie_to_radarr(plex_movie)

        self.assertIsNotNone(match)
        self.assertEqual(match['movie']['title'], 'The Avengers')

    def test_match_movie_to_radarr_token_subset_fallback(self):
        """Longer premiere titles should still match when meaningful tokens align."""
        self.mock_session.get.return_value = MagicMock(
            status_code=200,
            text="ok",
            json=lambda: [
                {
                    "title": "Marvel Studios' The Fantastic Four: First Steps - World Premiere",
                    "year": 2025,
                    "id": 2,
                    "tmdbId": "12345",
                    "movieFile": {"id": 12},
                }
            ],
        )

        plex_movie = {
            'title': 'The Fantastic Four: First Steps',
            'year': 2025,
            'guid': 'tmdb://12345',
        }
        match = self.cleanup.match_movie_to_radarr(plex_movie)

        self.assertIsNotNone(match)
        self.assertEqual(match['movie']['id'], 2)

    def test_match_movie_to_radarr_rejects_world_premiere_variant_without_external_id_match(self):
        """Token-subset fallback should not match title variants with no shared external IDs."""
        self.mock_session.get.return_value = MagicMock(
            status_code=200,
            text="ok",
            json=lambda: [
                {
                    "title": "Marvel Studios' The Fantastic Four: First Steps - World Premiere",
                    "year": 2025,
                    "id": 3,
                    "tmdbId": "99999",
                    "movieFile": {"id": 13},
                }
            ],
        )

        plex_movie = {
            'title': 'The Fantastic Four: First Steps',
            'year': 2025,
            'guid': 'tmdb://12345',
        }
        match = self.cleanup.match_movie_to_radarr(plex_movie)

        self.assertIsNone(match)

    def test_match_movie_to_radarr_ignores_generic_single_word_candidate(self):
        """Single-word candidates should not match unrelated long Plex titles."""
        self.mock_session.get.return_value = MagicMock(
            status_code=200,
            text="ok",
            json=lambda: [
                {'title': 'Normal', 'year': 2026, 'id': 3, 'movieFile': {}}
            ],
        )

        plex_movie = {
            'title': 'Octokuro - [VirtualTaboo-VR2Normal] - [2023] - Moo Means Fuck Me In The Ass [x265]',
            'year': 2026,
        }
        match = self.cleanup.match_movie_to_radarr(plex_movie)

        self.assertIsNone(match)

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

    def test_radarr_movie_delete_empty_body_is_success(self):
        """Delete responses with empty body from Radarr should count as success."""
        with patch.object(self.cleanup, '_radarr_request', return_value={}) as mock_request:
            result = self.cleanup.delete_radarr_movie_file(321)

        self.assertTrue(result)
        mock_request.assert_called_once_with("moviefile/321", method="DELETE")

    def test_sonarr_tags_cached_for_single_run(self):
        """Sonarr tags should be fetched once per run and then reused."""
        with patch.object(self.cleanup, "_sonarr_request", return_value=[{"id": 1, "label": "safe"}]) as mock_request:
            first = self.cleanup.get_sonarr_tags()
            second = self.cleanup.get_sonarr_tags()

        self.assertEqual(first, second)
        mock_request.assert_called_once_with("tag")

    def test_radarr_tags_cached_for_single_run(self):
        """Radarr tags should be fetched once per run and then reused."""
        with patch.object(self.cleanup, "_radarr_request", return_value=[{"id": 1, "label": "safe"}]) as mock_request:
            first = self.cleanup.get_radarr_tags()
            second = self.cleanup.get_radarr_tags()

        self.assertEqual(first, second)
        mock_request.assert_called_once_with("tag")

    def test_sonarr_series_cached_for_single_run(self):
        """Sonarr series list should be fetched once per run and then reused."""
        with patch.object(self.cleanup, "_sonarr_request", return_value=[{"id": 10, "title": "Test"}]) as mock_request:
            first = self.cleanup.get_sonarr_series()
            second = self.cleanup.get_sonarr_series()

        self.assertEqual(first, second)
        mock_request.assert_called_once_with("series")

    def test_radarr_movies_cached_for_single_run(self):
        """Radarr movie catalog should be fetched once per run and then reused."""
        with patch.object(self.cleanup, "_radarr_request", return_value=[{"id": 5, "title": "Movie", "year": 2024, "path": "/media/movies"}]) as mock_request:
            first = self.cleanup.get_radarr_movies()
            second = self.cleanup.get_radarr_movies()

        self.assertEqual(first, second)
        mock_request.assert_called_once_with("movie")

    def test_sonarr_episodes_cached_per_series_id_for_single_run(self):
        """Sonarr episode lists should be cached per series ID."""
        episodes = [
            {"seasonNumber": 1, "episodeNumber": 1},
            {"seasonNumber": 1, "episodeNumber": 2},
        ]
        with patch.object(self.cleanup, "_sonarr_request", return_value=episodes) as mock_request:
            first = self.cleanup.get_sonarr_episodes_for_series(7)
            second = self.cleanup.get_sonarr_episodes_for_series(7)

        self.assertEqual(first, second)
        mock_request.assert_called_once_with("episode?seriesId=7")

    def test_process_watched_movies_skips_matched_items_without_movie_file_id(self):
        """Matched Radarr entries without a movie file should not attempt deletion."""
        movie = {
            'title': 'Example Movie',
            'year': 2024,
            'file': '/movies/Example Movie (2024).mkv',
            'watched_by': {'user1': True},
            'rating_key': '123',
        }

        with patch.object(self.cleanup, 'get_watched_movies', return_value=[movie]), \
             patch.object(self.cleanup, 'get_radarr_tags', return_value=[]), \
             patch.object(
                 self.cleanup,
                 'match_movie_to_radarr',
                 return_value={'movie': {'id': 10, 'title': 'Example Movie', 'tags': []}, 'file_id': None},
             ), \
             patch.object(self.cleanup, 'delete_radarr_movie_file') as mock_delete:
            self.cleanup.process_watched_movies()

        mock_delete.assert_not_called()

    def test_process_watched_movies_unmanaged_path_skips_without_warning(self):
        """Watched movies outside Radarr-managed paths should log info instead of warning."""
        movie = {
            'title': 'External Movie',
            'year': 2025,
            'file': '/external/plex/External Movie (2025).mkv',
            'watched_by': {'owner-user': True},
            'rating_key': '999',
            'guid': 'tmdb://98765',
        }

        with patch.object(self.cleanup, 'get_watched_movies', return_value=[movie]), \
             patch.object(self.cleanup, 'get_radarr_tags', return_value=[]), \
             patch.object(self.cleanup, 'get_radarr_movies', return_value=[{'path': '/radarr/movies'}]), \
             patch.object(self.cleanup, 'match_movie_to_radarr', return_value=None), \
             patch.object(cleanarr.logger, 'warning') as mock_warning, \
             patch.object(cleanarr.logger, 'info') as mock_info:
            self.cleanup.process_watched_movies()

        self.assertEqual(mock_warning.call_count, 0)
        self.assertTrue(
            any(
                "outside Radarr-managed paths" in str(call.args[0])
                for call in mock_info.call_args_list
            )
        )

    def test_delete_episode_and_cleanup_records_tv_deletion_only_on_success(self):
        """Episode summaries should not report both deletion and failure for one delete."""
        with patch.object(self.cleanup, 'delete_sonarr_episode_file', return_value=True), \
             patch.object(self.cleanup, 'unmonitor_sonarr_episode') as mock_unmonitor, \
             patch.object(self.cleanup, 'remove_torrent_by_file_path') as mock_remove_torrent, \
             patch.object(self.cleanup, 'remove_from_plex_watchlist') as mock_remove_watchlist:
            result = self.cleanup._delete_episode_and_cleanup(
                "Memory of a Killer S1E9",
                "standard watched",
                6864,
                16728,
                file_path="/media/tv/Memory of a Killer/Season 1/Memory of a Killer - S01E09 - Shoot the Piano Player WEBDL-2160p.mkv",
                rating_key="12345",
            )

        self.assertTrue(result)
        self.assertEqual(
            self.cleanup.run_summary["tv_deletions"],
            ["Memory of a Killer S1E9 [standard watched]"],
        )
        self.assertEqual(self.cleanup.run_summary["errors"], [])
        mock_unmonitor.assert_called_once_with(16728)
        mock_remove_torrent.assert_called_once()
        mock_remove_watchlist.assert_called_once_with("12345")

    def test_delete_episode_and_cleanup_records_error_without_tv_deletion_on_failure(self):
        """Episode summaries should only report an error when the Sonarr delete fails."""
        with patch.object(self.cleanup, 'delete_sonarr_episode_file', return_value=False), \
             patch.object(self.cleanup, 'unmonitor_sonarr_episode') as mock_unmonitor, \
             patch.object(self.cleanup, 'remove_torrent_by_file_path') as mock_remove_torrent, \
             patch.object(self.cleanup, 'remove_from_plex_watchlist') as mock_remove_watchlist:
            result = self.cleanup._delete_episode_and_cleanup(
                "Memory of a Killer S1E9",
                "standard watched",
                6864,
                16728,
                file_path="/media/tv/Memory of a Killer/Season 1/Memory of a Killer - S01E09 - Shoot the Piano Player WEBDL-2160p.mkv",
                rating_key="12345",
            )

        self.assertFalse(result)
        self.assertEqual(self.cleanup.run_summary["tv_deletions"], [])
        self.assertEqual(
            self.cleanup.run_summary["errors"],
            ["Memory of a Killer S1E9 delete failed [standard watched]"],
        )
        mock_unmonitor.assert_not_called()
        mock_remove_torrent.assert_not_called()
        mock_remove_watchlist.assert_not_called()

    def test_remove_torrent_by_file_path(self):
        """Test removing torrent by file path."""
        mock_torrent = MagicMock()
        mock_torrent.id = 123
        mock_torrent.name = "Test.Torrent"
        mock_torrent.download_dir = "/media/downloads/sonarr"
        mock_torrent.rate_download = 0
        mock_torrent.files.return_value = [{'name': 'Test.Torrent/Test.Movie.mkv'}]
        mock_torrent.status = 6  # Seeding

        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]

        # Matches suffix
        file_path = "/downloads/Test.Torrent/Test.Movie.mkv"

        result = self.cleanup.remove_torrent_by_file_path(file_path)

        self.assertTrue(result)
        self.cleanup.transmission.remove_torrent.assert_called_with(123, delete_data=True)

    def test_remove_torrent_by_file_path_skips_disallowed_category(self):
        """Torrent removal should skip torrents outside the configured category allowlist."""
        cleanarr.CONFIG["torrent_cleanup_allowed_categories"] = {"sonarr"}

        mock_torrent = MagicMock()
        mock_torrent.id = 124
        mock_torrent.name = "Readarr.Torrent"
        mock_torrent.download_dir = "/media/downloads/readarr"
        mock_torrent.rate_download = 0
        mock_torrent.files.return_value = [{'name': 'Readarr.Torrent/Book.epub'}]

        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]

        result = self.cleanup.remove_torrent_by_file_path("/media/downloads/readarr/Readarr.Torrent/Book.epub")

        self.assertFalse(result)
        self.cleanup.transmission.remove_torrent.assert_not_called()

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

    def test_clean_failed_downloads_skips_disallowed_categories(self):
        """Errored torrents outside the allowlist should not be removed."""
        cleanarr.CONFIG["torrent_cleanup_allowed_categories"] = {"sonarr"}

        mock_torrent = MagicMock()
        mock_torrent.id = 200
        mock_torrent.name = "Readarr broken"
        mock_torrent.download_dir = "/media/downloads/readarr"
        mock_torrent.error = 1
        mock_torrent.error_string = "broken"

        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]
        self.cleanup.clean_failed_downloads()

        self.cleanup.transmission.remove_torrent.assert_not_called()

    def test_remove_stale_torrents_skips_disallowed_categories(self):
        """Stale torrent cleanup should only apply to allowed categories."""
        cleanarr.CONFIG["torrent_cleanup_allowed_categories"] = {"sonarr"}

        mock_torrent = MagicMock()
        mock_torrent.id = 201
        mock_torrent.name = "Readarr stale"
        mock_torrent.download_dir = "/media/downloads/readarr"
        mock_torrent.added_date = datetime.now(timezone.utc) - timedelta(hours=24)
        mock_torrent.percent_done = 1.0
        mock_torrent.rate_download = 0
        mock_torrent.status = 0
        mock_torrent.peers_connected = 0

        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]
        self.cleanup.remove_stale_torrents()

        self.cleanup.transmission.remove_torrent.assert_not_called()

    # ------------------------------------------------------------------
    # Label-filter tests (CLEANARR_TORRENT_CLEANUP_REQUIRED_LABELS)
    # ------------------------------------------------------------------

    def _make_torrent(self, *, torrent_id, name, download_dir, labels=None,
                      error=0, error_string="", added_hours_ago=24,
                      percent_done=1.0, rate_download=0, status=0,
                      peers_connected=0):
        t = MagicMock()
        t.id = torrent_id
        t.name = name
        t.download_dir = download_dir
        t.labels = labels or []
        t.error = error
        t.error_string = error_string
        t.added_date = datetime.now(timezone.utc) - timedelta(hours=added_hours_ago)
        t.percent_done = percent_done
        t.rate_download = rate_download
        t.status = status
        t.peers_connected = peers_connected
        return t

    def test_torrent_cleanup_allowed_skips_unlabelled_when_labels_required(self):
        """Unlabelled torrents must be skipped when a required-labels set is configured."""
        cleanarr.CONFIG["torrent_cleanup_required_labels"] = {"sonarr", "radarr"}
        torrent = self._make_torrent(torrent_id=300, name="Adult Content.mp4",
                                     download_dir="/media/downloads", labels=[])
        self.assertFalse(self.cleanup._torrent_cleanup_allowed(torrent, "test"))

    def test_torrent_cleanup_allowed_passes_matching_label(self):
        """Torrents carrying a recognised label should be cleared for cleanup."""
        cleanarr.CONFIG["torrent_cleanup_required_labels"] = {"sonarr", "radarr"}
        torrent = self._make_torrent(torrent_id=301, name="Fargo S02E03.mkv",
                                     download_dir="/media/downloads/sonarr",
                                     labels=["sonarr"])
        self.assertTrue(self.cleanup._torrent_cleanup_allowed(torrent, "test"))

    def test_torrent_cleanup_allowed_passes_when_no_labels_required(self):
        """Without a required-labels filter every torrent is allowed (existing behaviour)."""
        cleanarr.CONFIG["torrent_cleanup_required_labels"] = set()
        cleanarr.CONFIG["torrent_cleanup_allowed_categories"] = set()
        torrent = self._make_torrent(torrent_id=302, name="Unlabelled.mp4",
                                     download_dir="/media/downloads", labels=[])
        self.assertTrue(self.cleanup._torrent_cleanup_allowed(torrent, "test"))

    def test_remove_stale_torrents_skips_unlabelled_when_labels_required(self):
        """Stale torrent cleanup must not remove torrents that lack the required label."""
        cleanarr.CONFIG["torrent_cleanup_required_labels"] = {"sonarr", "radarr"}
        cleanarr.CONFIG["torrent_cleanup_allowed_categories"] = set()

        mock_torrent = self._make_torrent(
            torrent_id=303, name="Adult Stale.mp4",
            download_dir="/media/downloads", labels=[],
            added_hours_ago=48, percent_done=1.0, rate_download=0,
            status=0, peers_connected=0,
        )
        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]
        self.cleanup.remove_stale_torrents()
        self.cleanup.transmission.remove_torrent.assert_not_called()

    def test_clean_failed_downloads_skips_unlabelled_when_labels_required(self):
        """Failed-download cleanup must skip torrents that lack the required label."""
        cleanarr.CONFIG["torrent_cleanup_required_labels"] = {"sonarr"}
        cleanarr.CONFIG["torrent_cleanup_allowed_categories"] = set()

        mock_torrent = self._make_torrent(
            torrent_id=304, name="Adult Broken.mp4",
            download_dir="/media/downloads", labels=[],
            error=1, error_string="broken",
        )
        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]
        self.cleanup.clean_failed_downloads()
        self.cleanup.transmission.remove_torrent.assert_not_called()

    def test_remove_torrent_by_file_path_skips_unlabelled_when_labels_required(self):
        """File-path torrent removal must skip torrents that lack the required label."""
        cleanarr.CONFIG["torrent_cleanup_required_labels"] = {"sonarr"}
        cleanarr.CONFIG["torrent_cleanup_allowed_categories"] = set()

        mock_torrent = self._make_torrent(
            torrent_id=305, name="Adult.mp4",
            download_dir="/media/downloads", labels=[],
            rate_download=0, status=6,
        )
        mock_torrent.files.return_value = [{"name": "Adult.mp4"}]
        self.cleanup.transmission.get_torrents.return_value = [mock_torrent]

        result = self.cleanup.remove_torrent_by_file_path("/media/downloads/Adult.mp4")
        self.assertFalse(result)
        self.cleanup.transmission.remove_torrent.assert_not_called()

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
