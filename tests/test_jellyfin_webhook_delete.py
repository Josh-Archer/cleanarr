"""Issue #70: Jellyfin is a first-class delete source; unmatched items never delete."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr import cleanup, emby_client, webhook_app

ITEM_ID = "jf-ep-1"


def _jf_item(played):
    return {
        "Id": ITEM_ID,
        "Type": "Episode",
        "Name": "Pilot",
        "SeriesName": "Test Show",
        "ParentIndexNumber": 1,
        "IndexNumber": 4,
        "Path": "/media/tv/Test Show/S01E04.mkv",
        "ProviderIds": {"Tvdb": "999"},
        "UserData": {"Played": played},
    }


def _fake_emby_get(played_by):
    """played_by: {username: True/False/None(None = item not visible)}"""
    users = [{"Id": f"u-{n}", "Name": n} for n in played_by]

    def _get(session, baseurl, apikey, path, params=None):
        if path == "Users":
            return users
        uid = path.split("/")[1]
        name = uid[2:]
        state = played_by[name]
        if state is None:
            return {"Items": []}
        return {"Items": [_jf_item(state)]}

    return _get


def _ev():
    return {
        "event": "media.scrobble",
        "platform": "jellyfin",
        "account": {"title": "alice"},
        "metadata": {
            "type": "episode",
            "item_id": ITEM_ID,
            "title": "Pilot",
            "grandparentTitle": "Test Show",
            "parentIndex": 1,
            "index": 4,
        },
        "payload": {"ItemId": ITEM_ID},
    }


def _make_mc(series_tags=None, tag_list=None):
    with patch.dict(cleanup.CONFIG, {
        "dry_run": True,
        "media_source": "plex",
        "jellyfin": {"baseurl": "http://jf.test", "apikey": "k", "users": []},
        "plex": {"baseurl": "http://mock-plex:32400", "token": "t"},
        "sonarr": {"baseurl": "http://mock-sonarr:8989/api/v3/", "apikey": "s"},
        "radarr": {"baseurl": "http://mock-radarr:7878/api/v3/", "apikey": "r"},
        "disable_torrent_cleanup": True,
    }):
        with patch("cleanarr.cleanup.PlexServer"):
            mc = cleanup.MediaCleanup()
    mc.jellyfin_session = MagicMock()
    mc.match_episode_to_sonarr = MagicMock(return_value={
        "series": {"id": 1, "tags": series_tags or []},
        "episode": {"id": 10, "fileId": 100, "tags": []},
        "file_id": 100,
    })
    mc.get_sonarr_tags = MagicMock(return_value=tag_list or [])
    mc.get_user_tags = MagicMock(return_value=[])
    mc.delete_sonarr_episode_file = MagicMock(return_value=True)
    mc.unmonitor_sonarr_episode = MagicMock(return_value=True)
    mc.remove_torrent_by_file_path = MagicMock()
    mc.remove_from_plex_watchlist = MagicMock()
    mc.leaving_soon_allows_delete = MagicMock(return_value=True)
    mc.plex = MagicMock()
    return mc


class TestJellyfinDeleteSource(unittest.TestCase):
    def setUp(self):
        webhook_app.reset_dry_run_report()

    def _run(self, mc, played_by, jf_deletions="true"):
        emitted = []
        with patch.object(emby_client, "emby_get", side_effect=_fake_emby_get(played_by)), \
                patch.dict(cleanup.CONFIG, {
                    "household_policy": "require_all_users",
                    "jellyfin": {"baseurl": "http://jf.test", "apikey": "k", "users": []},
                }), \
                patch.dict(os.environ, {"CLEANARR_HOUSEHOLD_USERS": "",
                                        "CLEANARR_JELLYFIN_DELETIONS": jf_deletions}):
            webhook_app._process_jellyfin_finished(
                mc, _ev(), lambda *a, **k: emitted.append(a), lambda ep: None
            )
        return emitted

    def test_get_item_watch_state_reads_userdata_played(self):
        with patch.object(emby_client, "emby_get", side_effect=_fake_emby_get({"alice": True, "bob": False, "kid": None})):
            item, watched = emby_client.get_item_watch_state(MagicMock(), "http://jf", "k", ITEM_ID)
        self.assertEqual(item["Id"], ITEM_ID)
        self.assertEqual(watched, {"alice": True, "bob": False, "kid": False})

    def test_all_users_watched_deletes(self):
        mc = _make_mc()
        emitted = self._run(mc, {"alice": True, "bob": True})
        mc.delete_sonarr_episode_file.assert_called_once_with(100)
        self.assertTrue(emitted and emitted[0][0] is True)

    def test_partially_watched_is_kept(self):
        mc = _make_mc()
        self._run(mc, {"alice": True, "bob": False})
        mc.delete_sonarr_episode_file.assert_not_called()

    def test_user_who_cannot_see_item_counts_as_unwatched(self):
        mc = _make_mc()
        self._run(mc, {"alice": True, "bob": None})
        mc.delete_sonarr_episode_file.assert_not_called()

    def test_safe_and_kids_tags_respected(self):
        for label in ("safe", "kids"):
            with self.subTest(label=label):
                mc = _make_mc(series_tags=[7], tag_list=[{"id": 7, "label": label}])
                self._run(mc, {"alice": True, "bob": True})
                mc.delete_sonarr_episode_file.assert_not_called()

    def test_grace_period_respected(self):
        mc = _make_mc()
        mc.leaving_soon_allows_delete = MagicMock(return_value=False)
        self._run(mc, {"alice": True, "bob": True})
        mc.leaving_soon_allows_delete.assert_called_once()
        mc.delete_sonarr_episode_file.assert_not_called()

    def test_unresolvable_jellyfin_item_never_deletes(self):
        mc = _make_mc()
        self._run(mc, {"alice": None, "bob": None})
        mc.match_episode_to_sonarr.assert_not_called()
        mc.delete_sonarr_episode_file.assert_not_called()

    def test_jellyfin_deletions_flag_requires_exact_lowercase_true(self):
        """Only the exact string 'true' enables Jellyfin deletes (cleanarr#70)."""
        for off in ("1", "yes", "TRUE", "True", "", " true", "false"):
            with self.subTest(value=off):
                with patch.dict(os.environ, {"CLEANARR_JELLYFIN_DELETIONS": off}):
                    self.assertFalse(webhook_app._jellyfin_deletions_enabled())
        with patch.dict(os.environ, {"CLEANARR_JELLYFIN_DELETIONS": "true"}):
            self.assertTrue(webhook_app._jellyfin_deletions_enabled())

    def test_jellyfin_deletions_default_off_is_dry_run(self):
        """CLEANARR_JELLYFIN_DELETIONS unset -> policy runs, nothing mutated."""
        mc = _make_mc()
        env = {k: v for k, v in os.environ.items() if k != "CLEANARR_JELLYFIN_DELETIONS"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(webhook_app._jellyfin_deletions_enabled())
        for off in ("", "false", "0", "no", "1", "yes", "TRUE"):
            with self.subTest(value=off):
                mc = _make_mc()
                emitted = self._run(mc, {"alice": True, "bob": True}, jf_deletions=off)
                mc.delete_sonarr_episode_file.assert_not_called()
                mc.unmonitor_sonarr_episode.assert_not_called()
                mc.leaving_soon_allows_delete.assert_not_called()
                self.assertEqual(emitted, [])

    def test_plex_deletions_flag_does_not_enable_jellyfin(self):
        mc = _make_mc()
        with patch.object(webhook_app, "ENABLE_WEBHOOK_DELETIONS", True):
            self._run(mc, {"alice": True, "bob": True}, jf_deletions="")
        mc.delete_sonarr_episode_file.assert_not_called()

    def test_background_unmatched_plex_item_never_deletes(self):
        """The old watched_by=[] fallback is gone: no Plex match means no delete."""
        mc = MagicMock()
        mc.CONFIG = {"dry_run": True}
        mc.plex.fetchItem.side_effect = Exception("not found")
        mc.plex.search.side_effect = Exception("not found")
        mc.should_delete_media.return_value = True
        mc.match_episode_to_sonarr.return_value = {
            "series": {"id": 1, "tags": []},
            "episode": {"id": 10, "fileId": 100},
            "file_id": 100,
        }
        ev = {
            "event": "media.scrobble",
            "account": {"title": "alice"},
            "metadata": {"type": "episode", "ratingKey": "123", "title": "X",
                         "parentTitle": "Show", "parentIndex": 1, "index": 4},
        }
        with patch.object(webhook_app, "_get_media_cleanup", return_value=mc), \
                patch.object(webhook_app, "ENABLE_WEBHOOK_DELETIONS", True), \
                patch.object(webhook_app, "_append_event"):
            webhook_app._background_process_finished(ev)
        mc.delete_sonarr_episode_file.assert_not_called()
        mc.should_delete_media.assert_not_called()

    def test_background_routes_jellyfin_platform(self):
        with patch.object(webhook_app, "_process_jellyfin_finished") as proc, \
                patch.object(webhook_app, "_get_media_cleanup", return_value=MagicMock()), \
                patch.object(webhook_app, "ENABLE_WEBHOOK_DELETIONS", True), \
                patch.object(webhook_app, "_append_event"):
            webhook_app._background_process_finished(_ev())
        proc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
