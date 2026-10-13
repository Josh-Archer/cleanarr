import importlib
import logging
import os
import sys
import unittest
from unittest.mock import Mock, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from cleanarr import webhook_app


def _is_mock(module):
    return module is None or type(module).__module__.startswith("unittest.mock")


def _borrow_real_module(name):
    """Import the real module even if an earlier test swapped it for a MagicMock.

    test_cleanup_feature replaces sys.modules entries for loguru and requests at
    import time. Later tests then bind those mocks. Put the mocks back afterwards
    so the rest of the suite keeps the same import state.
    """
    saved = {}
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            module = sys.modules[key]
            if _is_mock(module):
                saved[key] = sys.modules.pop(key)
    return importlib.import_module(name), saved


def _restore_modules(name, saved):
    if not saved:
        return
    for key in list(sys.modules):
        if (key == name or key.startswith(name + ".")) and key not in saved:
            sys.modules.pop(key, None)
    sys.modules.update(saved)


def _log_record():
    return logging.LogRecord(
        "cleanarr.webhook_app",
        logging.INFO,
        __file__,
        1,
        "ping",
        (),
        None,
    )


def _prepared_handler(session_class):
    import logging_loki

    handler = logging_loki.LokiHandler(
        url="https://loki.example.invalid/loki/api/v1/push",
        tags={"application": "cleanarr-webhook"},
        version="1",
    )
    # Install a real Session before preparation. The emitter may have captured
    # a mocked requests.Session when logging_loki was first imported.
    handler.emitter.session_class = session_class
    return webhook_app._prepare_loki_handler(handler, "unit-cf-id", "unit-cf-secret")


class WebhookLogFormattingTests(unittest.TestCase):
    def test_webhook_received_renders_event_and_finished_flag(self):
        loguru, saved = _borrow_real_module("loguru")
        messages = []
        sink_id = loguru.logger.add(lambda message: messages.append(message.record["message"]))
        event = {
            "event": "media.scrobble",
            "action": "scrobble",
            "metadata": {"type": "movie", "title": "unit-item"},
        }
        try:
            with patch.object(webhook_app, "logger", loguru.logger), \
                 patch.object(webhook_app, "_append_event"), \
                 patch.object(webhook_app, "_record_webhook_decision"), \
                 patch.object(webhook_app, "_background_process_finished"), \
                 patch.object(webhook_app, "ENABLE_WEBHOOK_DELETIONS", False):
                webhook_app._process_webhook_event_actions(
                    event,
                    async_mode=False,
                    force_deletions=False,
                )
        finally:
            loguru.logger.remove(sink_id)
            _restore_modules("loguru", saved)

        received = [line for line in messages if line.startswith("Webhook received:")]
        self.assertEqual(len(received), 1)
        self.assertIn("event='media.scrobble'", received[0])
        self.assertIn("is_finished=True", received[0])
        self.assertNotIn("%s", received[0])


class LokiHandlerTests(unittest.TestCase):
    def setUp(self):
        self.requests, self._saved_requests = _borrow_real_module("requests")

    def tearDown(self):
        _restore_modules("requests", self._saved_requests)

    def test_cloudflare_headers_are_on_the_session_that_posts(self):
        handler = _prepared_handler(self.requests.Session)
        posted_headers = []

        class _RecordingSession(handler.emitter.session_class):
            def post(self, url, json=None, **kwargs):
                posted_headers.append(self.headers.get("CF-Access-Client-Id"))
                posted_headers.append(self.headers.get("CF-Access-Client-Secret"))
                return Mock(status_code=204)

        handler.emitter.session_class = _RecordingSession
        handler.emitter.close()
        handler.emit(_log_record())
        handler.emitter.close()
        handler.emit(_log_record())

        self.assertEqual(
            posted_headers,
            ["unit-cf-id", "unit-cf-secret", "unit-cf-id", "unit-cf-secret"],
        )

    def test_loki_error_status_does_not_raise_or_traceback(self):
        import logging_loki

        loguru, saved_loguru = _borrow_real_module("loguru")
        try:
            for status in (403, 502):
                with self.subTest(status=status):
                    handler = _prepared_handler(self.requests.Session)
                    warnings = []
                    sink_id = loguru.logger.add(
                        lambda message: warnings.append(message.record["message"])
                    )

                    class _StatusSession(handler.emitter.session_class):
                        def post(self, url, json=None, **kwargs):
                            return Mock(status_code=status)

                    handler.emitter.session_class = _StatusSession
                    handler.emitter.close()
                    try:
                        with patch.object(webhook_app, "logger", loguru.logger), \
                             patch.object(logging_loki.LokiHandler, "handleError") as handle_error:
                            handler.emit(_log_record())
                        handle_error.assert_not_called()
                    finally:
                        loguru.logger.remove(sink_id)

                    self.assertIn(f"Loki push failed with status {status}", warnings)
        finally:
            _restore_modules("loguru", saved_loguru)


if __name__ == "__main__":
    unittest.main()
