"""Emby media-source and webhook coverage (issue #16)."""

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
    os.path.join(tempfile.gettempdir(), "cleanarr_emby_test.log"),
)

from cleanarr import emby_client  # noqa: E402
from cleanarr import cleanup as cleanarr  # noqa: E402
from cleanarr import webhook_app  # noqa: E402
import cleanarr.webhook.proxy as proxy_module  # noqa: E402


class TestEmbyEventMapping(unittest.TestCase):
    def test_native_markplayed_is_finished(self):
        flags = emby_client.compute_emby_event_flags("item.markplayed", {})
        self.assertTrue(flags["finished"])
        self.assertTrue(flags["recorded"])
        self.assertTrue(flags["actionable"])

    def test_playback_stop_without_completion_is_not_finished(self):
        flags = emby_client.compute_emby_event_flags(
            "playback.stop",
            {"PlaybackInfo": {"PlayedToCompletion": False}},
        )
        self.assertTrue(flags["stopped"])
        self.assertFalse(flags["finished"])
        self.assertFalse(flags["recorded"])

    def test_playback_stop_with_completion_is_finished(self):
        flags = emby_client.compute_emby_event_flags(
            "playback.stop",
            {"PlaybackInfo": {"PlayedToCompletion": True}},
        )
        self.assertTrue(flags["stopped"])
        self.assertTrue(flags["finished"])
        self.assertTrue(flags["recorded"])

    def test_plugin_itemmarkplayed_is_finished(self):
        flags = emby_client.compute_emby_event_flags("ItemMarkPlayed", {})
        self.assertTrue(flags["finished"])

    def test_map_native_payload(self):
        payload = {
            "Event": "item.markplayed",
            "User": {"Name": "alice", "Id": "u1"},
            "Item": {
                "Name": "Example Movie",
                "Type": "Movie",
                "Id": "m1",
                "ProductionYear": 2020,
                "ProviderIds": {"Imdb": "tt123", "Tmdb": "456"},
            },
        }
        event = emby_client.map_emby_webhook_payload(payload, remote_addr="1.2.3.4")
        self.assertEqual(event["platform"], "emby")
        self.assertEqual(event["event"], "item.markplayed")
        self.assertTrue(event["finished"])
        self.assertEqual(event["account"]["title"], "alice")
        self.assertEqual(event["metadata"]["type"], "movie")
        self.assertEqual(event["metadata"]["guid"], "imdb://tt123")
        self.assertEqual(event["metadata"]["year"], 2020)

    def test_map_plugin_style_episode_payload(self):
        payload = {
            "NotificationType": "ItemMarkPlayed",
            "NotificationUsername": "bob",
            "UserId": "u2",
            "ItemType": "Episode",
            "Name": "Pilot",
            "SeriesName": "Example Show",
            "IndexNumber": 1,
            "ParentIndexNumber": 1,
            "ProviderIds": {"Tvdb": "789"},
        }
        event = emby_client.map_emby_webhook_payload(
            payload, canonical_user="canonical-bob"
        )
        self.assertEqual(event["account"]["title"], "canonical-bob")
        self.assertEqual(event["metadata"]["type"], "episode")
        self.assertEqual(event["metadata"]["parentTitle"], "Example Show")
        self.assertEqual(event["metadata"]["index"], 1)
        self.assertEqual(event["metadata"]["parentIndex"], 1)
        self.assertEqual(event["metadata"]["guid"], "tvdb://789")


class TestEmbyWatchedStateBuilders(unittest.TestCase):
    def test_build_watched_movies_merges_users(self):
        items_by_user = {
            "alice": [
                {
                    "Id": "movie-1",
                    "Name": "The Example",
                    "Type": "Movie",
                    "ProductionYear": 2019,
                    "Path": "/media/movies/The Example (2019)/movie.mkv",
                    "ProviderIds": {"Imdb": "tt999"},
                }
            ],
            "bob": [
                {
                    "Id": "movie-1",
                    "Name": "The Example",
                    "Type": "Movie",
                    "ProductionYear": 2019,
                    "Path": "/media/movies/The Example (2019)/movie.mkv",
                    "ProviderIds": {"Imdb": "tt999"},
                }
            ],
        }
        movies = emby_client.build_watched_movies(items_by_user)
        self.assertEqual(len(movies), 1)
        movie = movies[0]
        self.assertEqual(movie["title"], "The Example")
        self.assertEqual(movie["year"], 2019)
        self.assertEqual(movie["file"], "/media/movies/The Example (2019)/movie.mkv")
        self.assertEqual(movie["watched_by"], {"alice": True, "bob": True})
        self.assertEqual(
            movie["watch_evidence"],
            {"alice": "emby_isplayed", "bob": "emby_isplayed"},
        )
        self.assertEqual(movie["guid"], "imdb://tt999")
        self.assertEqual(movie["source"], "emby")

    def test_build_watched_episodes(self):
        items_by_user = {
            "alice": [
                {
                    "Id": "ep-1",
                    "Name": "Pilot",
                    "Type": "Episode",
                    "SeriesName": "Example Show",
                    "ParentIndexNumber": 1,
                    "IndexNumber": 1,
                    "Path": "/media/tv/Example Show/S01E01.mkv",
                }
            ]
        }
        episodes = emby_client.build_watched_episodes(items_by_user)
        self.assertEqual(len(episodes), 1)
        ep = episodes[0]
        self.assertEqual(ep["show_title"], "Example Show")
        self.assertEqual(ep["season"], 1)
        self.assertEqual(ep["episode"], 1)
        self.assertEqual(ep["watched_by"], {"alice": True})
        self.assertEqual(ep["watch_evidence"]["alice"], "emby_isplayed")


class TestEmbyHttpFixtures(unittest.TestCase):
    def test_load_watched_state_with_mocked_session(self):
        session = MagicMock()

        def _json_response(payload):
            resp = MagicMock()
            resp.content = b"{}"
            resp.raise_for_status = MagicMock()
            resp.json.return_value = payload
            return resp

        users = [
            {"Id": "u-alice", "Name": "alice", "Policy": {"IsDisabled": False}},
            {"Id": "u-bob", "Name": "bob", "Policy": {"IsDisabled": False}},
            {"Id": "u-skip", "Name": "disabled", "Policy": {"IsDisabled": True}},
        ]
        alice_movies = {
            "Items": [
                {
                    "Id": "m1",
                    "Name": "Shared Movie",
                    "Type": "Movie",
                    "ProductionYear": 2021,
                    "Path": "/movies/Shared.mkv",
                    "ProviderIds": {"Tmdb": "111"},
                }
            ]
        }
        bob_movies = {
            "Items": [
                {
                    "Id": "m1",
                    "Name": "Shared Movie",
                    "Type": "Movie",
                    "ProductionYear": 2021,
                    "Path": "/movies/Shared.mkv",
                    "ProviderIds": {"Tmdb": "111"},
                }
            ]
        }
        empty = {"Items": []}

        def get_side_effect(url, headers=None, params=None, timeout=None):
            if url.endswith("/Users") or url.endswith("Users"):
                return _json_response(users)
            if "u-alice" in url and params and params.get("IncludeItemTypes") == "Movie":
                return _json_response(alice_movies)
            if "u-bob" in url and params and params.get("IncludeItemTypes") == "Movie":
                return _json_response(bob_movies)
            return _json_response(empty)

        session.get.side_effect = get_side_effect

        movies, episodes = emby_client.load_watched_state(
            session,
            "http://emby:8096",
            "secret-key",
            allowed_usernames={"alice", "bob"},
        )
        self.assertEqual(len(movies), 1)
        self.assertEqual(movies[0]["watched_by"], {"alice": True, "bob": True})
        self.assertEqual(movies[0]["guid"], "tmdb://111")
        self.assertEqual(episodes, [])
        # Disabled user should never be queried when filtered by name list,
        # and never appear even without a filter.
        called_urls = [call.args[0] for call in session.get.call_args_list]
        self.assertTrue(any("Users" in url for url in called_urls))
        self.assertFalse(any("u-skip" in url for url in called_urls))


class TestMediaCleanupEmbySource(unittest.TestCase):
    def setUp(self):
        self.config_patcher = patch.dict(
            cleanarr.CONFIG,
            {
                "media_source": "emby",
                "plex": {"baseurl": "http://unused", "token": ""},
                "emby": {
                    "baseurl": "http://mock-emby:8096",
                    "apikey": "emby-key",
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

    def test_init_uses_emby_not_plex(self):
        cleaner = cleanarr.MediaCleanup()
        self.assertEqual(cleaner.media_source, "emby")
        self.assertIsNone(cleaner.plex)
        self.MockPlex.assert_not_called()
        self.assertIsNotNone(cleaner.emby_session)

    def test_get_watched_movies_from_emby(self):
        cleaner = cleanarr.MediaCleanup()
        fixtures = (
            [
                {
                    "title": "Shared Movie",
                    "year": 2021,
                    "file": "/movies/Shared.mkv",
                    "watched_by": {"alice": True},
                    "watch_evidence": {"alice": "emby_isplayed"},
                    "guid": "tmdb://111",
                    "guids": ["tmdb://111"],
                    "rating_key": "m1",
                    "source": "emby",
                }
            ],
            [],
        )
        with patch.object(
            cleanarr.emby_client, "load_watched_state", return_value=fixtures
        ) as load:
            movies = cleaner.get_watched_movies()
            episodes = cleaner.get_watched_episodes()
        load.assert_called_once()
        self.assertEqual(len(movies), 1)
        self.assertEqual(movies[0]["title"], "Shared Movie")
        self.assertEqual(episodes, [])
        # Second call uses cache
        with patch.object(
            cleanarr.emby_client, "load_watched_state", return_value=fixtures
        ) as load2:
            cleaner.get_watched_movies()
        load2.assert_not_called()

    def test_should_delete_with_emby_evidence(self):
        cleaner = cleanarr.MediaCleanup()
        media = {
            "title": "Shared Movie",
            "year": 2021,
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


class TestEmbyWebhookRoutes(unittest.TestCase):
    def setUp(self):
        self.client = webhook_app.APP.test_client()

    def _native_payload(self):
        return {
            "Event": "item.markplayed",
            "User": {"Name": "alice", "Id": "u1"},
            "Item": {
                "Name": "Example Movie",
                "Type": "Movie",
                "Id": "m1",
                "ProductionYear": 2020,
                "ProviderIds": {"Imdb": "tt123"},
            },
        }

    def test_emby_webhook_rejects_invalid_token(self):
        with patch.object(
            webhook_app, "EMBY_WEBHOOK_SECRET", "current-secret"
        ), patch.object(
            webhook_app, "EMBY_WEBHOOK_SECRET_PREVIOUS", None
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
        ) as process_actions:
            response = self.client.post(
                "/emby/webhook",
                headers={"X-Webhook-Token": "wrong-secret"},
                json=self._native_payload(),
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {"status": "error", "message": "Unauthorized"},
        )
        process_actions.assert_not_called()

    def test_emby_webhook_accepts_current_and_previous_secrets(self):
        with patch.object(
            webhook_app, "EMBY_WEBHOOK_SECRET", "current-secret"
        ), patch.object(
            webhook_app, "EMBY_WEBHOOK_SECRET_PREVIOUS", "previous-secret"
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app, "_queue_enqueuing_enabled", return_value=False
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
            return_value={"recorded": True},
        ) as process_actions:
            response_current = self.client.post(
                "/emby/webhook",
                headers={"X-Webhook-Token": "current-secret"},
                json=self._native_payload(),
            )
            response_previous = self.client.post(
                "/emby/webhook",
                headers={"X-Emby-Token": "previous-secret"},
                json=self._native_payload(),
            )

        self.assertEqual(response_current.status_code, 200)
        self.assertEqual(response_current.get_json().get("status"), "ok")
        self.assertTrue(response_current.get_json().get("recorded"))
        self.assertEqual(response_previous.status_code, 200)
        self.assertEqual(process_actions.call_count, 2)
        first_event = process_actions.call_args_list[0].args[0]
        self.assertEqual(first_event["platform"], "emby")
        self.assertTrue(first_event["finished"])


class TestEmbyProxyParse(unittest.TestCase):
    def test_parse_emby_webhook_event(self):
        body = b"""{
            "Event": "playback.stop",
            "User": {"Name": "alice", "Id": "u1"},
            "Item": {"Name": "Movie", "Type": "Movie", "Id": "m1"},
            "PlaybackInfo": {"PlayedToCompletion": true}
        }"""
        event = proxy_module._parse_emby_webhook_event(body, "127.0.0.1", "POST")
        self.assertEqual(event["platform"], "emby")
        self.assertTrue(event["finished"])
        self.assertTrue(event["stopped"])
        self.assertEqual(event["account"]["title"], "alice")


if __name__ == "__main__":
    unittest.main()
