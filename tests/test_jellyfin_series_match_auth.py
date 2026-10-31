import unittest
from unittest.mock import MagicMock

from cleanarr import emby_client
from cleanarr.cleanup import MediaCleanup


class JellyfinAuthHeaderTests(unittest.TestCase):
    def test_uses_mediabrowser_authorization(self):
        headers = emby_client.auth_headers(" tok ")
        self.assertEqual(headers["Authorization"], 'MediaBrowser Token="tok"')

    def test_empty_key_sends_nothing(self):
        self.assertEqual(emby_client.auth_headers(""), {})


class SeriesPathMatchTests(unittest.TestCase):
    SERIES = [
        {"id": 7, "title": "Pluribus Documentary", "path": "/tv/Pluribus Documentary"},
        {"id": 108, "title": "PLUR1BUS", "path": "/tv/Pluribus (2025)"},
    ]

    def _cleanup(self):
        mc = MediaCleanup.__new__(MediaCleanup)
        mc.get_sonarr_series = MagicMock(return_value=self.SERIES)
        mc.get_sonarr_episode = MagicMock(return_value={"id": 1, "episodeFileId": 55})
        return mc

    def test_matches_by_folder_despite_title_mismatch(self):
        mc = self._cleanup()
        ep = {"show_title": "Pluribus", "season": 1, "episode": 5,
              "file": "/data/media/tv/Pluribus (2025)/Season 01/Pluribus - S01E05.mkv"}
        match = mc.match_episode_to_sonarr(ep)
        self.assertEqual(match["series"]["id"], 108)
        mc.get_sonarr_episode.assert_called_with(108, 1, 5)

    def test_no_path_falls_back_to_title(self):
        mc = self._cleanup()
        ep = {"show_title": "PLUR1BUS", "season": 1, "episode": 5, "file": None}
        self.assertEqual(mc.match_episode_to_sonarr(ep)["series"]["id"], 108)

    def test_unrelated_path_returns_none(self):
        self.assertIsNone(MediaCleanup._match_series_by_path(self.SERIES, "/tv/Other/S01E01.mkv"))


if __name__ == "__main__":
    unittest.main()
