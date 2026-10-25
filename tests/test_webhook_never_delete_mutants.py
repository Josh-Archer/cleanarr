"""Issue #70 mutant-killing tests: movies are kept unless fully watched, and
missing Jellyfin config or a missing Plex match never deletes."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr import cleanup, emby_client, webhook_app

ITEM_ID = "jf-1"


def _make_mc():
    with patch.dict(cleanup.CONFIG, {
        "dry_run": True,
        "media_source": "plex",
        "plex": {"baseurl": "http://mock-plex:32400", "token": "t"},
        "sonarr": {"baseurl": "http://mock-sonarr:8989/api/v3/", "apikey": "s"},
        "radarr": {"baseurl": "http://mock-radarr:7878/api/v3/", "apikey": "r"},
        "disable_torrent_cleanup": True,
    }):
        with patch("cleanarr.cleanup.PlexServer"):
            mc = cleanup.MediaCleanup()
    mc.get_radarr_tags = MagicMock(return_value=[])
    mc.get_sonarr_tags = MagicMock(return_value=[])
    mc.get_user_tags = MagicMock(return_value=[])
    mc.leaving_soon_allows_delete = MagicMock(return_value=True)
    mc.delete_radarr_movie_file = MagicMock(return_value=True)
    mc.unmonitor_radarr_movie = MagicMock(return_value=True)
    mc.delete_sonarr_episode_file = MagicMock(return_value=True)
    mc.unmonitor_sonarr_episode = MagicMock(return_value=True)
    mc.remove_torrent_by_file_path = MagicMock()
    mc.remove_from_plex_watchlist = MagicMock()
    mc.match_movie_to_radarr = MagicMock(return_value={"movie": {"id": 5, "tags": []}, "file_id": 50})
    mc.match_episode_to_sonarr = MagicMock(return_value={
        "series": {"id": 1, "tags": []}, "episode": {"id": 10, "fileId": 100, "tags": []}, "file_id": 100,
    })
    mc.plex = MagicMock()
    return mc


def _reasons(rec):
    return [c.kwargs.get("reason") for c in rec.call_args_list]


class TestMoviePolicyKeepsMovie(unittest.TestCase):
    """Kills: `should_delete_media(...)` -> True in _apply_movie_delete_policy."""

    def setUp(self):
        webhook_app.reset_dry_run_report()

    def _run(self, watched_by):
        mc = _make_mc()
        mv = {"title": "Film", "year": 2020, "file": "/m/Film.mkv", "watched_by": watched_by,
              "rating_key": "77", "source": "plex"}
        emitted = []
        with patch.dict(cleanup.CONFIG, {"household_policy": "require_all_users"}), \
                patch.dict(os.environ, {"CLEANARR_HOUSEHOLD_USERS": ""}):
            webhook_app._apply_movie_delete_policy(
                mc, {"account": {"title": "alice"}}, mv,
                {"movie": {"id": 5, "tags": []}, "file_id": 50},
                lambda *a, **k: emitted.append(a),
            )
        return mc, emitted

    def test_partly_watched_movie_is_kept(self):
        mc, emitted = self._run({"alice": True, "bob": False})
        mc.delete_radarr_movie_file.assert_not_called()
        mc.unmonitor_radarr_movie.assert_not_called()
        self.assertFalse([e for e in emitted if e and e[0] is True])

    def test_movie_with_empty_watched_by_is_kept(self):
        mc, emitted = self._run({})
        mc.delete_radarr_movie_file.assert_not_called()
        mc.unmonitor_radarr_movie.assert_not_called()
        self.assertFalse([e for e in emitted if e and e[0] is True])


class TestMissingJellyfinConfigNeverDeletes(unittest.TestCase):
    """Kills: _jellyfin_watch_state returning a watched record with empty config."""

    def setUp(self):
        webhook_app.reset_dry_run_report()

    def _item(self):
        return {"Id": ITEM_ID, "Type": "Episode", "Name": "Pilot", "SeriesName": "Show",
                "ParentIndexNumber": 1, "IndexNumber": 1, "Path": "/tv/Show/S01E01.mkv",
                "UserData": {"Played": True}}

    def _fake_get(self, session, baseurl, apikey, path, params=None):
        if path == "Users":
            return [{"Id": "u-alice", "Name": "alice"}]
        return {"Items": [self._item()]}

    def test_empty_or_missing_baseurl_or_apikey(self):
        configs = {
            "missing_section": None,
            "empty_section": {},
            "empty_baseurl": {"baseurl": "", "apikey": "k"},
            "missing_baseurl": {"apikey": "k"},
            "empty_apikey": {"baseurl": "http://jf.test", "apikey": ""},
            "missing_apikey": {"baseurl": "http://jf.test"},
            "both_empty": {"baseurl": "", "apikey": ""},
        }
        for name, jf in configs.items():
            with self.subTest(config=name):
                mc = _make_mc()
                mc.jellyfin_session = MagicMock()
                mc.CONFIG = {"household_policy": "require_all_users"}
                if jf is not None:
                    mc.CONFIG["jellyfin"] = jf
                ev = {"event": "media.scrobble", "platform": "jellyfin", "account": {"title": "alice"},
                      "metadata": {"type": "episode", "item_id": ITEM_ID, "title": "Pilot"},
                      "payload": {"ItemId": ITEM_ID}}
                with patch.object(emby_client, "emby_get", side_effect=self._fake_get), \
                        patch.object(webhook_app, "_record_webhook_decision") as rec, \
                        patch.dict(os.environ, {"CLEANARR_HOUSEHOLD_USERS": ""}):
                    webhook_app._process_jellyfin_finished(mc, ev, lambda *a, **k: None, lambda ep: None)
                self.assertEqual(_reasons(rec), ["no_media_server_match"])
                mc.delete_sonarr_episode_file.assert_not_called()
                mc.delete_radarr_movie_file.assert_not_called()
                mc.match_episode_to_sonarr.assert_not_called()

    def test_watch_state_helper_returns_nothing_without_config(self):
        mc = MagicMock()
        mc.CONFIG = {"jellyfin": {"baseurl": "", "apikey": ""}}
        with patch.object(emby_client, "emby_get", side_effect=self._fake_get):
            record, watched = webhook_app._jellyfin_watch_state(
                mc, {"platform": "jellyfin", "metadata": {"item_id": ITEM_ID}, "payload": {"ItemId": ITEM_ID}})
        self.assertIsNone(record)
        self.assertEqual(watched, {})


class TestUnmatchedPlexMovieNeverDeletes(unittest.TestCase):
    def setUp(self):
        webhook_app.reset_dry_run_report()

    def test_movie_without_plex_match_is_not_deleted(self):
        mc = MagicMock()
        mc.CONFIG = {"dry_run": True}
        mc.plex.fetchItem.side_effect = Exception("not found")
        mc.plex.search.side_effect = Exception("not found")
        mc.plex.library.sections.side_effect = Exception("not found")
        mc.should_delete_media.return_value = True
        mc.match_movie_to_radarr.return_value = {"movie": {"id": 5, "tags": []}, "file_id": 50}
        ev = {"event": "media.scrobble", "account": {"title": "alice"},
              "metadata": {"type": "movie", "ratingKey": "321", "title": "Film", "year": 2020}}
        with patch.object(webhook_app, "_get_media_cleanup", return_value=mc), \
                patch.object(webhook_app, "ENABLE_WEBHOOK_DELETIONS", True), \
                patch.object(webhook_app, "_append_event"), \
                patch.object(webhook_app, "_record_webhook_decision") as rec:
            webhook_app._background_process_finished(ev)
        self.assertIn("no_media_server_match", _reasons(rec))
        mc.should_delete_media.assert_not_called()
        mc.delete_radarr_movie_file.assert_not_called()
        mc.unmonitor_radarr_movie.assert_not_called()


if __name__ == "__main__":
    unittest.main()
