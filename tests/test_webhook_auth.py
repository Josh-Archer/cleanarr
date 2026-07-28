import os
import sys
import unittest
from unittest.mock import patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr.webhook.auth import (  # noqa: E402
    compute_hmac_sha256_hex,
    verify_from_http,
    verify_webhook_request,
)


class TestWebhookAuthHelpers(unittest.TestCase):
    def test_disabled_when_no_secrets(self):
        ok, reason = verify_webhook_request(secrets=(None, "", "  "))
        self.assertTrue(ok)
        self.assertEqual(reason, "disabled")

    def test_fail_closed_missing_credentials(self):
        ok, reason = verify_webhook_request(secrets=("s3cret",), token="", signature_header="")
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_credentials")

    def test_accept_shared_secret_token(self):
        ok, reason = verify_webhook_request(secrets=("current", "previous"), token="current")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok_token")

        ok_prev, reason_prev = verify_webhook_request(
            secrets=("current", "previous"), token="previous"
        )
        self.assertTrue(ok_prev)
        self.assertEqual(reason_prev, "ok_token")

    def test_reject_invalid_token(self):
        ok, reason = verify_webhook_request(secrets=("current",), token="wrong")
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_credentials")

    def test_accept_hmac_signature(self):
        secret = "signing-secret"
        body = b'{"event":"media.scrobble"}'
        digest = compute_hmac_sha256_hex(secret, body)
        ok, reason = verify_webhook_request(
            secrets=(secret,),
            signature_header=f"sha256={digest}",
            body=body,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok_signature")

    def test_accept_bare_hex_signature_header(self):
        secret = "signing-secret"
        body = b"payload"
        digest = compute_hmac_sha256_hex(secret, body)
        ok, reason = verify_from_http(
            secrets=(secret,),
            headers={"X-Hub-Signature-256": digest},
            body=body,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok_signature")

    def test_reject_invalid_signature(self):
        secret = "signing-secret"
        body = b'{"event":"media.scrobble"}'
        ok, reason = verify_webhook_request(
            secrets=(secret,),
            signature_header="sha256=deadbeef",
            body=body,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_credentials")

    def test_reject_signature_body_mismatch(self):
        secret = "signing-secret"
        body = b"original"
        digest = compute_hmac_sha256_hex(secret, body)
        ok, reason = verify_webhook_request(
            secrets=(secret,),
            signature_header=f"sha256={digest}",
            body=b"tampered",
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_credentials")

    def test_extract_token_and_signature_from_headers(self):
        ok, reason = verify_from_http(
            secrets=("abc",),
            headers={"X-Cleanarr-Webhook-Token": "abc"},
            body=b"{}",
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok_token")

        ok_query, reason_query = verify_from_http(
            secrets=("abc",),
            headers={},
            query_token="abc",
            body=b"{}",
        )
        self.assertTrue(ok_query)
        self.assertEqual(reason_query, "ok_token")


if __name__ == "__main__":
    unittest.main()
