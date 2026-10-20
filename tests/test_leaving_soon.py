import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from cleanarr import cleanup
from cleanarr import webhook_app


class TestLeavingSoonState(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tempdir.name, "leaving-soon.json")
        self.config = patch.dict(cleanup.CONFIG, {
            "plex": {"baseurl": "http://plex", "token": "token"},
            "sonarr": {"baseurl": "http://sonarr", "apikey": "key"},
            "radarr": {"baseurl": "http://radarr", "apikey": "key"},
            "transmission": {
                "host": "transmission", "port": 9091,
                "username": "user", "password": "pass",
                "rpc_timeout_seconds": 90,
            },
            "grace_days": 2,
            "leaving_soon_collection": None,
            "leaving_soon_state_file": self.state_path,
            "dry_run": False,
        })
        self.config.start()
        self.plex_patch = patch("cleanarr.cleanup.PlexServer")
        self.transmission_patch = patch("cleanarr.cleanup.TransmissionClient")
        self.requests_patch = patch("cleanarr.cleanup.requests")
        self.plex_patch.start()
        self.transmission_patch.start()
        self.requests_patch.start()
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(self.config.stop)
        self.addCleanup(self.plex_patch.stop)
        self.addCleanup(self.transmission_patch.stop)
        self.addCleanup(self.requests_patch.stop)
        self.cleaner = cleanup.MediaCleanup()
        self.cleaner.plex_source = MagicMock()

    def test_new_pending_expired_and_removed_transitions(self):
        key = "plex:123"
        self.assertFalse(self.cleaner.leaving_soon_allows_delete(key, "Example", 123))
        with open(self.state_path, encoding="utf-8") as state_file:
            state = json.load(state_file)
        datetime.fromisoformat(state[key])
        self.assertFalse(self.cleaner.leaving_soon_allows_delete(key, "Example", 123))

        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        state[key] = old
        with open(self.state_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file)
        self.assertTrue(self.cleaner.leaving_soon_allows_delete(key, "Example", 123))

        self.cleaner._leaving_soon_eligible.clear()
        self.cleaner._sync_leaving_soon()
        with open(self.state_path, encoding="utf-8") as state_file:
            self.assertEqual(json.load(state_file), {})

    def test_zero_grace_preserves_immediate_delete_without_state(self):
        cleanup.CONFIG["grace_days"] = 0
        self.assertTrue(self.cleaner.leaving_soon_allows_delete("plex:123", "Example", 123))
        self.assertFalse(os.path.exists(self.state_path))

    def test_collection_sync_runs_during_dry_run(self):
        cleanup.CONFIG["dry_run"] = True
        cleanup.CONFIG["leaving_soon_collection"] = "Leaving Soon"
        self.cleaner.media_source = "plex"
        self.assertFalse(self.cleaner.leaving_soon_allows_delete("plex:123", "Example", 123))
        self.cleaner.plex_source.sync_leaving_soon_collection.assert_called_once_with(
            "Leaving Soon", ["123"], dry_run=True
        )

    def test_webhook_finished_movie_obeys_grace_period(self):
        item = MagicMock()
        item.type = "movie"
        item.title = "Example"
        item.year = 2024
        item.locations = ["/movies/example.mkv"]
        item.guid = "tmdb://1234"
        item.ratingKey = 123
        self.cleaner.plex.fetchItem.return_value = item
        match = {"movie": {"id": 44, "tags": []}, "file_id": 55}
        with patch.object(webhook_app, "_get_media_cleanup", return_value=self.cleaner), \
             patch.object(webhook_app, "_record_webhook_decision"), \
             patch.object(webhook_app.time, "sleep"), \
             patch.object(self.cleaner, "_get_watch_status", return_value={"alice": True}), \
             patch.object(self.cleaner, "match_movie_to_radarr", return_value=match), \
             patch.object(self.cleaner, "get_radarr_tags", return_value=[]), \
             patch.object(self.cleaner, "get_user_tags", return_value=[]), \
             patch.object(self.cleaner, "should_delete_media", return_value=True), \
             patch.object(self.cleaner, "delete_radarr_movie_file") as delete_file:
            webhook_app._background_process_finished({
                "event": "media.scrobble",
                "metadata": {"type": "movie", "ratingKey": "123"},
            })

        delete_file.assert_not_called()
        with open(self.state_path, encoding="utf-8") as state_file:
            self.assertIn("plex:123", json.load(state_file))

    def test_removed_movie_with_existing_file_is_not_deleted(self):
        cleanup.CONFIG["grace_days"] = 0
        movie_path = os.path.join(self.tempdir.name, "example.mkv")
        with open(movie_path, "w", encoding="utf-8") as media_file:
            media_file.write("media")
        movie = {
            "id": 44, "title": "Example", "year": 2024, "tags": [],
            "movieFile": {"id": 55, "path": movie_path},
        }
        with patch.object(webhook_app, "_get_media_cleanup", return_value=self.cleaner), \
             patch.object(webhook_app, "_record_webhook_decision"), \
             patch.object(webhook_app, "_log_webhook_cleanup_outcome"), \
             patch.object(self.cleaner, "_radarr_request", return_value=[]), \
             patch.object(self.cleaner, "get_radarr_movies", return_value=[movie]), \
             patch.object(self.cleaner, "get_radarr_tags", return_value=[]), \
             patch.object(self.cleaner, "delete_radarr_movie", return_value=True) as delete_movie, \
             patch.object(self.cleaner, "delete_radarr_movie_file", return_value=True) as delete_file:
            webhook_app._background_process_removed({
                "event": "library.deleted",
                "metadata": {"type": "movie", "title": "Example", "year": 2024,
                             "ratingKey": "123", "guid": "tmdb://1"},
            })
        delete_movie.assert_not_called()
        delete_file.assert_not_called()

    def test_webhook_removed_movie_honors_grace_and_persists_removed_marker(self):
        movie = {"id": 44, "title": "Example", "year": 2024, "tags": []}
        with patch.object(webhook_app, "_get_media_cleanup", return_value=self.cleaner), \
             patch.object(webhook_app, "_record_webhook_decision"), \
             patch.object(self.cleaner, "_radarr_request", return_value=[]), \
             patch.object(self.cleaner, "get_radarr_movies", return_value=[movie]), \
             patch.object(self.cleaner, "get_radarr_tags", return_value=[]), \
             patch.object(self.cleaner, "delete_radarr_movie", return_value=True) as delete_movie, \
             patch.object(self.cleaner, "delete_radarr_movie_file", return_value=True) as delete_file:
            webhook_app._background_process_removed({
                "event": "library.remove",
                "metadata": {"type": "movie", "title": "Example", "year": 2024,
                             "ratingKey": "123", "guid": "tmdb://1"},
            })
        delete_movie.assert_not_called()
        delete_file.assert_not_called()
        with open(self.state_path, encoding="utf-8") as state_file:
            state = json.load(state_file)
        self.assertEqual(state["plex:123"]["source"], "removed_event")
        self.assertEqual(state["plex:123"]["media_type"], "movie")
        self.cleaner._sync_leaving_soon()
        with open(self.state_path, encoding="utf-8") as state_file:
            self.assertIn("plex:123", json.load(state_file))

    def test_removed_episode_never_deletes_series(self):
        series = {"id": 99, "title": "Example Show", "tags": []}
        with patch.object(webhook_app, "_get_media_cleanup", return_value=self.cleaner), \
             patch.object(webhook_app, "_record_webhook_decision"), \
             patch.object(self.cleaner, "_sonarr_request", return_value=[]), \
             patch.object(self.cleaner, "get_sonarr_series", return_value=[series]), \
             patch.object(self.cleaner, "get_sonarr_tags", return_value=[]), \
             patch.object(self.cleaner, "delete_sonarr_series", return_value=True) as delete_series:
            webhook_app._background_process_removed({
                "event": "library.remove",
                "metadata": {"type": "episode", "grandparentTitle": "Example Show",
                             "title": "Episode 1", "ratingKey": "321", "guid": "tvdb://9"},
            })
        delete_series.assert_not_called()

    def test_dry_run_does_not_write_state(self):
        cleanup.CONFIG["dry_run"] = True
        self.assertFalse(self.cleaner.leaving_soon_allows_delete("plex:123", "Example", 123))
        self.assertFalse(os.path.exists(self.state_path))
        self.cleaner._sync_leaving_soon()
        self.assertFalse(os.path.exists(self.state_path))

    def test_prune_skipped_after_abort_or_partial_failure(self):
        for outcome in (cleanup.OUTCOME_ABORTED, cleanup.OUTCOME_PARTIAL_FAILURE):
            with self.subTest(outcome=outcome):
                state = {"plex:123": datetime.now(timezone.utc).isoformat()}
                with open(self.state_path, "w", encoding="utf-8") as state_file:
                    json.dump(state, state_file)
                self.cleaner.run_outcome = outcome
                self.cleaner._leaving_soon_eligible.clear()
                self.cleaner._sync_leaving_soon()
                with open(self.state_path, encoding="utf-8") as state_file:
                    self.assertEqual(json.load(state_file), state)


class TestPlexLeavingSoonDryRun(unittest.TestCase):
    def test_dry_run_makes_no_plex_mutations(self):
        from cleanarr.adapters.plex import PlexMediaSource
        server = MagicMock()
        item = MagicMock(ratingKey=1)
        section = MagicMock()
        item.section.return_value = section
        server.fetchItem.return_value = item
        server.library.sections.return_value = [section]
        existing = MagicMock(title="Leaving Soon")
        existing.items.return_value = []
        for collections in ([], [existing]):
            section.collections.return_value = collections
            PlexMediaSource(server, {}).sync_leaving_soon_collection(
                "Leaving Soon", ["1"], dry_run=True
            )
        section.createCollection.assert_not_called()
        for method in ("addItems", "removeItems", "modeUpdate", "visibility"):
            getattr(existing, method).assert_not_called()


if __name__ == "__main__":
    unittest.main()
