import json
import os
import sys
import unittest
import importlib
from unittest.mock import MagicMock, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

lambda_main = importlib.import_module("apps.lambda.main")
job_lambda = importlib.import_module("apps.job.lambda_handler")
job_main = importlib.import_module("apps.job.main")
cleanup = importlib.import_module("cleanarr.cleanup")
webhook_app = importlib.import_module("cleanarr.webhook_app")


class TestLambdaConsumer(unittest.TestCase):
    def test_lambda_handler_processes_direct_http_webhook_events(self):
        payload = {
            "event": "media.scrobble",
            "Metadata": {"guid": "plex://movie/123", "ratingKey": "123"},
            "Account": {"id": 1, "title": "alice"},
        }
        event = {
            "version": "2.0",
            "routeKey": "POST /plex/webhook",
            "rawPath": "/plex/webhook",
            "rawQueryString": "",
            "headers": {"content-type": "application/json"},
            "requestContext": {"http": {"method": "POST", "sourceIp": "127.0.0.1"}},
            "body": json.dumps(payload),
            "isBase64Encoded": False,
        }

        with patch.object(webhook_app, "WEBHOOK_SECRET", None), \
             patch.object(webhook_app, "_start_background_threads"), \
             patch.object(webhook_app, "_queue_enqueuing_enabled", return_value=False), \
             patch.object(webhook_app, "_process_webhook_event_actions", return_value={"recorded": True}) as process_actions:
            response = lambda_main.lambda_handler(event, None)

        process_actions.assert_called_once()
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertFalse(body["queued"])
        self.assertTrue(body["recorded"])

    def test_lambda_handler_returns_partial_batch_failures_for_sqs_records(self):
        summary = {
            "enabled": True,
            "queue_mode": "sqs",
            "received": 2,
            "processed": 1,
            "deleted": 0,
            "failed": 1,
            "failed_message_ids": ["msg-2"],
            "reason": "",
        }

        with patch.object(lambda_main, "process_sqs_event_records", return_value=summary) as process_records:
            response = lambda_main.lambda_handler(
                {"Records": [{"messageId": "msg-1"}, {"messageId": "msg-2"}]},
                None,
            )

        process_records.assert_called_once()
        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "msg-2"}]},
        )

    def test_lambda_handler_does_not_poll_queue_without_records(self):
        with patch.object(webhook_app, "process_sqs_queue_messages") as process_queue, \
             patch.object(webhook_app, "process_sqs_event_records") as process_records:
            response = lambda_main.lambda_handler({}, None)

        process_queue.assert_not_called()
        process_records.assert_not_called()
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            json.loads(response["body"]).get("message"),
            "No webhook queue event payload",
        )


class TestScheduledRuntimeBoundary(unittest.TestCase):
    def test_job_main_does_not_poll_webhook_queue(self):
        cleaner = MagicMock()
        cleaner.run = MagicMock()
        with patch.object(job_main, "MediaCleanup", return_value=cleaner) as run_cleanup, \
             patch.object(webhook_app, "process_sqs_queue_messages") as process_queue:
            job_main.main()

        run_cleanup.assert_called_once()
        process_queue.assert_not_called()
        cleaner.run.assert_called_once()

    def test_job_lambda_handler_ignores_sqs_records_and_runs_cleanup(self):
        cleaner = MagicMock()
        cleaner.run = MagicMock()
        with patch.object(cleanup, "MediaCleanup", return_value=cleaner) as cleanup_cls, \
             patch.object(webhook_app, "process_sqs_event_records") as process_records, \
             patch.object(webhook_app, "process_sqs_queue_messages") as process_queue:
            response = job_lambda.lambda_handler({"Records": [{"messageId": "msg-1"}]}, None)

        cleanup_cls.assert_called_once()
        cleaner.run.assert_called_once()
        process_records.assert_not_called()
        process_queue.assert_not_called()
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["body"], "Cleanup executed successfully.")


if __name__ == "__main__":
    unittest.main()
