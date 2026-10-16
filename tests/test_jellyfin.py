"""Jellyfin scheduled-job media-source coverage (issue #33)."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

os.environ.setdefault(
    "CLEANARR_LOG_FILE",
    os.path.join(tempfile.gettempdir(), "cleanarr_jellyfin_test.log"),
)

from cleanarr import cleanup as cleanarr  # noqa: E402
from cleanarr.adapters.jellyfin import JellyfinMediaSource  # noqa: E402


class TestMediaCleanupJellyfinSource(unittest.TestCase):
    def setUp(self):
        self.config_patcher = patch.dict(
            cleanarr.CONFIG,
            {
                "media_source": "jellyfin",
                "plex": {"baseurl": "http://unused", "token": ""},
                "jellyfin": {
                    "baseurl": "http://mock-jellyfin:8096",
                    "apikey": "jellyfin-key",
                    "users": set(),
                },
                "sonarr": {
                    "baseurl": "http://mock-sonarr:8989/",
                    "apikey": "mock-api",
                },
                "radarr": {
                    "baseurl": "http://mock-radarr:7878/",
                    "apikey": "mock-api",
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
                "transmission_io_error_cleanup_enabled": False,
            },
            clear=False,
        )
        self.config_patcher.start()
        self.plex_patcher = patch("cleanarr.cleanup.PlexServer")
        self.MockPlex = self.plex_patcher.start()
        self.trans_patcher = patch("cleanarr.cleanup.TransmissionClient")
        self.MockTransmission = self.trans_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()
        self.plex_patcher.stop()
        self.trans_patcher.stop()

    def test_init_uses_jellyfin_not_plex(self):
        cleaner = cleanarr.MediaCleanup()
        self.assertEqual(cleaner.media_source, "jellyfin")
        self.assertIsNone(cleaner.plex)
        self.MockPlex.assert_not_called()
        self.assertIsNotNone(cleaner.jellyfin_session)
        self.assertIsNotNone(cleaner.jellyfin)
        self.assertEqual(cleaner.jellyfin.config["baseurl"], "http://mock-jellyfin:8096")
        self.assertEqual(cleaner.jellyfin.config["apikey"], "jellyfin-key")

    def test_init_missing_apikey_exits(self):
        with patch.dict(cleanarr.CONFIG, {"jellyfin": {"baseurl": "http://mock-jellyfin:8096", "apikey": "", "users": set()}}):
            with self.assertRaises(SystemExit) as cm:
                cleanarr.MediaCleanup()
            self.assertEqual(cm.exception.code, 1)

    def test_unsupported_media_source_raises(self):
        with patch.dict(cleanarr.CONFIG, {"media_source": "invalid_source"}):
            with self.assertRaises(ValueError) as cm:
                cleanarr.MediaCleanup()
            self.assertIn("Unsupported CLEANARR_MEDIA_SOURCE='invalid_source'", str(cm.exception))
            self.assertIn("jellyfin", str(cm.exception))

    def test_get_watched_movies_from_jellyfin(self):
        cleaner = cleanarr.MediaCleanup()
        fixtures = (
            [
                {
                    "title": "Jellyfin Shared Movie",
                    "year": 2023,
                    "file": "/movies/JellyfinShared.mkv",
                    "watched_by": {"alice": True},
                    "watch_evidence": {"alice": "emby_isplayed"},
                    "guid": "tmdb://222",
                    "guids": ["tmdb://222"],
                    "rating_key": "jf-m1",
                    "source": "jellyfin",
                }
            ],
            [
                {
                    "show_title": "Jellyfin Show",
                    "season": 1,
                    "episode": 2,
                    "title": "Episode Two",
                    "file": "/tv/Jellyfin Show/S01E02.mkv",
                    "watched_by": {"alice": True},
                    "watch_evidence": {"alice": "emby_isplayed"},
                    "guid": "tvdb://333",
                    "rating_key": "jf-ep2",
                    "source": "jellyfin",
                }
            ],
        )
        with patch.object(
            cleanarr.emby_client, "load_watched_state", return_value=fixtures
        ) as load:
            movies = cleaner.get_watched_movies()
            episodes = cleaner.get_watched_episodes()
        load.assert_called_once()
        self.assertEqual(len(movies), 1)
        self.assertEqual(movies[0]["title"], "Jellyfin Shared Movie")
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["show_title"], "Jellyfin Show")

        # Second call uses cache
        with patch.object(
            cleanarr.emby_client, "load_watched_state", return_value=fixtures
        ) as load2:
            cleaner.get_watched_movies()
            cleaner.get_watched_episodes()
        load2.assert_not_called()

    def test_jellyfin_watched_cache_alias(self):
        cleaner = cleanarr.MediaCleanup()
        cache_data = ([{"title": "Movie"}], [{"title": "Episode"}])
        cleaner._jellyfin_watched_cache = cache_data
        self.assertEqual(cleaner._emby_watched_cache, cache_data)
        self.assertEqual(cleaner._jellyfin_watched_cache, cache_data)

    def test_should_delete_with_jellyfin_evidence(self):
        cleaner = cleanarr.MediaCleanup()
        media = {
            "title": "Jellyfin Movie",
            "year": 2023,
            "watched_by": {"alice": True},
            "watch_evidence": {"alice": "emby_isplayed"},
        }
        self.assertTrue(cleaner.should_delete_media(media, [], media["watched_by"]))
        self.assertTrue(
            cleaner.should_delete_media(media, ["alice"], media["watched_by"])
        )
        self.assertFalse(
            cleaner.should_delete_media(media, ["bob"], media["watched_by"])
        )

    def test_get_played_tracks_skips_when_jellyfin(self):
        cleaner = cleanarr.MediaCleanup()
        tracks = cleaner.get_played_tracks()
        self.assertEqual(tracks, [])

    def test_jellyfin_adapter_passes_config(self):
        session = MagicMock()
        config = {
            "baseurl": "http://jellyfin.local:8096",
            "apikey": "secret-jf-key",
            "users": {"josh", "erin"},
        }
        with patch(
            "cleanarr.adapters.jellyfin.emby_client.load_watched_state",
            return_value=([], []),
        ) as load:
            source = JellyfinMediaSource(session, config)
            res = source.load_watched_state()
            self.assertEqual(res, ([], []))
        load.assert_called_once_with(
            session,
            "http://jellyfin.local:8096",
            "secret-jf-key",
            allowed_usernames={"josh", "erin"},
        )


if __name__ == "__main__":
    unittest.main()
