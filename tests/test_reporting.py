import json
import os
import tempfile
import unittest

# Ensure we can import the local package
import sys
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr import reporting


class TestDecisionReporting(unittest.TestCase):

    def test_emit_writes_jsonl_record(self):
        with tempfile.NamedTemporaryFile(delete=False) as fp:
            report_file = fp.name

        try:
            reporter = reporting.DecisionReporter(component="cleanup", report_file=report_file)
            record = reporter.emit(
                reason_code="delete",
                media_type="movie",
                media_title="Inception",
                reason="webhook_finished",
                details={"source": "unit-test"},
            )

            with open(report_file, "r", encoding="utf-8") as handle:
                line = handle.read().strip()

            payload = json.loads(line)
            self.assertEqual(payload["component"], "cleanup")
            self.assertEqual(payload["reason_code"], "delete")
            self.assertEqual(payload["media_type"], "movie")
            self.assertEqual(payload["media_title"], "Inception")
            self.assertEqual(payload["reason"], "webhook_finished")
            self.assertEqual(payload["details"], {"source": "unit-test"})
            self.assertEqual(record, payload)
        finally:
            os.remove(report_file)

    def test_redact_sensitive_values_and_keys(self):
        os.environ['CLEANARR_WEBHOOK_SECRET'] = 'super-secret'

        with tempfile.NamedTemporaryFile(delete=False) as fp:
            report_file = fp.name

        try:
            reporter = reporting.DecisionReporter(
                component="webhook",
                report_file=report_file,
            )
            reporter.emit(
                reason_code="error",
                media_type="episode",
                media_title="Sample Episode",
                reason="test_redaction",
                details={
                    "api_key": "token-value",
                    "authorization": "bearer",
                    "token": "another",
                    "nested": {
                        "secret": "hidden",
                        "value": "super-secret",
                        "items": [
                            "token-value",
                        ],
                    },
                },
            )

            with open(report_file, "r", encoding="utf-8") as handle:
                payload = json.loads(handle.read().strip())

            details = payload["details"]
            self.assertEqual(details["api_key"], "[REDACTED]")
            self.assertEqual(details["authorization"], "[REDACTED]")
            self.assertEqual(details["token"], "[REDACTED]")
            self.assertEqual(details["nested"]["secret"], "[REDACTED]")
            self.assertEqual(details["nested"]["items"][0], "[REDACTED]")
            self.assertEqual(details["nested"].get("value"), "[REDACTED]")
        finally:
            os.remove(report_file)


if __name__ == '__main__':
    unittest.main()
