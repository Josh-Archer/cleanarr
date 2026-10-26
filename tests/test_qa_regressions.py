"""Regression tests rebuilt from QA repros cleanarr#79, #80, #81."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

os.environ.setdefault("CLEANARR_LOG_FILE", os.path.join(tempfile.gettempdir(), "cleanarr_qa_test.log"))

from cleanarr import emby_client  # noqa: E402
from cleanarr.adapters.servarr import ServarrClient  # noqa: E402
from cleanarr.policy import decide_exact_item_delete, user_names_match  # noqa: E402


class TestExactUserMatching(unittest.TestCase):
    """cleanarr#79: user matching must be exact, not regex substring."""

    def test_required_user_not_matched_by_substring(self):
        decision = decide_exact_item_delete(
            ["ann", "bob"], {"joanna": True, "bob": True}, policy="require_all_users"
        )
        self.assertFalse(decision.allowed)

    def test_exact_and_case_normalized(self):
        self.assertTrue(user_names_match(" Ann ", "ann"))
        self.assertFalse(user_names_match("ann", "joanna"))
        self.assertFalse(user_names_match("joanna", "ann"))
        self.assertFalse(user_names_match("a.n", "ann"))
        self.assertFalse(user_names_match("", ""))


def _movie():
    return {"Id": "m1", "Name": "Film", "Type": "Movie", "ProductionYear": 2020,
            "Path": "/media/movies/Film (2020)/film.mkv", "ProviderIds": {"Imdb": "tt1"}}


def _episode():
    return {"Id": "e1", "Name": "Pilot", "Type": "Episode", "SeriesName": "Show",
            "ParentIndexNumber": 1, "IndexNumber": 1, "Path": "/media/tv/Show/S01E01.mkv"}


class TestEmbyWatchedSetIncludesAllUsers(unittest.TestCase):
    """cleanarr#80: one Emby watcher of three must not satisfy require_all_users."""

    def test_movie_watched_by_one_of_three_is_blocked(self):
        items = {"alice": [_movie()], "bob": [], "carol": []}
        [movie] = emby_client.build_watched_movies(items)
        self.assertEqual(movie["watched_by"], {"alice": True, "bob": False, "carol": False})
        decision = decide_exact_item_delete([], movie["watched_by"], policy="require_all_users")
        self.assertFalse(decision.allowed)

    def test_episode_watched_by_one_of_three_is_blocked(self):
        items = {"alice": [_episode()], "bob": [], "carol": []}
        [ep] = emby_client.build_watched_episodes(items)
        decision = decide_exact_item_delete([], ep["watched_by"], policy="require_all_users")
        self.assertFalse(decision.allowed)

    def test_all_three_watched_is_allowed(self):
        items = {u: [_movie()] for u in ("alice", "bob", "carol")}
        [movie] = emby_client.build_watched_movies(items)
        self.assertTrue(decide_exact_item_delete([], movie["watched_by"]).allowed)


class TestServarrSubpathBaseurl(unittest.TestCase):
    """cleanarr#81: endpoint must be appended to a subpath baseurl."""

    def _url(self, baseurl, endpoint):
        session = MagicMock()
        ok = MagicMock(status_code=200, text="{}")
        ok.json.return_value = {}
        session.get.return_value = ok
        client = ServarrClient("Sonarr", {"baseurl": baseurl, "apikey": "k"}, session=session,
                               missing_key_env="CLEANARR_SONARR_APIKEY")
        client.request(endpoint)
        return session.get.call_args[0][0]

    def test_subpath_without_trailing_slash(self):
        self.assertEqual(self._url("http://h/sonarr", "api/v3/series/1"), "http://h/sonarr/api/v3/series/1")
        self.assertEqual(self._url("http://h/api/v3", "series/1"), "http://h/api/v3/series/1")

    def test_trailing_and_leading_slashes(self):
        self.assertEqual(self._url("http://h/api/v3/", "/series/1"), "http://h/api/v3/series/1")


if __name__ == "__main__":
    unittest.main()
