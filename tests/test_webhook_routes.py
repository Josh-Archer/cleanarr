import os
import sys
import unittest
from unittest.mock import patch

# Ensure we can import the local package
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr import webhook_app  # noqa: E402


class TestWebhookRouteSecretGate(unittest.TestCase):

    def setUp(self):
        self.client = webhook_app.APP.test_client()

    def _payload(self):
        return {
            "NotificationType": "ItemMarkPlayed",
            "ItemType": "Movie",
            "NotificationUsername": "alice",
            "ItemName": "Example Movie",
        }

    def test_jellyfin_webhook_rejects_invalid_token(self):
        with patch.object(
            webhook_app, "JELLYFIN_WEBHOOK_SECRET", "current-secret"
        ), patch.object(
            webhook_app, "JELLYFIN_WEBHOOK_SECRET_PREVIOUS", None
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
        ) as process_actions:
            response = self.client.post(
                "/jellyfin/webhook",
                headers={"X-Webhook-Token": "wrong-secret"},
                json=self._payload(),
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {"status": "error", "message": "Unauthorized"},
        )
        process_actions.assert_not_called()

    def test_jellyfin_webhook_accepts_current_and_previous_secrets(self):
        with patch.object(
            webhook_app, "JELLYFIN_WEBHOOK_SECRET", "current-secret"
        ), patch.object(
            webhook_app, "JELLYFIN_WEBHOOK_SECRET_PREVIOUS", "previous-secret"
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
                "/jellyfin/webhook",
                headers={"X-Webhook-Token": "current-secret"},
                json=self._payload(),
            )
            response_previous = self.client.post(
                "/jellyfin/webhook",
                headers={"X-Webhook-Token": "previous-secret"},
                json=self._payload(),
            )

        self.assertEqual(response_current.status_code, 200)
        self.assertEqual(response_current.get_json().get("status"), "ok")
        self.assertEqual(response_previous.status_code, 200)
        self.assertEqual(response_previous.get_json().get("status"), "ok")
        self.assertEqual(process_actions.call_count, 2)
