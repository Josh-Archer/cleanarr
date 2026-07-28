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


class TestDryRunReportShape(unittest.TestCase):
    """Acceptance tests for per-user dry-run report artifacts (#18)."""

    def _sample_decisions(self):
        return [
            {
                "recorded_at": "2026-07-27T00:00:00+00:00",
                "component": "cleanup",
                "reason_code": "dry-run",
                "media_type": "episode",
                "media_title": "Show S01E01",
                "reason": "standard watched",
                "details": {
                    "watched_by": {"alice": True, "bob": True},
                    "user_tags": ["alice", "bob"],
                },
            },
            {
                "recorded_at": "2026-07-27T00:00:01+00:00",
                "component": "cleanup",
                "reason_code": "protected",
                "media_type": "movie",
                "media_title": "Kids Movie (2020)",
                "reason": "protected_series_or_movie_tags",
                "details": {
                    "protected_labels": ["kids"],
                    "watched_by": {"alice": True},
                },
            },
            {
                "recorded_at": "2026-07-27T00:00:02+00:00",
                "component": "cleanup",
                "reason_code": "protected",
                "media_type": "episode",
                "media_title": "Archive Show S02E03",
                "reason": "protected_series_or_episode_tags",
                "details": {
                    "protected_labels": ["safe"],
                    "watched_by": {"bob": True},
                },
            },
            {
                "recorded_at": "2026-07-27T00:00:03+00:00",
                "component": "cleanup",
                "reason_code": "skip",
                "media_type": "movie",
                "media_title": "Waiting (2019)",
                "reason": "tagged_users_not_all_watched",
                "details": {
                    "user_tags": ["alice", "bob"],
                    "watched_by": {"alice": True, "bob": False},
                    "skip_category": "policy",
                },
            },
            {
                "recorded_at": "2026-07-27T00:00:04+00:00",
                "component": "cleanup",
                "reason_code": "dry-run",
                "media_type": "movie",
                "media_title": "Orphan Title (2018)",
                "reason": "standard watched",
                "details": {},
            },
        ]

    def test_classify_skip_categories(self):
        self.assertIsNone(
            reporting.classify_skip_category("dry-run", "standard watched", {})
        )
        self.assertEqual(
            reporting.classify_skip_category(
                "protected",
                "protected_series_or_movie_tags",
                {"protected_labels": ["kids"]},
            ),
            "kids",
        )
        self.assertEqual(
            reporting.classify_skip_category(
                "protected",
                "protected_series_or_episode_tags",
                {"protected_labels": ["safe"]},
            ),
            "safe",
        )
        self.assertEqual(
            reporting.classify_skip_category(
                "skip",
                "tagged_users_not_all_watched",
                {},
            ),
            "policy",
        )
        self.assertEqual(
            reporting.classify_skip_category("unmatched", "no_sonarr_match", {}),
            "unmatched",
        )

    def test_build_dry_run_report_shape(self):
        collector = reporting.DryRunReportCollector(component="cleanup", mode="dry-run")
        for decision in self._sample_decisions():
            collector.add_decision(decision)

        report = collector.build()

        # Top-level schema
        self.assertEqual(report["schema_version"], reporting.DRY_RUN_REPORT_SCHEMA_VERSION)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["component"], "cleanup")
        self.assertIn("generated_at", report)
        self.assertIn("summary", report)
        self.assertIn("users", report)
        self.assertIn("items", report)

        summary = report["summary"]
        self.assertEqual(summary["would_delete"], 2)
        self.assertEqual(summary["skipped"], 3)
        self.assertEqual(summary["users"], 3)  # alice, bob, _unattributed
        self.assertEqual(summary["skip_breakdown"]["kids"], 1)
        self.assertEqual(summary["skip_breakdown"]["safe"], 1)
        self.assertEqual(summary["skip_breakdown"]["policy"], 1)

        # Item shape
        for item in report["items"]:
            self.assertIn(item["action"], {"would_delete", "skip"})
            self.assertIn("reason_code", item)
            self.assertIn("media_type", item)
            self.assertIn("media_title", item)
            self.assertIn("reason", item)
            self.assertIn("related_users", item)
            if item["action"] == "skip":
                self.assertIn(item["skip_category"], reporting.SKIP_CATEGORIES)

        # Per-user buckets
        self.assertIn("alice", report["users"])
        self.assertIn("bob", report["users"])
        self.assertIn("_unattributed", report["users"])

        alice = report["users"]["alice"]
        self.assertTrue(any(i["media_title"] == "Show S01E01" for i in alice["would_delete"]))
        self.assertTrue(
            any(
                i["media_title"] == "Kids Movie (2020)" and i["skip_category"] == "kids"
                for i in alice["skipped"]
            )
        )
        self.assertTrue(
            any(
                i["media_title"] == "Waiting (2019)" and i["skip_category"] == "policy"
                for i in alice["skipped"]
            )
        )

        bob = report["users"]["bob"]
        self.assertTrue(
            any(
                i["media_title"] == "Archive Show S02E03" and i["skip_category"] == "safe"
                for i in bob["skipped"]
            )
        )

        unattributed = report["users"]["_unattributed"]
        self.assertTrue(
            any(i["media_title"] == "Orphan Title (2018)" for i in unattributed["would_delete"])
        )

    def test_write_json_and_markdown_artifacts(self):
        collector = reporting.DryRunReportCollector(component="cleanup", mode="dry-run")
        for decision in self._sample_decisions():
            collector.add_decision(decision)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = collector.write_artifacts(report_dir=tmpdir)
            self.assertIn("json", paths)
            self.assertIn("markdown", paths)
            self.assertTrue(os.path.isfile(paths["json"]))
            self.assertTrue(os.path.isfile(paths["markdown"]))

            with open(paths["json"], "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["summary"]["would_delete"], 2)

            with open(paths["markdown"], "r", encoding="utf-8") as handle:
                markdown = handle.read()
            self.assertIn("# Cleanarr dry-run report", markdown)
            self.assertIn("### alice", markdown)
            self.assertIn("skip=`kids`", markdown)
            self.assertIn("skip=`safe`", markdown)
            self.assertIn("skip=`policy`", markdown)
            self.assertIn("Would delete", markdown)

    def test_extract_related_users(self):
        users = reporting.extract_related_users(
            {
                "user": "Alice",
                "watched_by": {"Alice": True, "bob": False, "Carol": True},
                "user_tags": ["bob"],
            }
        )
        # Alice once, Carol from watched_by True, bob from user_tags
        lowered = [u.lower() for u in users]
        self.assertIn("alice", lowered)
        self.assertIn("carol", lowered)
        self.assertIn("bob", lowered)


if __name__ == '__main__':
    unittest.main()
