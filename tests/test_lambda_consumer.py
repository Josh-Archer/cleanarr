import json
import os
import sys
import unittest
import importlib
from unittest.mock import patch


repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

lambda_main = importlib.import_module("apps.lambda.main")
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

        with patch.object(lambda_main, "process_sqs_event_records", return_value=summary) as process_records, \
             patch.object(lambda_main, "process_sqs_queue_messages") as process_queue:
            response = lambda_main.lambda_handler(
                {"Records": [{"messageId": "msg-1"}, {"messageId": "msg-2"}]},
                None,
            )

        process_records.assert_called_once()
        process_queue.assert_not_called()
        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "msg-2"}]},
        )

    def test_lambda_handler_returns_success_summary_for_polled_queue(self):
        summary = {
            "enabled": True,
            "queue_mode": "sqs",
            "received": 1,
            "processed": 1,
            "deleted": 1,
            "failed": 0,
            "reason": "",
        }

        with patch.object(lambda_main, "process_sqs_queue_messages", return_value=summary) as process_queue, \
             patch.object(lambda_main, "process_sqs_event_records") as process_records:
            response = lambda_main.lambda_handler({}, None)

        process_records.assert_not_called()
        process_queue.assert_called_once()
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["message"], "Queued webhook processing complete")
        self.assertEqual(body["queue"], summary)

    def test_lambda_handler_returns_500_for_polled_queue_failures(self):
        summary = {
            "enabled": True,
            "queue_mode": "sqs",
            "received": 2,
            "processed": 1,
            "deleted": 1,
            "failed": 1,
            "reason": "",
        }

        with patch.object(lambda_main, "process_sqs_queue_messages", return_value=summary):
            response = lambda_main.lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 500)
        body = json.loads(response["body"])
        self.assertEqual(body["message"], "Queued webhook processing failed")
        self.assertEqual(body["queue"], summary)


if __name__ == "__main__":
    unittest.main()
