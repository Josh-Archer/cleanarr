import unittest
import os
import io
import json
from unittest.mock import patch, MagicMock
from http.server import HTTPServer

import apps.webhook.main as main_module
import cleanarr.webhook.proxy as proxy_module

class TestProxy(unittest.TestCase):
    def setUp(self):
        # Reset environment before each test
        if "CLEANARR_WEBHOOK_FORWARD_URL" in os.environ:
            del os.environ["CLEANARR_WEBHOOK_FORWARD_URL"]
        if "CLEANARR_WEBHOOK_QUEUE_URL" in os.environ:
            del os.environ["CLEANARR_WEBHOOK_QUEUE_URL"]
        if "CLEANARR_WEBHOOK_QUEUE_REGION" in os.environ:
            del os.environ["CLEANARR_WEBHOOK_QUEUE_REGION"]

    @patch("apps.webhook.main.run_proxy")
    @patch("apps.webhook.main._start_background_threads")
    @patch("apps.webhook.main.APP")
    def test_proxy_mode_enabled(self, mock_app, mock_threads, mock_run_proxy):
        with patch.dict(os.environ, {"CLEANARR_WEBHOOK_FORWARD_URL": "http://lambda"}):
            main_module.main()
            mock_run_proxy.assert_called_once()
            mock_app.run.assert_not_called()
            mock_threads.assert_not_called()

    @patch("apps.webhook.main.run_proxy")
    @patch("apps.webhook.main._start_background_threads")
    @patch("apps.webhook.main.APP")
    def test_proxy_mode_enabled_when_queue_url_is_set(self, mock_app, mock_threads, mock_run_proxy):
        with patch.dict(os.environ, {"CLEANARR_WEBHOOK_QUEUE_URL": "https://sqs.example/queue"}):
            main_module.main()
            mock_run_proxy.assert_called_once()
            mock_app.run.assert_not_called()
            mock_threads.assert_not_called()

    @patch("apps.webhook.main.run_proxy")
    @patch("apps.webhook.main._start_background_threads")
    @patch("apps.webhook.main.APP")
    def test_direct_mode_enabled(self, mock_app, mock_threads, mock_run_proxy):
        with patch.dict(os.environ, {"CLEANARR_WEBHOOK_FORWARD_URL": "", "CLEANARR_WEBHOOK_QUEUE_URL": ""}):
            main_module.main()
            mock_run_proxy.assert_not_called()
            mock_app.run.assert_called_once()
            mock_threads.assert_called_once()

class TestProxyHandler(unittest.TestCase):
    def setUp(self):
        # We need to mock the socket and server to avoid actual network binding
        self.mock_request = MagicMock()
        self.mock_server = MagicMock()
        self.mock_server.server_address = ("0.0.0.0", 8000)

    @patch("cleanarr.webhook.proxy.urlopen")
    @patch("cleanarr.webhook.proxy.sign_headers")
    def test_do_POST_success(self, mock_sign, mock_urlopen):
        # Mock dependencies
        mock_sign.return_value = {"Authorization": "AWS4...", "Content-Type": "application/json"}
        
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"status":"ok"}'
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Instantiate handler manually
        # Note: BaseHTTPRequestHandler.__init__ calls handle() which we want to avoid
        with patch.object(proxy_module.ProxyHandler, 'setup'), \
             patch.object(proxy_module.ProxyHandler, 'handle'), \
             patch.object(proxy_module.ProxyHandler, 'finish'):
            handler = proxy_module.ProxyHandler(self.mock_request, ("127.0.0.1", 12345), self.mock_server)
        
        handler.path = "/plex/webhook"
        handler.headers = {"Content-Length": "2", "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(b"{}")
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        with patch.dict(os.environ, {"CLEANARR_WEBHOOK_FORWARD_URL": "http://lambda"}):
            handler.do_POST()

        self.assertEqual(handler.wfile.getvalue(), b'{"status":"ok"}')
        handler.send_response.assert_called_with(200)

    @patch("cleanarr.webhook.proxy._forward_webhook_request")
    @patch("cleanarr.webhook.proxy._publish_webhook_event_to_sqs")
    @patch("cleanarr.webhook.proxy._parse_webhook_event")
    def test_do_POST_publishes_to_sqs_when_queue_configured(self, mock_parse, mock_publish, mock_forward):
        mock_parse.return_value = {"event": "media.scrobble", "recorded": True}
        mock_publish.return_value = True

        with patch.object(proxy_module.ProxyHandler, "setup"), \
             patch.object(proxy_module.ProxyHandler, "handle"), \
             patch.object(proxy_module.ProxyHandler, "finish"):
            handler = proxy_module.ProxyHandler(self.mock_request, ("127.0.0.1", 12345), self.mock_server)

        handler.path = "/plex/webhook"
        handler.command = "POST"
        handler.headers = {"Content-Length": "2", "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(b"{}")
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.client_address = ("127.0.0.1", 12345)

        with patch.dict(
            os.environ,
            {
                "CLEANARR_WEBHOOK_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/031332257715/cleanarr-webhook-events",
                "CLEANARR_WEBHOOK_QUEUE_REGION": "us-east-1",
            },
        ):
            handler.do_POST()

        payload = json.loads(handler.wfile.getvalue().decode("utf-8").strip())
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["queued"])
        self.assertEqual(payload["sink"], "sqs")
        handler.send_response.assert_called_with(200)
        mock_publish.assert_called_once()
        mock_forward.assert_not_called()

    @patch("cleanarr.webhook.proxy._forward_webhook_request")
    @patch("cleanarr.webhook.proxy._publish_webhook_event_to_sqs")
    @patch("cleanarr.webhook.proxy._parse_webhook_event")
    def test_do_POST_falls_back_to_lambda_when_sqs_enqueue_fails(self, mock_parse, mock_publish, mock_forward):
        mock_parse.return_value = {"event": "media.scrobble", "recorded": True}
        mock_publish.return_value = False
        mock_forward.return_value = {
            "status": 200,
            "content_type": "application/json",
            "payload": b'{"status":"ok"}',
        }

        with patch.object(proxy_module.ProxyHandler, "setup"), \
             patch.object(proxy_module.ProxyHandler, "handle"), \
             patch.object(proxy_module.ProxyHandler, "finish"):
            handler = proxy_module.ProxyHandler(self.mock_request, ("127.0.0.1", 12345), self.mock_server)

        handler.path = "/plex/webhook"
        handler.command = "POST"
        handler.headers = {"Content-Length": "2", "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(b"{}")
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.client_address = ("127.0.0.1", 12345)

        with patch.dict(
            os.environ,
            {
                "CLEANARR_WEBHOOK_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/031332257715/cleanarr-webhook-events",
                "CLEANARR_WEBHOOK_QUEUE_REGION": "us-east-1",
                "CLEANARR_WEBHOOK_FORWARD_URL": "http://lambda",
            },
        ):
            handler.do_POST()

        self.assertEqual(handler.wfile.getvalue(), b'{"status":"ok"}')
        handler.send_response.assert_called_with(200)
        mock_publish.assert_called_once()
        mock_forward.assert_called_once()

    def test_do_GET_healthz(self):
        with patch.object(proxy_module.ProxyHandler, 'setup'), \
             patch.object(proxy_module.ProxyHandler, 'handle'), \
             patch.object(proxy_module.ProxyHandler, 'finish'):
            handler = proxy_module.ProxyHandler(self.mock_request, ("127.0.0.1", 12345), self.mock_server)
        
        handler.path = "/healthz"
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()

        self.assertIn(b'{"ok":true}', handler.wfile.getvalue())
        handler.send_response.assert_called_with(200)

    def test_do_GET_404(self):
        with patch.object(proxy_module.ProxyHandler, 'setup'), \
             patch.object(proxy_module.ProxyHandler, 'handle'), \
             patch.object(proxy_module.ProxyHandler, 'finish'):
            handler = proxy_module.ProxyHandler(self.mock_request, ("127.0.0.1", 12345), self.mock_server)
        
        handler.path = "/unknown"
        handler.send_response = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()

        handler.send_response.assert_called_with(404)

if __name__ == "__main__":
    unittest.main()
