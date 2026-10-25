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

from cleanarr import cleanup, webhook_app
lambda_main = importlib.import_module("apps.lambda.main")


def _mock_plex_episode():
    """A Plex episode the webhook can match (issue #70: unmatched items never delete)."""
    item = MagicMock()
    item.type = "episode"
    item.title = "Winter Is Coming"
    item.grandparentTitle = "Game of Thrones"
    item.parentIndex = 1
    item.seasonNumber = 1
    item.index = 1
    item.ratingKey = 123
    item.locations = ["/media/got/s01e01.mkv"]
    item.guid = "plex://episode/123"
    return item


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
            "token": "secret-token-xyz",
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
                kids_movie = MagicMock()
                kids_movie.type = "movie"
                kids_movie.title = "Kids Movie"
                kids_movie.year = 2022
                kids_movie.ratingKey = 102
                kids_movie.locations = ["/media/movies/kids.mkv"]
                kids_movie.guid = "plex://movie/102"
                mock_mc.plex.fetchItem.side_effect = (
                    lambda key: kids_movie if int(key) == 102 else _mock_plex_episode()
                )
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
                mock_mc.plex.fetchItem.return_value = _mock_plex_episode()
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
                mock_mc.plex.fetchItem.return_value = _mock_plex_episode()
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


def _is_mock(module):
    if module is None:
        return True
    return type(module).__module__.startswith("unittest.mock")


def _borrow_real_module(name):
    saved = {}
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            module = sys.modules[key]
            if _is_mock(module):
                saved[key] = sys.modules.pop(key)
    return importlib.import_module(name), saved


_real_requests, _ = _borrow_real_module("requests")


class RecordingSession(_real_requests.Session):
    """Intercepts HTTP calls, records them, and mocks Servarr responses."""

    def __init__(self, endpoint_handlers=None):
        super().__init__()
        self.recorded_requests = []
        self.endpoint_handlers = endpoint_handlers or {}

    def request(self, method, url, **kwargs):
        method_upper = method.upper()
        url_str = str(url)
        self.recorded_requests.append({
            "method": method_upper,
            "url": url_str,
            "kwargs": kwargs,
        })
        endpoint = url_str
        if "/api/v3/" in url_str:
            endpoint = url_str.split("/api/v3/", 1)[1]
        elif "://" in url_str:
            endpoint = url_str.split("://", 1)[1].split("/", 1)[-1]

        for (pat_method, pat_end), res_data in self.endpoint_handlers.items():
            if method_upper == pat_method.upper():
                matched = False
                if callable(pat_end):
                    matched = pat_end(endpoint)
                elif isinstance(pat_end, str):
                    matched = (
                        endpoint == pat_end or endpoint.startswith(pat_end)
                    )
                if matched:
                    resp = _real_requests.Response()
                    resp.status_code = 200
                    if callable(res_data):
                        res_data = res_data()
                    if isinstance(res_data, (dict, list)):
                        resp._content = json.dumps(res_data).encode("utf-8")
                        resp.headers["Content-Type"] = "application/json"
                    else:
                        resp._content = str(res_data).encode("utf-8")
                    resp.url = url_str
                    return resp

        resp = _real_requests.Response()
        resp.status_code = 200
        resp._content = b"{}"
        resp.headers["Content-Type"] = "application/json"
        resp.url = url_str
        return resp

    @property
    def mutating_requests(self):
        """All mutating HTTP requests (DELETE, PUT, POST, PATCH)."""
        return [
            r for r in self.recorded_requests
            if r["method"] in ("DELETE", "PUT", "POST", "PATCH")
        ]

    @property
    def delete_requests(self):
        """All HTTP DELETE requests."""
        return [r for r in self.recorded_requests if r["method"] == "DELETE"]


def _default_arr_handlers():
    return {
        ("GET", "tag"): [],
        ("GET", "series"): [{"id": 1, "title": "Game of Thrones", "tags": []}],
        ("GET", "episode?seriesId=1"): [{
            "id": 10,
            "seriesId": 1,
            "seasonNumber": 1,
            "episodeNumber": 1,
            "episodeFileId": 100,
            "tags": [],
        }],
        ("GET", "movie"): [{
            "id": 2,
            "title": "Inception",
            "year": 2010,
            "tags": [],
            "movieFile": {"id": 200},
        }],
        ("GET", "episode/10"): {"id": 10, "monitored": True},
        ("PUT", "episode/10"): {"id": 10, "monitored": False},
        ("DELETE", "episodefile/100"): {},
    }


def _create_real_media_cleanup(rec_session, dry_run=True, plex_item=None):
    config_overrides = {
        "dry_run": dry_run,
        "media_source": "plex",
        "plex": {
            "baseurl": "http://mock-plex:32400",
            "token": "mock-plex-token",
        },
        "sonarr": {
            "baseurl": "http://mock-sonarr:8989/api/v3/",
            "apikey": "mock-sonarr-apikey",
        },
        "radarr": {
            "baseurl": "http://mock-radarr:7878/api/v3/",
            "apikey": "mock-radarr-apikey",
        },
        "disable_torrent_cleanup": True,
    }
    with patch.dict(cleanup.CONFIG, config_overrides):
        with patch("cleanarr.cleanup.PlexServer"):
            mc = cleanup.MediaCleanup()
            mc.sonarr_session = rec_session
            mc.radarr_session = rec_session
            mc.sonarr_client._session = rec_session
            mc.radarr_client._session = rec_session
            if plex_item is not None:
                mc.plex.fetchItem.return_value = plex_item
            else:
                mc.plex.fetchItem.side_effect = Exception(
                    "not found in mock plex"
                )
                mc.plex.search.side_effect = Exception(
                    "not found in mock plex"
                )
            if hasattr(mc.plex, "_session"):
                mc.plex._session = rec_session
            return mc


class TestRealDeleteMethodsDryRunHttpLayer(unittest.TestCase):
    def setUp(self):
        self.client = webhook_app.APP.test_client()
        webhook_app.reset_dry_run_report()

    def test_webhook_dry_run_real_delete_methods_no_mutations(self):
        rec_session = RecordingSession(_default_arr_handlers())
        mc = _create_real_media_cleanup(rec_session, dry_run=True, plex_item=_mock_plex_episode())
        mc.plex_source.watch_status = MagicMock(return_value={"alice": True})

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
                },
            ), patch.dict(
                cleanup.CONFIG,
                {
                    "dry_run": True,
                    "dry_run_report_dir": tmpdir,
                },
            ), patch.object(
                webhook_app, "_get_media_cleanup", return_value=mc
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ), patch.object(
                webhook_app, "_queue_enqueuing_enabled", return_value=False
            ), patch.object(
                webhook_app, "_start_background_threads"
            ), patch.object(
                webhook_app, "_append_event"
            ):
                response = self.client.post("/plex/webhook", json=payload)
                self.assertEqual(response.status_code, 200)
                time.sleep(0.1)

            # Assert at HTTP layer: no DELETE or mutating requests sent
            self.assertEqual(rec_session.delete_requests, [])
            self.assertEqual(rec_session.mutating_requests, [])
            self.assertTrue(len(rec_session.recorded_requests) > 0)
            self.assertTrue(
                all(
                    r["method"] == "GET" for r in rec_session.recorded_requests
                )
            )

            # Verify dry-run artifacts
            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            self.assertTrue(os.path.isfile(json_path))
            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["component"], "webhook")
            self.assertEqual(report["summary"]["would_delete"], 1)

    def test_webhook_direct_plex_item_dry_run_real_deletes(self):
        rec_session = RecordingSession(_default_arr_handlers())
        mock_item = MagicMock()
        mock_item.type = "episode"
        mock_item.title = "Winter Is Coming"
        mock_item.grandparentTitle = "Game of Thrones"
        mock_item.parentIndex = 1
        mock_item.seasonNumber = 1
        mock_item.index = 1
        mock_item.ratingKey = 123
        mock_item.locations = ["/media/got/s01e01.mkv"]
        mock_item.guid = "plex://episode/123"

        mc = _create_real_media_cleanup(
            rec_session, dry_run=True, plex_item=mock_item
        )
        mc.plex_source.watch_status = MagicMock(return_value={"alice": True})

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
                },
            ), patch.dict(
                cleanup.CONFIG,
                {
                    "dry_run": True,
                    "dry_run_report_dir": tmpdir,
                },
            ), patch.object(
                webhook_app, "_get_media_cleanup", return_value=mc
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ), patch.object(
                webhook_app, "_queue_enqueuing_enabled", return_value=False
            ), patch.object(
                webhook_app, "_start_background_threads"
            ), patch.object(
                webhook_app, "_append_event"
            ):
                response = self.client.post("/plex/webhook", json=payload)
                self.assertEqual(response.status_code, 200)
                time.sleep(0.1)

            self.assertEqual(rec_session.delete_requests, [])
            self.assertEqual(rec_session.mutating_requests, [])
            self.assertTrue(
                all(
                    r["method"] == "GET" for r in rec_session.recorded_requests
                )
            )

            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            self.assertTrue(os.path.isfile(json_path))
            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["summary"]["would_delete"], 1)

    def test_webhook_remove_dry_run_real_delete_no_mutations(self):
        rec_session = RecordingSession(_default_arr_handlers())
        mc = _create_real_media_cleanup(rec_session, dry_run=True)

        payload = {
            "event": "library.remove",
            "Metadata": {
                "type": "episode",
                "title": "Game of Thrones",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "CLEANARR_DRY_RUN": "true",
                    "CLEANARR_DRY_RUN_REPORT_DIR": tmpdir,
                },
            ), patch.dict(
                cleanup.CONFIG,
                {
                    "dry_run": True,
                    "dry_run_report_dir": tmpdir,
                },
            ), patch.object(
                webhook_app, "_get_media_cleanup", return_value=mc
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ), patch.object(
                webhook_app, "_queue_enqueuing_enabled", return_value=False
            ), patch.object(
                webhook_app, "_start_background_threads"
            ), patch.object(
                webhook_app, "_append_event"
            ):
                response = self.client.post("/plex/webhook", json=payload)
                self.assertEqual(response.status_code, 200)
                time.sleep(0.1)

            # Assert at HTTP layer: no DELETE or mutating requests sent
            self.assertEqual(rec_session.delete_requests, [])
            self.assertEqual(rec_session.mutating_requests, [])
            self.assertTrue(
                all(
                    r["method"] == "GET" for r in rec_session.recorded_requests
                )
            )

            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            self.assertTrue(os.path.isfile(json_path))
            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            # Removed episode events are deferred because they must never
            # delete an entire Sonarr series.
            self.assertEqual(report["summary"]["would_delete"], 0)

    def test_sqs_queue_dry_run_real_delete_no_mutations(self):
        rec_session = RecordingSession(_default_arr_handlers())
        mc = _create_real_media_cleanup(rec_session, dry_run=True, plex_item=_mock_plex_episode())
        mc.plex_source.watch_status = MagicMock(return_value={"alice": True})

        messages = [
            {
                "MessageId": "sqs-msg-movie-1",
                "ReceiptHandle": "rcpt-movie-1",
                "Body": json.dumps({
                    "event": "media.scrobble",
                    "metadata": {
                        "type": "movie",
                        "ratingKey": "500",
                        "title": "Inception",
                        "year": 2010,
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
            ), patch.dict(
                cleanup.CONFIG,
                {
                    "dry_run": True,
                    "dry_run_report_dir": tmpdir,
                },
            ), patch.object(
                webhook_app, "WEBHOOK_QUEUE_MODE", "sqs"
            ), patch.object(
                webhook_app, "WEBHOOK_QUEUE_POLLING", True
            ), patch.object(
                webhook_app,
                "WEBHOOK_QUEUE_URL",
                "https://sqs.us-east-1.amazonaws.com/123/queue",
            ), patch.object(
                webhook_app, "_get_sqs_client", return_value=fake_client
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ), patch.object(
                webhook_app, "_get_media_cleanup", return_value=mc
            ), patch.object(
                webhook_app, "_append_event"
            ):
                summary = webhook_app.process_sqs_queue_messages(
                    max_messages=1
                )

            self.assertEqual(summary["received"], 1)
            self.assertEqual(summary["processed"], 1)
            self.assertEqual(summary["deleted"], 1)

            # Assert at HTTP layer: no DELETE or mutating requests sent
            self.assertEqual(rec_session.delete_requests, [])
            self.assertEqual(rec_session.mutating_requests, [])
            self.assertTrue(
                all(
                    r["method"] == "GET" for r in rec_session.recorded_requests
                )
            )

            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            self.assertTrue(os.path.isfile(json_path))
            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["component"], "sqs")
            self.assertEqual(report["summary"]["would_delete"], 1)

    def test_sqs_lambda_records_dry_run_real_delete_no_mutations(self):
        rec_session = RecordingSession(_default_arr_handlers())
        mc = _create_real_media_cleanup(rec_session, dry_run=True, plex_item=_mock_plex_episode())
        mc.plex_source.watch_status = MagicMock(return_value={"alice": True})

        records = [
            {
                "messageId": "lambda-msg-ep-1",
                "body": json.dumps({
                    "event": "media.scrobble",
                    "metadata": {
                        "type": "episode",
                        "ratingKey": "123",
                        "title": "Winter Is Coming",
                        "parentTitle": "Game of Thrones",
                        "parentIndex": 1,
                        "index": 1,
                    },
                    "account": {"title": "david"},
                }),
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "CLEANARR_DRY_RUN": "true",
                    "CLEANARR_DRY_RUN_REPORT_DIR": tmpdir,
                },
            ), patch.dict(
                cleanup.CONFIG,
                {
                    "dry_run": True,
                    "dry_run_report_dir": tmpdir,
                },
            ), patch.object(
                webhook_app, "ENABLE_WEBHOOK_DELETIONS", True
            ), patch.object(
                webhook_app, "_get_media_cleanup", return_value=mc
            ), patch.object(
                webhook_app, "_append_event"
            ):
                summary = webhook_app.process_sqs_event_records(records)

            self.assertEqual(summary["received"], 1)
            self.assertEqual(summary["processed"], 1)
            self.assertEqual(summary["failed"], 0)

            # Assert at HTTP layer: no DELETE or mutating requests sent
            self.assertEqual(rec_session.delete_requests, [])
            self.assertEqual(rec_session.mutating_requests, [])
            self.assertTrue(
                all(
                    r["method"] == "GET" for r in rec_session.recorded_requests
                )
            )

            json_path = os.path.join(tmpdir, "cleanarr-dry-run-report.json")
            self.assertTrue(os.path.isfile(json_path))
            with open(json_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["component"], "sqs")
            self.assertEqual(report["summary"]["would_delete"], 1)

    def test_live_mode_real_delete_methods_send_mutating_http_requests(self):
        rec_session = RecordingSession(_default_arr_handlers())
        mc = _create_real_media_cleanup(rec_session, dry_run=False)

        with patch.dict(cleanup.CONFIG, {"dry_run": False}):
            del_result = mc.delete_sonarr_episode_file(100)
            self.assertTrue(del_result)

            unmon_result = mc.unmonitor_sonarr_episode(10)
            self.assertTrue(unmon_result)

            # Verify that in live mode, DELETE and PUT requests ARE sent
            self.assertEqual(len(rec_session.delete_requests), 1)
            self.assertEqual(
                rec_session.delete_requests[0]["method"], "DELETE"
            )
            self.assertIn(
                "episodefile/100", rec_session.delete_requests[0]["url"]
            )

            put_requests = [
                r for r in rec_session.recorded_requests
                if r["method"] == "PUT"
            ]
            self.assertEqual(len(put_requests), 1)
            self.assertIn("episode/10", put_requests[0]["url"])


if __name__ == "__main__":
    unittest.main()
