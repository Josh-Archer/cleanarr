"""Unit tests for Seerr client, Seerr protection policy, and exclusion tags."""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

os.environ["CLEANARR_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "cleanarr_seerr_test.log")

from cleanarr import cleanup as cleanarr  # noqa: E402
from cleanarr.adapters.seerr import SeerrClient, SeerrError, join_url  # noqa: E402
from cleanarr.policy import (  # noqa: E402
    is_seerr_request_eligible,
    seerr_request_matches_episode,
    seerr_request_matches_movie,
    protected_ids_from_tags,
    protected_labels_for_ids,
    user_tags_from_ids,
    decide_tagged_user_delete,
)


class TestSeerrAdapter(unittest.TestCase):
    def test_join_url(self):
        self.assertEqual(join_url("http://localhost:5055", "api/v1/request"), "http://localhost:5055/api/v1/request")
        self.assertEqual(join_url("http://localhost:5055/", "/api/v1/request"), "http://localhost:5055/api/v1/request")
        self.assertEqual(join_url("http://localhost:5055/api/v1/", "request"), "http://localhost:5055/api/v1/request")

    def test_seerr_client_initialization_and_headers(self):
        client = SeerrClient(baseurl="http://seerr.local:5055/", apikey="secret-key")
        self.assertEqual(client.baseurl, "http://seerr.local:5055/api/v1")
        self.assertEqual(client.session.headers.get("X-Api-Key"), "secret-key")

        # Client with api/v1 already in baseurl
        client2 = SeerrClient(baseurl="http://seerr.local:5055/api/v1", apikey="secret-key")
        self.assertEqual(client2.baseurl, "http://seerr.local:5055/api/v1")

    def test_get_requests_success(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"results": []}'
        mock_resp.json.return_value = {
            "pageInfo": {"pages": 1, "pageSize": 10, "results": 2, "page": 1},
            "results": [{"id": 1, "type": "movie"}, {"id": 2, "type": "tv"}],
        }
        mock_session.request.return_value = mock_resp

        client = SeerrClient(baseurl="http://seerr:5055", apikey="key", session=mock_session)
        data = client.get_requests(take=10, skip=0, filter="all")

        self.assertEqual(data["pageInfo"]["results"], 2)
        self.assertEqual(len(data["results"]), 2)
        mock_session.request.assert_called_once_with(
            "GET",
            "http://seerr:5055/api/v1/request",
            timeout=10,
            params={"take": 10, "skip": 0, "sort": "added", "filter": "all"},
        )

    def test_get_all_requests_pagination(self):
        mock_session = MagicMock()
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.text = '{"results": []}'
        resp1.json.return_value = {
            "pageInfo": {"pages": 2, "pageSize": 2, "results": 3, "page": 1},
            "results": [{"id": 1}, {"id": 2}],
        }
        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.text = '{"results": []}'
        resp2.json.return_value = {
            "pageInfo": {"pages": 2, "pageSize": 2, "results": 3, "page": 2},
            "results": [{"id": 3}],
        }
        mock_session.request.side_effect = [resp1, resp2]

        client = SeerrClient(baseurl="http://seerr:5055", apikey="key", session=mock_session)
        all_reqs = client.get_all_requests(page_size=2)

        self.assertEqual(len(all_reqs), 3)
        self.assertEqual([r["id"] for r in all_reqs], [1, 2, 3])
        self.assertEqual(mock_session.request.call_count, 2)

    def test_get_all_requests_empty_or_no_results(self):
        mock_session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{"results": []}'
        resp.json.return_value = {"pageInfo": {"pages": 1, "results": 0}, "results": []}
        mock_session.request.return_value = resp

        client = SeerrClient(baseurl="http://seerr:5055", apikey="key", session=mock_session)
        all_reqs = client.get_all_requests()
        self.assertEqual(all_reqs, [])

    def test_get_requests_raises_seerr_error_on_401(self):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_resp.raise_for_status.side_effect = RuntimeError("401 Unauthorized")
        mock_session.request.return_value = mock_resp

        client = SeerrClient(baseurl="http://seerr:5055", apikey="wrong-key", session=mock_session, max_retries=1)
        with self.assertRaises(SeerrError) as ctx:
            client.get_requests()
        self.assertIn("401", str(ctx.exception))

    def test_get_requests_retries_transient_error(self):
        mock_session = MagicMock()
        resp_502 = MagicMock()
        resp_502.status_code = 502
        resp_502.text = "Bad Gateway"

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.text = '{"results": []}'
        resp_ok.json.return_value = {"results": [{"id": 42}]}

        mock_session.request.side_effect = [resp_502, resp_ok]

        client = SeerrClient(baseurl="http://seerr:5055", apikey="key", session=mock_session, max_retries=2)
        data = client.get_requests()
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(mock_session.request.call_count, 2)


class TestSeerrPolicy(unittest.TestCase):
    def test_is_seerr_request_eligible_status(self):
        # Status 1 (pending) and 2 (approved) are eligible
        eligible, _ = is_seerr_request_eligible({"status": 1})
        self.assertTrue(eligible)
        eligible, _ = is_seerr_request_eligible({"status": 2})
        self.assertTrue(eligible)
        # Status 3 (declined) is not eligible
        eligible, reason = is_seerr_request_eligible({"status": 3})
        self.assertFalse(eligible)
        self.assertEqual(reason, "declined")
        # Invalid / missing
        eligible, _ = is_seerr_request_eligible({})
        self.assertFalse(eligible)

    def test_is_seerr_request_eligible_window_days(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        recent_date = (now - datetime.timedelta(days=3)).isoformat()
        old_date = (now - datetime.timedelta(days=15)).isoformat()

        req_recent = {"status": 2, "createdAt": recent_date}
        req_old = {"status": 2, "createdAt": old_date}

        # With 7-day window
        eligible_recent, _ = is_seerr_request_eligible(req_recent, window_days=7, now=now)
        self.assertTrue(eligible_recent)

        eligible_old, reason = is_seerr_request_eligible(req_old, window_days=7, now=now)
        self.assertFalse(eligible_old)
        self.assertEqual(reason, "outside_window_or_unprotected")

        # With window_days=None (unlimited window)
        eligible_unlimited, _ = is_seerr_request_eligible(req_old, window_days=None, now=now)
        self.assertTrue(eligible_unlimited)

    def test_is_seerr_request_eligible_protected_users(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old_date = (now - datetime.timedelta(days=20)).isoformat()

        # Requested by protected user (matches username or displayName, case-insensitive)
        req_vip_user = {
            "status": 2,
            "createdAt": old_date,
            "requestedBy": {"username": "Alice", "displayName": "Alice Smith"},
        }
        req_regular_user = {
            "status": 2,
            "createdAt": old_date,
            "requestedBy": {"username": "bob", "displayName": "Bob Jones"},
        }

        protected_users = {"alice", "charlie"}

        # Even though outside 7-day window, Alice is protected
        eligible_vip, reason = is_seerr_request_eligible(
            req_vip_user,
            window_days=7,
            protected_users=protected_users,
            now=now,
        )
        self.assertTrue(eligible_vip)
        self.assertIn("protected_user:Alice", reason)

        # Bob is not protected and outside window
        eligible_regular, reason = is_seerr_request_eligible(
            req_regular_user,
            window_days=7,
            protected_users=protected_users,
            now=now,
        )
        self.assertFalse(eligible_regular)
        self.assertEqual(reason, "outside_window_or_unprotected")

        # Match via displayName
        req_vip_display = {
            "status": 2,
            "createdAt": old_date,
            "requestedBy": {"username": "user123", "displayName": "Charlie"},
        }
        eligible_display, _ = is_seerr_request_eligible(
            req_vip_display,
            window_days=7,
            protected_users=protected_users,
            now=now,
        )
        self.assertTrue(eligible_display)

    def test_is_seerr_request_eligible_clock_skew(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        # 10 minutes in the future (within 1 hour clock skew tolerance)
        future_slight = (now + datetime.timedelta(minutes=10)).isoformat()
        # 3 hours in the future (exceeds clock skew tolerance)
        future_far = (now + datetime.timedelta(hours=3)).isoformat()

        eligible_slight, _ = is_seerr_request_eligible({"status": 2, "createdAt": future_slight}, window_days=7, now=now)
        self.assertTrue(eligible_slight)

        eligible_far, _ = is_seerr_request_eligible({"status": 2, "createdAt": future_far}, window_days=7, now=now)
        self.assertFalse(eligible_far)

    def test_seerr_request_matches_movie(self):
        request = {
            "type": "movie",
            "media": {
                "mediaType": "movie",
                "tmdbId": 12345,
                "imdbId": "tt67890",
            },
        }

        # TMDb match
        self.assertTrue(seerr_request_matches_movie(request, {"tmdbId": 12345}))
        self.assertFalse(seerr_request_matches_movie(request, {"tmdbId": 99999}))

        # IMDb match
        self.assertTrue(seerr_request_matches_movie(request, {"imdbId": "tt67890"}))
        self.assertTrue(seerr_request_matches_movie(request, {"imdbId": "67890"}))
        self.assertFalse(seerr_request_matches_movie(request, {"imdbId": "tt00000"}))

        # Title + Year match fallback
        req_title_year = {
            "type": "movie",
            "media": {"mediaType": "movie", "title": "Inception"},
            "movie": {"title": "Inception", "releaseDate": "2010-07-16"},
        }
        self.assertTrue(seerr_request_matches_movie(req_title_year, {"title": "inception", "year": 2010}))
        self.assertFalse(seerr_request_matches_movie(req_title_year, {"title": "inception", "year": 2020}))
        self.assertFalse(seerr_request_matches_movie(req_title_year, {"title": "different", "year": 2010}))

        # TV request does not match movie
        req_tv = {"type": "tv", "media": {"tmdbId": 12345}}
        self.assertFalse(seerr_request_matches_movie(req_tv, {"tmdbId": 12345}))

    def test_seerr_request_matches_episode(self):
        series = {"title": "Breaking Bad", "tvdbId": 81189, "tmdbId": 1396}
        episode_s1 = {"seasonNumber": 1, "episodeNumber": 1}
        episode_s2 = {"seasonNumber": 2, "episodeNumber": 1}

        # Request entire series (no seasons listed)
        req_all_seasons = {
            "type": "tv",
            "media": {"mediaType": "tv", "tvdbId": 81189},
            "seasons": [],
        }
        self.assertTrue(seerr_request_matches_episode(req_all_seasons, series, episode_s1))
        self.assertTrue(seerr_request_matches_episode(req_all_seasons, series, episode_s2))

        # Request specific season 1 only
        req_s1_only = {
            "type": "tv",
            "media": {"mediaType": "tv", "tvdbId": 81189},
            "seasons": [{"seasonNumber": 1}],
        }
        self.assertTrue(seerr_request_matches_episode(req_s1_only, series, episode_s1))
        self.assertFalse(seerr_request_matches_episode(req_s1_only, series, episode_s2))

        # TMDb matching for TV
        req_tmdb = {
            "type": "tv",
            "media": {"mediaType": "tv", "tmdbId": 1396},
            "seasons": [{"seasonNumber": 1}],
        }
        self.assertTrue(seerr_request_matches_episode(req_tmdb, series, episode_s1))

        # Mismatched series
        series_diff = {"title": "Other", "tvdbId": 99999}
        self.assertFalse(seerr_request_matches_episode(req_s1_only, series_diff, episode_s1))


class TestExclusionTagsPolicy(unittest.TestCase):
    def test_protected_ids_from_tags_with_extra_tags(self):
        tags = [
            {"id": 1, "label": "safe"},
            {"id": 2, "label": "kids"},
            {"id": 3, "label": "keep"},
            {"id": 4, "label": "bob"},
        ]
        # Without extra tags, keep is treated as a regular/user tag
        self.assertEqual(protected_ids_from_tags(tags), {1, 2})
        # With extra tags "keep"
        self.assertEqual(protected_ids_from_tags(tags, extra_tags={"keep"}), {1, 2, 3})

    def test_protected_labels_for_ids_with_extra_tags(self):
        tags = [
            {"id": 1, "label": "safe"},
            {"id": 2, "label": "kids"},
            {"id": 3, "label": "KEEP"},
            {"id": 4, "label": "bob"},
        ]
        self.assertEqual(
            protected_labels_for_ids(tags, [3], extra_tags={"keep"}),
            ["keep"],
        )
        self.assertEqual(
            protected_labels_for_ids(tags, [1, 3], extra_tags={"keep"}),
            ["safe", "keep"],
        )

    def test_user_tags_from_ids_excludes_exclusion_tags(self):
        tags = [
            {"id": 1, "label": "safe"},
            {"id": 2, "label": "keep"},
            {"id": 3, "label": "bob"},
        ]
        # Exclusion tag "keep" must NOT be treated as a household user
        user_tags = user_tags_from_ids(tags, {1, 2, 3}, extra_tags={"keep"})
        self.assertEqual(user_tags, ["bob"])

    def test_decide_tagged_user_delete_ignores_exclusion_tags(self):
        decision = decide_tagged_user_delete(
            ["keep", "bob"],
            {"bob": True},
            policy="require_all_users",
            extra_protected_tags={"keep"},
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(list(decision.user_tags), ["bob"])


class TestMediaCleanupExclusionAndSeerr(unittest.TestCase):
    def setUp(self):
        self.config_patcher = patch.dict(
            cleanarr.CONFIG,
            {
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
                "debug": False,
                "dry_run": True,
                "disable_torrent_cleanup": True,
                "exclusion_tags": {"keep", "archive"},
                "seerr": {
                    "enabled": True,
                    "baseurl": "http://mock-seerr:5055",
                    "apikey": "mock-seerr-key",
                    "window_days": 14,
                    "protected_users": {"vipuser"},
                },
            },
        )
        self.config_patcher.start()

        self.plex_patcher = patch("cleanarr.cleanup.PlexServer")
        self.plex_patcher.start()
        self.trans_patcher = patch("cleanarr.cleanup.TransmissionClient")
        self.trans_patcher.start()
        self.requests_patcher = patch("cleanarr.cleanup.requests")
        self.requests_patcher.start()

        self.cleanup = cleanarr.MediaCleanup()

    def tearDown(self):
        self.config_patcher.stop()
        self.plex_patcher.stop()
        self.trans_patcher.stop()
        self.requests_patcher.stop()

    def test_movie_skipped_due_to_exclusion_tag(self):
        radarr_tags = [{"id": 10, "label": "keep"}]
        self.cleanup.get_radarr_tags = MagicMock(return_value=radarr_tags)

        radarr_movie = {
            "id": 100,
            "title": "Exclusion Movie",
            "year": 2023,
            "tags": [10],
            "movieFile": {"id": 500},
        }
        self.cleanup.match_movie_to_radarr = MagicMock(
            return_value={"movie": radarr_movie, "file_id": 500}
        )
        self.cleanup.delete_radarr_movie_file = MagicMock()

        watched_movies = [
            {
                "title": "Exclusion Movie",
                "year": 2023,
                "watched_by": {"alice": True},
                "file": "/media/movies/Exclusion.Movie.2023.mkv",
            }
        ]
        self.cleanup.get_watched_movies = MagicMock(return_value=watched_movies)

        self.cleanup.process_watched_movies()

        # Must not delete movie file
        self.cleanup.delete_radarr_movie_file.assert_not_called()

        items = self.cleanup.dry_run_report.items
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason_code"], "protected")
        self.assertEqual(items[0]["reason"], "tag_exclusion: keep")
        self.assertIn("Exclusion Movie (2023) [tag_exclusion: keep]", self.cleanup.run_summary.get("protected_skips", []))

    def test_episode_skipped_due_to_exclusion_tag(self):
        sonarr_tags = [{"id": 20, "label": "archive"}]
        self.cleanup.get_sonarr_tags = MagicMock(return_value=sonarr_tags)
        self.cleanup.get_sonarr_series = MagicMock(return_value=[])

        sonarr_series = {"id": 200, "title": "Exclusion Series", "tags": [20]}
        sonarr_episode = {"id": 201, "seasonNumber": 1, "episodeNumber": 1, "tags": []}

        self.cleanup.match_episode_to_sonarr = MagicMock(
            return_value={
                "series": sonarr_series,
                "episode": sonarr_episode,
                "file_id": 600,
            }
        )
        self.cleanup.delete_sonarr_episode_file = MagicMock()

        watched_episodes = [
            {
                "show_title": "Exclusion Series",
                "season": 1,
                "episode": 1,
                "watched_by": {"alice": True},
                "file": "/media/tv/Exclusion Series/S01E01.mkv",
            }
        ]
        self.cleanup.get_watched_episodes = MagicMock(return_value=watched_episodes)

        self.cleanup.process_watched_episodes()

        self.cleanup.delete_sonarr_episode_file.assert_not_called()
        items = self.cleanup.dry_run_report.items
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason_code"], "protected")
        self.assertEqual(items[0]["reason"], "tag_exclusion: archive")

    def test_movie_protected_by_recent_seerr_request(self):
        self.cleanup.get_radarr_tags = MagicMock(return_value=[])
        radarr_movie = {
            "id": 101,
            "title": "Recent Request Movie",
            "year": 2024,
            "tmdbId": 55555,
            "tags": [],
            "movieFile": {"id": 501},
        }
        self.cleanup.match_movie_to_radarr = MagicMock(
            return_value={"movie": radarr_movie, "file_id": 501}
        )
        self.cleanup.delete_radarr_movie_file = MagicMock()

        # Mock Seerr returning a request from 2 days ago
        now = datetime.datetime.now(datetime.timezone.utc)
        recent_request = {
            "id": 1,
            "status": 2,
            "type": "movie",
            "createdAt": (now - datetime.timedelta(days=2)).isoformat(),
            "requestedBy": {"username": "someuser"},
            "media": {"mediaType": "movie", "tmdbId": 55555},
        }
        self.cleanup.seerr_client.get_all_requests = MagicMock(return_value=[recent_request])

        watched_movies = [
            {
                "title": "Recent Request Movie",
                "year": 2024,
                "watched_by": {"alice": True},
                "file": "/media/movies/Recent.Request.2024.mkv",
            }
        ]
        self.cleanup.get_watched_movies = MagicMock(return_value=watched_movies)

        self.cleanup.process_watched_movies()

        self.cleanup.delete_radarr_movie_file.assert_not_called()
        items = self.cleanup.dry_run_report.items
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason_code"], "protected")
        self.assertTrue(items[0]["reason"].startswith("seerr_request: within_window"))
        self.assertEqual(items[0]["details"]["seerr_request_id"], 1)

    def test_movie_protected_by_seerr_protected_user_even_if_outside_window(self):
        self.cleanup.get_radarr_tags = MagicMock(return_value=[])
        radarr_movie = {
            "id": 102,
            "title": "VIP Request Movie",
            "year": 2022,
            "tmdbId": 77777,
            "tags": [],
            "movieFile": {"id": 502},
        }
        self.cleanup.match_movie_to_radarr = MagicMock(
            return_value={"movie": radarr_movie, "file_id": 502}
        )
        self.cleanup.delete_radarr_movie_file = MagicMock()

        now = datetime.datetime.now(datetime.timezone.utc)
        old_request_by_vip = {
            "id": 2,
            "status": 2,
            "type": "movie",
            "createdAt": (now - datetime.timedelta(days=100)).isoformat(),
            "requestedBy": {"username": "vipuser"},
            "media": {"mediaType": "movie", "tmdbId": 77777},
        }
        self.cleanup.seerr_client.get_all_requests = MagicMock(return_value=[old_request_by_vip])

        watched_movies = [
            {
                "title": "VIP Request Movie",
                "year": 2022,
                "watched_by": {"alice": True},
                "file": "/media/movies/VIP.Request.2022.mkv",
            }
        ]
        self.cleanup.get_watched_movies = MagicMock(return_value=watched_movies)

        self.cleanup.process_watched_movies()

        self.cleanup.delete_radarr_movie_file.assert_not_called()
        items = self.cleanup.dry_run_report.items
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason_code"], "protected")
        self.assertTrue(items[0]["reason"].startswith("seerr_request: protected_user"))

    def test_movie_deletes_when_seerr_request_is_outside_window_and_not_protected_user(self):
        self.cleanup.get_radarr_tags = MagicMock(return_value=[])
        radarr_movie = {
            "id": 103,
            "title": "Old Request Movie",
            "year": 2021,
            "tmdbId": 88888,
            "tags": [],
            "movieFile": {"id": 503},
        }
        self.cleanup.match_movie_to_radarr = MagicMock(
            return_value={"movie": radarr_movie, "file_id": 503}
        )
        self.cleanup.delete_radarr_movie_file = MagicMock(return_value=True)
        self.cleanup.unmonitor_radarr_movie = MagicMock()
        self.cleanup.remove_torrent_by_file_path = MagicMock()
        self.cleanup.remove_from_plex_watchlist = MagicMock()

        now = datetime.datetime.now(datetime.timezone.utc)
        old_request_regular = {
            "id": 3,
            "status": 2,
            "type": "movie",
            "createdAt": (now - datetime.timedelta(days=100)).isoformat(),
            "requestedBy": {"username": "regularguy"},
            "media": {"mediaType": "movie", "tmdbId": 88888},
        }
        self.cleanup.seerr_client.get_all_requests = MagicMock(return_value=[old_request_regular])

        watched_movies = [
            {
                "title": "Old Request Movie",
                "year": 2021,
                "watched_by": {"alice": True},
                "file": "/media/movies/Old.Request.2021.mkv",
            }
        ]
        self.cleanup.get_watched_movies = MagicMock(return_value=watched_movies)

        self.cleanup.process_watched_movies()

        # Deletion proceeds because request is outside window
        self.cleanup.delete_radarr_movie_file.assert_called_once_with(503)
        items = self.cleanup.dry_run_report.items
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason_code"], "dry-run")
        self.assertEqual(items[0]["reason"], "standard watched")
        self.assertEqual(items[0]["action"], "would_delete")

    def test_episode_protected_by_seerr_tv_request(self):
        self.cleanup.get_sonarr_tags = MagicMock(return_value=[])
        self.cleanup.get_sonarr_series = MagicMock(return_value=[])

        sonarr_series = {"id": 300, "title": "Protected Show", "tvdbId": 12345, "tags": []}
        sonarr_episode = {"id": 301, "seasonNumber": 1, "episodeNumber": 2, "tags": []}

        self.cleanup.match_episode_to_sonarr = MagicMock(
            return_value={
                "series": sonarr_series,
                "episode": sonarr_episode,
                "file_id": 700,
            }
        )
        self.cleanup.delete_sonarr_episode_file = MagicMock()

        now = datetime.datetime.now(datetime.timezone.utc)
        tv_request = {
            "id": 4,
            "status": 2,
            "type": "tv",
            "createdAt": (now - datetime.timedelta(days=1)).isoformat(),
            "requestedBy": {"username": "tvfan"},
            "media": {"mediaType": "tv", "tvdbId": 12345},
            "seasons": [{"seasonNumber": 1}],
        }
        self.cleanup.seerr_client.get_all_requests = MagicMock(return_value=[tv_request])

        watched_episodes = [
            {
                "show_title": "Protected Show",
                "season": 1,
                "episode": 2,
                "watched_by": {"alice": True},
                "file": "/media/tv/Protected Show/S01E02.mkv",
            }
        ]
        self.cleanup.get_watched_episodes = MagicMock(return_value=watched_episodes)

        self.cleanup.process_watched_episodes()

        self.cleanup.delete_sonarr_episode_file.assert_not_called()
        items = self.cleanup.dry_run_report.items
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason_code"], "protected")
        self.assertTrue(items[0]["reason"].startswith("seerr_request: within_window"))
        self.assertEqual(items[0]["details"]["seerr_request_id"], 4)

    def test_seerr_unreachable_fails_safe_and_skips_deletion(self):
        self.cleanup.get_radarr_tags = MagicMock(return_value=[])
        radarr_movie = {
            "id": 104,
            "title": "Fail Safe Movie",
            "year": 2023,
            "tmdbId": 99999,
            "tags": [],
            "movieFile": {"id": 504},
        }
        self.cleanup.match_movie_to_radarr = MagicMock(
            return_value={"movie": radarr_movie, "file_id": 504}
        )
        self.cleanup.delete_radarr_movie_file = MagicMock()

        # Mock Seerr raising SeerrError
        self.cleanup.seerr_client.get_all_requests = MagicMock(
            side_effect=SeerrError("Connection refused by Seerr host")
        )

        watched_movies = [
            {
                "title": "Fail Safe Movie",
                "year": 2023,
                "watched_by": {"alice": True},
                "file": "/media/movies/Fail.Safe.2023.mkv",
            }
        ]
        self.cleanup.get_watched_movies = MagicMock(return_value=watched_movies)

        self.cleanup.process_watched_movies()

        # Must NOT delete anything when Seerr is unreachable (fail-safe)
        self.cleanup.delete_radarr_movie_file.assert_not_called()

        items = self.cleanup.dry_run_report.items
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason_code"], "error")
        self.assertIn("seerr_unreachable", items[0]["reason"])
        self.assertIn("seerr_skips", self.cleanup.run_summary)
        self.assertTrue(any("seerr_unreachable" in s for s in self.cleanup.run_summary["seerr_skips"]))


if __name__ == "__main__":
    unittest.main()
