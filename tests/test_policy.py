"""Shared cleanup policy tests for scheduled jobs and webhook processing."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

os.environ["CLEANARR_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "cleanarr_policy_test.log")

from cleanarr import cleanup as cleanarr  # noqa: E402
from cleanarr import webhook_app  # noqa: E402
from cleanarr.policy import (  # noqa: E402
    collect_watch_ahead_candidates,
    decide_exact_item_delete,
    household_policy_allows_delete,
    ids_include_any_label,
    protected_ids_from_tags,
    repeated_io_error_disposition,
    torrent_cleanup_decision,
    user_tags_from_ids,
)


# user_tags, watched_by, policy, primary, household, expected allow
EXACT_ITEM_CASES = [
    ([], {"alice": True, "bob": False}, "require_all_users", "", set(), False),
    (["alice"], {"alice": True, "bob": False}, "require_all_users", "", set(), True),
    (["alice", "bob"], {"alice": True, "bob": False}, "require_all_users", "", set(), False),
    (
        ["alice", "bob", "carol"],
        {"alice": True, "bob": True, "carol": False},
        "majority",
        "",
        set(),
        True,
    ),
    (["alice", "bob"], {"bob": True}, "primary_user_only", "alice", set(), False),
    (["alice", "bob"], {"alice": True}, "primary_user_only", "alice", set(), True),
    ([], {"alice": False}, "require_all_users", "", set(), False),
    (["alice"], {"alice": True}, "require_all_users", "", {"alice", "bob"}, False),
    ([], {"alice": True}, "require_all_users", "", {"alice"}, True),
]


class TestPurePolicy(unittest.TestCase):
    def test_exact_item_cases(self):
        for user_tags, watched_by, policy, primary, household, expected in EXACT_ITEM_CASES:
            decision = decide_exact_item_delete(
                user_tags,
                watched_by,
                policy=policy,
                primary_user=primary,
                household_users=household,
            )
            self.assertEqual(
                decision.allowed,
                expected,
                msg=f"{user_tags} {watched_by} {policy} {primary} {household}",
            )

    def test_watch_ahead_requires_two_episode_gap_and_household(self):
        candidates = collect_watch_ahead_candidates(
            {"Alice": {(1, 5)}},
            [(1, number) for number in range(1, 6)],
        )
        self.assertEqual(set(candidates), {(1, 1), (1, 2), (1, 3)})
        self.assertEqual(candidates[(1, 1)]["users_ahead"], {"alice"})
        self.assertEqual(candidates[(1, 1)]["later_by_user"]["alice"], [5])
        allowed, detail = household_policy_allows_delete(
            ["alice", "bob"],
            sorted(candidates[(1, 1)]["users_ahead"]),
            policy="require_all_users",
        )
        self.assertFalse(allowed)
        self.assertIn("require_all_users", detail)

    def test_watch_ahead_ignores_episode_user_already_watched(self):
        candidates = collect_watch_ahead_candidates(
            {"alice": {(1, 1), (1, 4)}},
            [(1, 1), (1, 2), (1, 4)],
        )
        self.assertNotIn((1, 1), candidates)
        self.assertIn((1, 2), candidates)

    def test_protected_labels_match_job_and_webhook_rules(self):
        tags = [
            {"id": 1, "label": "kids"},
            {"id": 2, "label": "10 - safe"},
            {"id": 3, "label": "alice"},
        ]
        self.assertEqual(protected_ids_from_tags(tags), {1, 2})
        # Finished playback protects safe and kids. Removal of a movie
        # historically checks safe only.
        self.assertTrue(ids_include_any_label(tags, [1], ("safe", "kids")))
        self.assertFalse(ids_include_any_label(tags, [1], ("safe",)))
        self.assertTrue(ids_include_any_label(tags, [2], ("safe",)))
        self.assertEqual(user_tags_from_ids(tags, {1, 2, 3}), ["alice"])

    def test_torrent_label_and_category_policy(self):
        allowed, kind, labels = torrent_cleanup_decision(
            labels=set(),
            category="sonarr",
            required_labels={"sonarr", "radarr"},
            allowed_categories=set(),
        )
        self.assertTrue(allowed)
        self.assertIsNone(kind)
        self.assertIn("sonarr", labels)

        allowed, kind, _ = torrent_cleanup_decision(
            labels={"ebooks"},
            category="books",
            required_labels={"sonarr"},
            allowed_categories=set(),
        )
        self.assertFalse(allowed)
        self.assertEqual(kind, "labels")

        allowed, kind, _ = torrent_cleanup_decision(
            labels={"sonarr"},
            category="books",
            required_labels=set(),
            allowed_categories={"sonarr", "radarr"},
        )
        self.assertFalse(allowed)
        self.assertEqual(kind, "category")

    def test_repeated_io_error_disposition(self):
        self.assertEqual(
            repeated_io_error_disposition(count=5, threshold=2, active=True, dry_run=False),
            "leave_active",
        )
        self.assertEqual(
            repeated_io_error_disposition(count=1, threshold=2, active=False, dry_run=False),
            "below_threshold",
        )
        self.assertEqual(
            repeated_io_error_disposition(count=2, threshold=2, active=False, dry_run=True),
            "dry_run",
        )
        self.assertEqual(
            repeated_io_error_disposition(count=2, threshold=2, active=False, dry_run=False),
            "remove",
        )


class TestJobAndWebhookSharePolicy(unittest.TestCase):
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
                "dry_run": False,
                "disable_torrent_cleanup": True,
                "transmission_io_error_cleanup_enabled": False,
                "multi_user_delete_policy": "require_all_users",
                "primary_user": "",
                "household_users": set(),
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

    def test_exact_item_policy_matches_job_and_webhook(self):
        media = {"title": "Shared", "show_title": "Shared", "season": 1, "episode": 1}
        for user_tags, watched_by, policy, primary, household, expected in EXACT_ITEM_CASES:
            decision = decide_exact_item_delete(
                user_tags,
                watched_by,
                policy=policy,
                primary_user=primary,
                household_users=household,
            )
            config = {
                "multi_user_delete_policy": policy,
                "primary_user": primary,
                "household_users": set(household),
            }
            self.assertEqual(decision.allowed, expected)
            self.assertEqual(
                webhook_app.household_delete_allowed(user_tags, watched_by, config),
                expected,
            )
            with patch.dict(cleanarr.CONFIG, config, clear=False):
                self.assertEqual(
                    self.cleanup.should_delete_media(media, user_tags, watched_by),
                    expected,
                )

    def test_user_tags_match_between_policy_and_job(self):
        tags = [
            {"id": 1, "label": "safe"},
            {"id": 2, "label": "kids"},
            {"id": 3, "label": "user1"},
            {"id": 4, "label": " 10 - user2 "},
        ]
        self.assertEqual(
            self.cleanup.get_user_tags(tags, {3, 4}),
            user_tags_from_ids(tags, {3, 4}),
        )

    def test_dry_run_episode_decision_stays_would_delete(self):
        cleanarr.CONFIG["dry_run"] = True
        deleted = self.cleanup._delete_episode_and_cleanup(
            "Show S1E1",
            "standard watched",
            9,
            8,
        )
        self.assertTrue(deleted)
        self.assertEqual(len(self.cleanup.dry_run_report.items), 1)
        item = self.cleanup.dry_run_report.items[0]
        self.assertEqual(item["reason_code"], "dry-run")
        self.assertEqual(item["action"], "would_delete")
        self.assertEqual(item["media_title"], "Show S1E1")
        self.assertEqual(item["media_type"], "episode")
        self.assertTrue(self.cleanup.run_summary["tv_deletions"])


class TestAdapters(unittest.TestCase):
    def test_servarr_get_and_empty_delete(self):
        from cleanarr.adapters.servarr import ServarrClient

        session = MagicMock()
        ok = MagicMock(status_code=200, text='{"name": "show"}')
        ok.json.return_value = {"name": "show"}
        session.get.return_value = ok
        client = ServarrClient(
            "Sonarr",
            {"baseurl": "http://sonarr:8989/api/v3/", "apikey": "key"},
            session=session,
            missing_key_env="CLEANARR_SONARR_APIKEY",
        )
        self.assertEqual(client.request("series"), {"name": "show"})
        session.get.assert_called_with("http://sonarr:8989/api/v3/series", timeout=30)

        empty = MagicMock(status_code=200, text="")
        session.delete.return_value = empty
        self.assertEqual(client.request("episodefile/9", method="DELETE"), {})
        session.delete.assert_called_with(
            "http://sonarr:8989/api/v3/episodefile/9",
            timeout=30,
        )

    def test_servarr_missing_key_does_not_call_session(self):
        from cleanarr.adapters.servarr import ServarrClient

        session = MagicMock()
        client = ServarrClient(
            "Radarr",
            {"baseurl": "http://radarr:7878/api/v3/", "apikey": ""},
            session=session,
            missing_key_env="CLEANARR_RADARR_APIKEY",
        )
        self.assertIsNone(client.request("movie"))
        session.get.assert_not_called()

    def test_jellyfin_adapter_reads_emby_compatible_api(self):
        from cleanarr.adapters.jellyfin import JellyfinMediaSource

        session = MagicMock()
        with patch(
            "cleanarr.adapters.jellyfin.emby_client.load_watched_state",
            return_value=([], []),
        ) as load:
            source = JellyfinMediaSource(
                session,
                {"baseurl": "http://jellyfin:8096", "apikey": "key", "users": {"alice"}},
            )
            self.assertEqual(source.load_watched_state(), ([], []))
        load.assert_called_once_with(
            session,
            "http://jellyfin:8096",
            "key",
            allowed_usernames={"alice"},
        )

    def test_plex_watch_status_records_history_and_fallback(self):
        from cleanarr.adapters.plex import PlexMediaSource

        owner = MagicMock(username="Owner", title="Owner")
        managed = MagicMock(username="bob", title="Bob", id=7)
        owner.users.return_value = [managed]
        server = MagicMock()
        server.myPlexAccount.return_value = owner

        def history(**kwargs):
            if kwargs.get("accountID") == 1:
                return []
            if kwargs.get("accountID") == 7:
                return ["played"]
            return []

        server.history.side_effect = history
        item = MagicMock(ratingKey=5, title="Movie", isWatched=True)
        evidence = {}
        status = PlexMediaSource(server, evidence).watch_status(item)
        self.assertEqual(status, {"Owner": True, "bob": True})
        self.assertEqual(evidence[5]["Owner"], "isWatched_fallback")
        self.assertEqual(evidence[5]["bob"], "history")

    def test_transmission_adapter_delegates_remove(self):
        from cleanarr.adapters.transmission import (
            TransmissionClientAdapter,
            get_torrent_category,
            transmission_labels,
        )

        client = MagicMock()
        torrent = MagicMock(labels=["Sonarr", " "], download_dir="/media/downloads/radarr")
        adapter = TransmissionClientAdapter(client)
        adapter.remove_torrent(4, delete_data=False)
        client.remove_torrent.assert_called_once_with(4, delete_data=False)
        self.assertEqual(transmission_labels(torrent), {"sonarr"})
        self.assertEqual(get_torrent_category(torrent), "radarr")


if __name__ == "__main__":
    unittest.main()


class TestDecideTaggedUserDelete(unittest.TestCase):
    def test_tagged_watcher_allows(self):
        from cleanarr.policy import decide_tagged_user_delete
        self.assertTrue(decide_tagged_user_delete(["josh"], {"josh": True, "gawly": False}).allowed)

    def test_untagged_never_allows(self):
        from cleanarr.policy import decide_tagged_user_delete
        d = decide_tagged_user_delete([], {"josh": True})
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "no_user_tags")

    def test_protected_labels_are_not_users(self):
        from cleanarr.policy import decide_tagged_user_delete
        self.assertFalse(decide_tagged_user_delete(["safe", "kids"], {"safe": True, "kids": True}).allowed)
