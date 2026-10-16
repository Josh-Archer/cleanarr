import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure we can import the local package
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr import webhook_app
lambda_main = importlib.import_module("apps.lambda.main")


class _FakeSqsClient:
    def __init__(self, messages):
        self._messages = list(messages)
        self.deleted = []

    def receive_message(self, **kwargs):
        max_messages = kwargs.get('MaxNumberOfMessages', 1)
        batch = self._messages[:max_messages]
        self._messages = self._messages[max_messages:]
        if not batch:
            return {}
        return {'Messages': batch}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append((QueueUrl, ReceiptHandle))


class TestWebhookAndSqsDryRunReports(unittest.TestCase):
    def setUp(self):
        self.client = webhook_app.APP.test_client()
        webhook_app.reset_dry_run_report()

    def test_webhook_dry_run_emits_json_and_markdown_artifacts(self):
        payload = {
            "event": "media.scrobble",
            "Account": {"id": 1, "title": "alice"},
            "Metadata": {
                "type": "episode",
                "ratingKey": "123",
                "title": "Winter Is Coming",
                "parentTitle": "Game of Thrones",
                "parentIndex": 1,
                "index": 1,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "CLEANARR_DRY_RUN": "true",
                    "CLEANARR_DRY_RUN_REPORT_DIR": tmpdir,
                    "CLEANARR_WEBHOOK_SECRET": "secret-token-xyz",
                },
            ), patch.object(
                webhook_app, "WEBHOOK_SECRET", None
            ), patch.object(
                webhook_app, "_start_background_threads"
            ), patch.object(
                webhook_app, "_queue_enqueuing_enabled", return_value=False
            ), patch.object(
                webhook_app, "_append_event"
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ):
                mock_mc = MagicMock()
                mock_mc.CONFIG = {"dry_run": True}
                mock_mc.plex.fetchItem.side_effect = Exception("not found")
                mock_mc.plex.search.side_effect = Exception("not found")
                mock_mc.delete_sonarr_episode_file.return_value = True
                mock_mc.unmonitor_sonarr_episode.return_value = True
                mock_mc.match_episode_to_sonarr.return_value = {
                    "series": {"id": 1, "tags": []},
                    "episode": {"id": 10, "fileId": 100},
                    "file_id": 100,
                }
                mock_mc.get_sonarr_tags.return_value = []
                mock_mc.get_user_tags.return_value = []
                mock_mc.should_delete_media.return_value = True

                with patch.object(webhook_app, "_get_media_cleanup", return_value=mock_mc):
                    response = self.client.post("/plex/webhook", json=payload)
                    self.assertEqual(response.status_code, 200)

                    # Give background thread a moment to finish processing
                    time.sleep(0.1)

                    # Also invoke with an ignored path to verify multiple decisions in report
                    ignored_payload = {
                        "event": "media.scrobble",
                        "Account": {"id": 2, "title": "bob"},
                        "Metadata": {
                            "type": "movie",
                            "title": "Protected Movie",
                        },
                    }
                    with patch.object(webhook_app, "_ignored_path_for_event", return_value="/movies/protected"):
                        ign_resp = self.client.post("/plex/webhook", json=ignored_payload)
                        self.assertEqual(ign_resp.status_code, 200)

            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            md_path = os.path.join(tmpdir, "cleanarr-dry-run-report.md")

            self.assertTrue(os.path.isfile(json_path), f"Missing {json_path}")
            self.assertTrue(os.path.isfile(md_path), f"Missing {md_path}")

            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)

            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["component"], "webhook")
            self.assertIn("summary", report)
            self.assertIn("users", report)
            self.assertIn("items", report)

            # Check that sensitive secret is redacted
            report_str = json.dumps(report)
            self.assertNotIn("secret-token-xyz", report_str)

            # Check markdown rendering
            with open(md_path, "r", encoding="utf-8") as handle:
                md_content = handle.read()

            self.assertIn("# Cleanarr dry-run report", md_content)
            self.assertIn("- Component: `webhook`", md_content)
            self.assertIn("## Summary", md_content)
            self.assertIn("## Per user / profile", md_content)

    def test_webhook_live_mode_does_not_emit_report_artifacts(self):
        payload = {
            "event": "media.scrobble",
            "Account": {"id": 1, "title": "alice"},
            "Metadata": {
                "type": "episode",
                "ratingKey": "123",
                "title": "Winter Is Coming",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "CLEANARR_DRY_RUN": "false",
                    "CLEANARR_DRY_RUN_REPORT_DIR": tmpdir,
                },
            ), patch.object(
                webhook_app, "WEBHOOK_SECRET", None
            ), patch.object(
                webhook_app, "_start_background_threads"
            ), patch.object(
                webhook_app, "_queue_enqueuing_enabled", return_value=False
            ), patch.object(
                webhook_app, "_append_event"
            ), patch.object(
                webhook_app, "_background_process_finished"
            ):
                response = self.client.post("/plex/webhook", json=payload)
                self.assertEqual(response.status_code, 200)

            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            md_path = os.path.join(tmpdir, "cleanarr-dry-run-report.md")
            self.assertFalse(os.path.exists(json_path))
            self.assertFalse(os.path.exists(md_path))

    def test_sqs_event_records_dry_run_emits_report_artifacts(self):
        records = [
            {
                "messageId": "msg-101",
                "body": json.dumps({
                    "event": "media.scrobble",
                    "metadata": {
                        "type": "episode",
                        "ratingKey": "101",
                        "title": "Show S1E1",
                        "parentTitle": "Test Show",
                        "index": 1,
                        "parentIndex": 1,
                    },
                    "account": {"title": "alice"},
                }),
            },
            {
                "messageId": "msg-102",
                "body": json.dumps({
                    "event": "media.scrobble",
                    "metadata": {
                        "type": "movie",
                        "ratingKey": "102",
                        "title": "Kids Movie",
                        "year": 2022,
                    },
                    "account": {"title": "bob"},
                }),
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "CLEANARR_DRY_RUN": "true",
                    "CLEANARR_DRY_RUN_REPORT_DIR": tmpdir,
                },
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ), patch.object(
                webhook_app, "_append_event"
            ):
                mock_mc = MagicMock()
                mock_mc.CONFIG = {"dry_run": True}
                mock_mc.plex.fetchItem.side_effect = Exception("not found")
                mock_mc.plex.search.side_effect = Exception("not found")
                # For episode: would delete
                mock_mc.delete_sonarr_episode_file.return_value = True
                mock_mc.unmonitor_sonarr_episode.return_value = True
                mock_mc.match_episode_to_sonarr.return_value = {
                    "series": {"id": 1, "tags": []},
                    "episode": {"id": 10, "fileId": 100},
                    "file_id": 100,
                }
                mock_mc.get_sonarr_tags.return_value = []
                mock_mc.get_user_tags.return_value = []
                mock_mc.should_delete_media.return_value = True

                # For movie: protected tag 'kids'
                mock_mc.match_movie_to_radarr.return_value = {
                    "movie": {"id": 2, "tags": [5]},
                    "file_id": 200,
                }
                mock_mc.get_radarr_tags.return_value = [{"id": 5, "label": "kids"}]

                with patch.object(webhook_app, "_get_media_cleanup", return_value=mock_mc):
                    summary = webhook_app.process_sqs_event_records(records)

            self.assertEqual(summary["received"], 2)
            self.assertEqual(summary["processed"], 2)
            self.assertEqual(summary["failed"], 0)

            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            md_path = os.path.join(tmpdir, "cleanarr-dry-run-report.md")

            self.assertTrue(os.path.isfile(json_path))
            self.assertTrue(os.path.isfile(md_path))

            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)

            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["component"], "sqs")
            self.assertGreaterEqual(report["summary"]["would_delete"], 1)
            self.assertGreaterEqual(report["summary"]["skipped"], 1)
            self.assertEqual(report["summary"]["skip_breakdown"].get("kids"), 1)

            with open(md_path, "r", encoding="utf-8") as handle:
                md_content = handle.read()

            self.assertIn("# Cleanarr dry-run report", md_content)
            self.assertIn("- Component: `sqs`", md_content)
            self.assertIn("skip=`kids`", md_content)

    def test_process_sqs_queue_messages_dry_run_emits_report_artifacts(self):
        messages = [
            {
                "MessageId": "sqs-msg-1",
                "ReceiptHandle": "rcpt-1",
                "Body": json.dumps({
                    "event": "media.scrobble",
                    "metadata": {
                        "type": "movie",
                        "ratingKey": "500",
                        "title": "Action Movie",
                        "year": 2021,
                    },
                    "account": {"title": "charlie"},
                }),
            }
        ]
        fake_client = _FakeSqsClient(messages)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "CLEANARR_DRY_RUN": "true",
                    "CLEANARR_DRY_RUN_REPORT_DIR": tmpdir,
                },
            ), patch.object(
                webhook_app, "WEBHOOK_QUEUE_MODE", "sqs"
            ), patch.object(
                webhook_app, "WEBHOOK_QUEUE_POLLING", True
            ), patch.object(
                webhook_app, "WEBHOOK_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/queue"
            ), patch.object(
                webhook_app, "_get_sqs_client", return_value=fake_client
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ), patch.object(
                webhook_app, "_append_event"
            ):
                mock_mc = MagicMock()
                mock_mc.CONFIG = {"dry_run": True}
                mock_mc.plex.fetchItem.side_effect = Exception("not found")
                mock_mc.plex.search.side_effect = Exception("not found")
                mock_mc.match_movie_to_radarr.return_value = {
                    "movie": {"id": 50, "tags": []},
                    "file_id": 500,
                }
                mock_mc.get_radarr_tags.return_value = []
                mock_mc.get_user_tags.return_value = []
                mock_mc.should_delete_media.return_value = True
                mock_mc.delete_radarr_movie_file.return_value = True
                mock_mc.unmonitor_radarr_movie.return_value = True

                with patch.object(webhook_app, "_get_media_cleanup", return_value=mock_mc):
                    summary = webhook_app.process_sqs_queue_messages(max_messages=1)

            self.assertEqual(summary["received"], 1)
            self.assertEqual(summary["processed"], 1)

            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            md_path = os.path.join(tmpdir, "cleanarr-dry-run-report.md")
            self.assertTrue(os.path.isfile(json_path))
            self.assertTrue(os.path.isfile(md_path))

            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["component"], "sqs")
            self.assertEqual(report["summary"]["would_delete"], 1)

    def test_lambda_handler_sqs_records_dry_run_writes_artifacts(self):
        record = {
            "messageId": "lambda-sqs-1",
            "body": json.dumps({
                "event": "media.scrobble",
                "metadata": {
                    "type": "episode",
                    "ratingKey": "888",
                    "title": "Lambda Episode",
                    "parentTitle": "Lambda Show",
                    "index": 1,
                    "parentIndex": 1,
                },
                "account": {"title": "david"},
            }),
        }
        event = {"Records": [record]}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "CLEANARR_DRY_RUN": "true",
                    "CLEANARR_DRY_RUN_REPORT_DIR": tmpdir,
                },
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ), patch.object(
                webhook_app, "_append_event"
            ):
                mock_mc = MagicMock()
                mock_mc.CONFIG = {"dry_run": True}
                mock_mc.plex.fetchItem.side_effect = Exception("not found")
                mock_mc.plex.search.side_effect = Exception("not found")
                mock_mc.match_episode_to_sonarr.return_value = {
                    "series": {"id": 10, "tags": []},
                    "episode": {"id": 88, "fileId": 880},
                    "file_id": 880,
                }
                mock_mc.get_sonarr_tags.return_value = []
                mock_mc.get_user_tags.return_value = []
                mock_mc.should_delete_media.return_value = True
                mock_mc.delete_sonarr_episode_file.return_value = True
                mock_mc.unmonitor_sonarr_episode.return_value = True

                with patch.object(webhook_app, "_get_media_cleanup", return_value=mock_mc):
                    res = lambda_main.lambda_handler(event, None)

            self.assertEqual(res, {"batchItemFailures": []})
            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            self.assertTrue(os.path.isfile(json_path))
            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["component"], "sqs")
            self.assertEqual(report["summary"]["would_delete"], 1)


if __name__ == "__main__":
    unittest.main()
