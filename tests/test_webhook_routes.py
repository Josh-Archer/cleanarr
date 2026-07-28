import os
import sys
import unittest
from unittest.mock import patch

# Ensure we can import the local package
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr import webhook_app  # noqa: E402
from cleanarr.webhook.auth import compute_hmac_sha256_hex  # noqa: E402


class TestWebhookRouteSecretGate(unittest.TestCase):

    def setUp(self):
        self.client = webhook_app.APP.test_client()

    def _jellyfin_payload(self):
        return {
            "NotificationType": "ItemMarkPlayed",
            "ItemType": "Movie",
            "NotificationUsername": "alice",
            "ItemName": "Example Movie",
        }

    def _plex_payload(self):
        return {
            "event": "media.scrobble",
            "Account": {"id": 1, "title": "alice"},
            "Metadata": {
                "ratingKey": "123",
                "title": "Example Movie",
                "type": "movie",
            },
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
                json=self._jellyfin_payload(),
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {"status": "error", "message": "Unauthorized"},
        )
        process_actions.assert_not_called()

    def test_jellyfin_webhook_rejects_missing_token_when_enabled(self):
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
                json=self._jellyfin_payload(),
            )

        self.assertEqual(response.status_code, 401)
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
                json=self._jellyfin_payload(),
            )
            response_previous = self.client.post(
                "/jellyfin/webhook",
                headers={"X-Webhook-Token": "previous-secret"},
                json=self._jellyfin_payload(),
            )

        self.assertEqual(response_current.status_code, 200)
        self.assertEqual(response_current.get_json().get("status"), "ok")
        self.assertEqual(response_previous.status_code, 200)
        self.assertEqual(response_previous.get_json().get("status"), "ok")
        self.assertEqual(process_actions.call_count, 2)

    def test_jellyfin_webhook_accepts_hmac_signature(self):
        secret = "sig-secret"
        body = b'{"NotificationType":"ItemMarkPlayed","ItemType":"Movie","NotificationUsername":"alice","ItemName":"Example Movie"}'
        digest = compute_hmac_sha256_hex(secret, body)

        with patch.object(
            webhook_app, "JELLYFIN_WEBHOOK_SECRET", secret
        ), patch.object(
            webhook_app, "JELLYFIN_WEBHOOK_SECRET_PREVIOUS", None
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app, "_queue_enqueuing_enabled", return_value=False
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
            return_value={"recorded": True},
        ) as process_actions:
            response = self.client.post(
                "/jellyfin/webhook",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Cleanarr-Signature": f"sha256={digest}",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().get("status"), "ok")
        process_actions.assert_called_once()

    def test_plex_webhook_rejects_invalid_token(self):
        with patch.object(
            webhook_app, "WEBHOOK_SECRET", "plex-secret"
        ), patch.object(
            webhook_app, "WEBHOOK_SECRET_PREVIOUS", None
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
        ) as process_actions:
            response = self.client.post(
                "/plex/webhook",
                headers={"X-Cleanarr-Webhook-Token": "wrong"},
                json=self._plex_payload(),
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {"status": "error", "message": "Unauthorized"},
        )
        process_actions.assert_not_called()

    def test_plex_webhook_rejects_missing_token_when_enabled(self):
        with patch.object(
            webhook_app, "WEBHOOK_SECRET", "plex-secret"
        ), patch.object(
            webhook_app, "WEBHOOK_SECRET_PREVIOUS", None
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
        ) as process_actions:
            response = self.client.post(
                "/plex/webhook",
                json=self._plex_payload(),
            )

        self.assertEqual(response.status_code, 401)
        process_actions.assert_not_called()

    def test_plex_webhook_accepts_valid_token(self):
        with patch.object(
            webhook_app, "WEBHOOK_SECRET", "plex-secret"
        ), patch.object(
            webhook_app, "WEBHOOK_SECRET_PREVIOUS", "old-plex-secret"
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app, "_queue_enqueuing_enabled", return_value=False
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
            return_value={"recorded": True},
        ) as process_actions:
            response = self.client.post(
                "/plex/webhook",
                headers={"X-Cleanarr-Webhook-Token": "plex-secret"},
                json=self._plex_payload(),
            )
            response_prev = self.client.post(
                "/plex/webhook",
                headers={"X-Webhook-Token": "old-plex-secret"},
                json=self._plex_payload(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().get("status"), "ok")
        self.assertEqual(response_prev.status_code, 200)
        self.assertEqual(process_actions.call_count, 2)

    def test_plex_webhook_accepts_hmac_signature(self):
        secret = "plex-sig-secret"
        body = b'{"event":"media.scrobble","Account":{"title":"alice"},"Metadata":{"title":"Example","type":"movie"}}'
        digest = compute_hmac_sha256_hex(secret, body)

        with patch.object(
            webhook_app, "WEBHOOK_SECRET", secret
        ), patch.object(
            webhook_app, "WEBHOOK_SECRET_PREVIOUS", None
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app, "_queue_enqueuing_enabled", return_value=False
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
            return_value={"recorded": True},
        ) as process_actions:
            response = self.client.post(
                "/plex/webhook",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": f"sha256={digest}",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().get("status"), "ok")
        process_actions.assert_called_once()

    def test_plex_webhook_open_when_secret_unset(self):
        with patch.object(
            webhook_app, "WEBHOOK_SECRET", None
        ), patch.object(
            webhook_app, "WEBHOOK_SECRET_PREVIOUS", None
        ), patch.object(
            webhook_app, "_start_background_threads"
        ), patch.object(
            webhook_app, "_queue_enqueuing_enabled", return_value=False
        ), patch.object(
            webhook_app,
            "_process_webhook_event_actions",
            return_value={"recorded": True},
        ) as process_actions:
            response = self.client.post(
                "/plex/webhook",
                json=self._plex_payload(),
            )

        self.assertEqual(response.status_code, 200)
        process_actions.assert_called_once()
