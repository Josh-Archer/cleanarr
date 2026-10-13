import logging
import os
import sys
import unittest
from unittest.mock import Mock, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from loguru import logger
import logging_loki

from cleanarr import webhook_app


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


def _prepared_handler():
    handler = logging_loki.LokiHandler(
        url="https://loki.example.invalid/loki/api/v1/push",
        tags={"application": "cleanarr-webhook"},
        version="1",
    )
    return webhook_app._prepare_loki_handler(handler, "unit-cf-id", "unit-cf-secret")


class WebhookLogFormattingTests(unittest.TestCase):
    def test_webhook_received_renders_event_and_finished_flag(self):
        messages = []
        sink_id = logger.add(lambda message: messages.append(message.record["message"]))
        event = {
            "event": "media.scrobble",
            "action": "scrobble",
            "metadata": {"type": "movie", "title": "unit-item"},
        }
        try:
            with patch.object(webhook_app, "_append_event"), \
                 patch.object(webhook_app, "_record_webhook_decision"), \
                 patch.object(webhook_app, "_background_process_finished"), \
                 patch.object(webhook_app, "ENABLE_WEBHOOK_DELETIONS", False):
                webhook_app._process_webhook_event_actions(
                    event,
                    async_mode=False,
                    force_deletions=False,
                )
        finally:
            logger.remove(sink_id)

        received = [line for line in messages if line.startswith("Webhook received:")]
        self.assertEqual(len(received), 1)
        self.assertIn("event='media.scrobble'", received[0])
        self.assertIn("is_finished=True", received[0])
        self.assertNotIn("%s", received[0])


class LokiHandlerTests(unittest.TestCase):
    def test_cloudflare_headers_are_on_the_session_that_posts(self):
        handler = _prepared_handler()
        posted_headers = []

        def _post(session, url, json=None, **kwargs):
            posted_headers.append(session.headers.get("CF-Access-Client-Id"))
            posted_headers.append(session.headers.get("CF-Access-Client-Secret"))
            return Mock(status_code=204)

        handler.emitter.session_class.post = _post
        handler.emitter.close()
        handler.emit(_log_record())
        handler.emitter.close()
        handler.emit(_log_record())

        self.assertEqual(
            posted_headers,
            ["unit-cf-id", "unit-cf-secret", "unit-cf-id", "unit-cf-secret"],
        )

    def test_loki_error_status_does_not_raise_or_traceback(self):
        for status in (403, 502):
            with self.subTest(status=status):
                handler = _prepared_handler()
                warnings = []
                sink_id = logger.add(lambda message: warnings.append(message.record["message"]))

                def _post(session, url, json=None, **kwargs):
                    return Mock(status_code=status)

                handler.emitter.session_class.post = _post
                handler.emitter.close()
                try:
                    with patch.object(logging_loki.LokiHandler, "handleError") as handle_error:
                        handler.emit(_log_record())
                    handle_error.assert_not_called()
                finally:
                    logger.remove(sink_id)

                self.assertIn(f"Loki push failed with status {status}", warnings)


if __name__ == "__main__":
    unittest.main()
