"""Disk free-space rule condition: thresholds, root-folder mapping, fetch failure."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

os.environ["CLEANARR_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "cleanarr_disk_rules_test.log")

from cleanarr.disk import (  # noqa: E402
    attach_disk_facts,
    fetch_disk_facts,
    normalize_diskspace,
    path_is_under,
    select_disk_entry,
)
from cleanarr.rules import (  # noqa: E402
    RuleSchemaError,
    dumps_ruleset,
    evaluate_rules_dry_run,
    load_ruleset,
    parse_ruleset,
    render_rule_dry_run_markdown,
)

GB = 1024 ** 3
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def _entry(path, free_gb, total_gb=1000):
    return {"path": path, "free_bytes": int(free_gb * GB), "total_bytes": int(total_gb * GB)}


def _disk(free_percent, source="radarr", path="/media"):
    return {source: {"entries": [_entry(path, free_percent * 10)]}}


def _movie(disk, *, added=40, root="/media/movies", **extra):
    fact = {
        "media_type": "movie",
        "title": "Dune",
        "user_tags": ["alice"],
        "watched_by": {"alice": True},
        "age_days": {"added": added},
        "root_folder": root,
    }
    if disk is not None:
        fact["disk"] = disk
    fact.update(extra)
    return fact


def _rule(condition, *, rule_id="delete-low-disk", action="delete", priority=100, extra=()):
    return {
        "id": rule_id,
        "priority": priority,
        "action": action,
        "conditions": [dict({"type": "disk"}, **condition), *extra],
    }


def _decide(fact, rules):
    report = evaluate_rules_dry_run([fact], rules, now=NOW, generated_at="t")
    return report["items"][0]


def _documented_tiers():
    with open(os.path.join(repo_root, "docs", "rules.md"), encoding="utf-8") as handle:
        text = handle.read()
    body = text.split("<!-- disk-tiers-ruleset:start -->", 1)[1].split("<!-- disk-tiers-ruleset:end -->", 1)[0]
    return load_ruleset(json.loads(body.split("```json", 1)[1].split("```", 1)[0]))


class TestDiskConditionSchema(unittest.TestCase):
    def test_round_trips_and_defaults_source_to_any(self):
        parsed = parse_ruleset([_rule({"op": "free_gb_below", "value": 50, "path": " /media "})])
        condition = parsed["rules"][0]["conditions"][0]
        self.assertEqual(
            condition,
            {"type": "disk", "source": "any", "op": "free_gb_below", "value": 50, "path": "/media"},
        )
        self.assertEqual(load_ruleset(dumps_ruleset(parsed)), parsed)

    def test_rejects_invalid_conditions(self):
        bad = [
            {"op": "free_percent_above", "value": 10},
            {"op": "free_percent_below"},
            {"op": "free_percent_below", "value": 101},
            {"op": "free_percent_below", "value": -1},
            {"op": "free_percent_below", "value": True},
            {"op": "free_gb_below", "value": 5, "source": "lidarr"},
            {"op": "free_gb_below", "value": 5, "path": ""},
            {"op": "free_gb_below", "value": 5, "mount": "/media"},
            {"op": "free_percent_below", "value": 5, "leaving_soon_offset_percent": 101},
            {"op": "free_percent_below", "value": 5, "leaving_soon_offset_percent": -1},
        ]
        for condition in bad:
            with self.subTest(condition=condition):
                with self.assertRaises(RuleSchemaError):
                    parse_ruleset([_rule(condition)])

    def test_leaving_soon_offset_is_rejected_on_keep_rules(self):
        condition = {"op": "free_percent_below", "value": 5, "leaving_soon_offset_percent": 2}
        with self.assertRaises(RuleSchemaError):
            parse_ruleset([_rule(condition, action="keep")])
        parse_ruleset([_rule(condition, action="delete")])


class TestDiskThresholds(unittest.TestCase):
    def test_percent_threshold_is_strict(self):
        rules = [_rule({"source": "radarr", "op": "free_percent_below", "value": 10})]
        self.assertEqual(_decide(_movie(_disk(9.9)), rules)["decision"], "would_delete")
        self.assertEqual(_decide(_movie(_disk(10)), rules)["decision"], "no_match")
        self.assertEqual(_decide(_movie(_disk(50)), rules)["decision"], "no_match")

    def test_gb_threshold(self):
        rules = [_rule({"op": "free_gb_below", "value": 100})]
        low = _movie({"sonarr": {"entries": [_entry("/media", 99)]}})
        high = _movie({"sonarr": {"entries": [_entry("/media", 100)]}})
        item = _decide(low, rules)
        self.assertEqual(item["decision"], "would_delete")
        self.assertIn("sonarr /media 99GB free free_gb_below 100", item["reason"])
        self.assertEqual(_decide(high, rules)["decision"], "no_match")

    def test_gb_threshold_works_without_total_space(self):
        rules = [_rule({"op": "free_gb_below", "value": 100})]
        disk = {"radarr": {"entries": [{"path": "/media", "free_bytes": 5 * GB}]}}
        self.assertEqual(_decide(_movie(disk), rules)["decision"], "would_delete")

    def test_tiers_select_the_rule_for_the_current_pressure(self):
        tiers = _documented_tiers()
        expected = {
            # free %: (added 20 days ago, added 40 days ago)
            20: (("no_match", None), ("no_match", None)),
            11: (("no_match", None), ("would_leave_soon", "delete-low-disk")),
            8: (("no_match", None), ("would_delete", "delete-low-disk")),
            6: (("would_leave_soon", "delete-critical-disk"), ("would_delete", "delete-low-disk")),
            4: (("would_delete", "delete-critical-disk"), ("would_delete", "delete-critical-disk")),
        }
        for free, outcomes in expected.items():
            for added, (decision, rule_id) in zip((20, 40), outcomes):
                with self.subTest(free=free, added=added):
                    item = _decide(_movie(_disk(free), added=added), tiers)
                    self.assertEqual(item["decision"], decision)
                    self.assertEqual(item["decisive_rule_id"], rule_id)

    def test_tiers_never_delete_unwatched_or_protected_media(self):
        tiers = _documented_tiers()
        unwatched = _movie(_disk(1), watched_by={"alice": False})
        self.assertEqual(_decide(unwatched, tiers)["decision"], "no_match")
        kept = _movie(_disk(1), tags=["keep"])
        self.assertEqual(_decide(kept, tiers)["decision"], "would_keep")


class TestRootFolderMapping(unittest.TestCase):
    def test_longest_prefix_wins_across_root_folders(self):
        disk = {
            "radarr": {
                "entries": [
                    _entry("/", 900),
                    _entry("/media", 500),
                    _entry("/media/movies", 20),
                    _entry("/media/movies-4k", 800),
                ]
            }
        }
        rules = [_rule({"source": "radarr", "op": "free_percent_below", "value": 10})]
        self.assertEqual(_decide(_movie(disk, root="/media/movies"), rules)["decision"], "would_delete")
        self.assertEqual(_decide(_movie(disk, root="/media/movies/Dune (2021)"), rules)["decision"], "would_delete")
        # A sibling folder sharing a name prefix is a different disk.
        self.assertEqual(_decide(_movie(disk, root="/media/movies-4k"), rules)["decision"], "no_match")
        self.assertEqual(_decide(_movie(disk, root="/media/tv"), rules)["decision"], "no_match")
        self.assertEqual(_decide(_movie(disk, root="/downloads"), rules)["decision"], "no_match")

    def test_path_helpers(self):
        self.assertTrue(path_is_under("/media/movies/", "/media"))
        self.assertTrue(path_is_under("/media", "/media/"))
        self.assertFalse(path_is_under("/mediafiles", "/media"))
        self.assertTrue(path_is_under("D:\\Media\\Movies", "d:\\media"))
        self.assertFalse(path_is_under("", "/"))
        self.assertIsNone(select_disk_entry([_entry("/tv", 1)], "/media"))
        self.assertEqual(select_disk_entry([_entry("/", 1), _entry("/media", 2)], "/media/x")["path"], "/media")

    def test_root_folder_falls_back_to_arr_item(self):
        rules = [_rule({"op": "free_percent_below", "value": 10})]
        disk = {"radarr": {"entries": [_entry("/media/movies", 20), _entry("/media/tv", 800)]}}
        from_root = _movie(disk, root=None, arr_item={"rootFolderPath": "/media/movies", "path": "/media/tv/x"})
        self.assertEqual(_decide(from_root, rules)["decision"], "would_delete")
        from_path = _movie(disk, root=None, arr_item={"path": "/media/movies/Dune"})
        self.assertEqual(_decide(from_path, rules)["decision"], "would_delete")

    def test_condition_path_overrides_the_item_root_folder(self):
        disk = {"radarr": {"entries": [_entry("/media/movies", 800), _entry("/cache", 20)]}}
        rules = [_rule({"op": "free_percent_below", "value": 10, "path": "/cache"})]
        self.assertEqual(_decide(_movie(disk), rules)["decision"], "would_delete")

    def test_source_selects_which_arr_is_read(self):
        disk = {
            "radarr": {"entries": [_entry("/media", 20)]},
            "sonarr": {"entries": [_entry("/media", 800)]},
        }
        radarr = [_rule({"source": "radarr", "op": "free_percent_below", "value": 10})]
        sonarr = [_rule({"source": "sonarr", "op": "free_percent_below", "value": 10})]
        either = [_rule({"source": "any", "op": "free_percent_below", "value": 10})]
        self.assertEqual(_decide(_movie(disk), radarr)["decision"], "would_delete")
        self.assertEqual(_decide(_movie(disk), sonarr)["decision"], "no_match")
        # Same path reported twice: the reading with more free space is used.
        self.assertEqual(_decide(_movie(disk), either)["decision"], "no_match")

    def test_missing_evidence_does_not_match(self):
        rules = [_rule({"source": "radarr", "op": "free_percent_below", "value": 10})]
        cases = {
            "disk facts missing": _movie(None),
            "radarr disk facts missing": _movie({"sonarr": {"entries": [_entry("/media", 1)]}}),
            "root_folder missing": _movie(_disk(1), root=None),
            "no disk entry covers /media/movies": _movie(_disk(1, path="/tv")),
        }
        for reason, fact in cases.items():
            with self.subTest(reason=reason):
                item = _decide(fact, rules)
                self.assertEqual(item["decision"], "no_match")
                self.assertIn(reason, item["rules"][0]["reason"])


class TestDiskFetchFailure(unittest.TestCase):
    def test_failed_source_is_an_error_and_never_a_delete(self):
        rules = [_rule({"source": "radarr", "op": "free_percent_below", "value": 99})]
        report = evaluate_rules_dry_run(
            [_movie({"radarr": {"error": "unreachable"}})], rules, now=NOW, generated_at="t"
        )
        item = report["items"][0]
        self.assertEqual(item["decision"], "error")
        self.assertIsNone(item["decisive_rule_id"])
        self.assertEqual(item["rules"][0]["status"], "error")
        self.assertEqual(item["rules"][0]["reason"], "disk: radarr disk space unavailable")
        self.assertEqual(report["summary"]["would_delete"], 0)
        self.assertEqual(report["summary"]["errors"], 1)

    def test_error_wins_even_when_another_rule_would_delete(self):
        rules = [
            _rule({"source": "radarr", "op": "free_percent_below", "value": 99}),
            {
                "id": "delete-old",
                "priority": 200,
                "action": "delete",
                "conditions": [{"type": "age", "field": "added", "op": "older_than", "days": 1}],
            },
        ]
        item = _decide(_movie({"radarr": {"error": "boom"}}), rules)
        self.assertEqual(item["decision"], "error")

    def test_any_source_fails_when_either_arr_failed(self):
        rules = [_rule({"source": "any", "op": "free_percent_below", "value": 99})]
        disk = {"radarr": {"entries": [_entry("/media", 1)]}, "sonarr": {"error": "unreachable"}}
        self.assertEqual(_decide(_movie(disk), rules)["decision"], "error")
        # Reading only the healthy Arr is unaffected by the other one.
        radarr_only = [_rule({"source": "radarr", "op": "free_percent_below", "value": 99})]
        self.assertEqual(_decide(_movie(disk), radarr_only)["decision"], "would_delete")

    def test_malformed_disk_data_is_an_error(self):
        rules = [_rule({"source": "radarr", "op": "free_percent_below", "value": 99})]
        cases = [
            {"radarr": "nope"},
            {"radarr": {"entries": "nope"}},
            {"radarr": {"entries": [{"path": "/media", "free_bytes": None, "total_bytes": GB}]}},
            {"radarr": {"entries": [{"path": "/media", "free_bytes": GB, "total_bytes": 0}]}},
            {"radarr": {"entries": [{"path": "/media", "free_bytes": GB}]}},
        ]
        for disk in cases:
            with self.subTest(disk=disk):
                self.assertEqual(_decide(_movie(disk), rules)["decision"], "error")


class TestLeavingSoonOffset(unittest.TestCase):
    CONDITION = {"source": "radarr", "op": "free_percent_below", "value": 10, "leaving_soon_offset_percent": 2}
    OLD = {"type": "age", "field": "added", "op": "older_than", "days": 30}

    def test_band_enters_leaving_soon_and_only_deletes_once_crossed(self):
        rules = [_rule(self.CONDITION, extra=[self.OLD])]
        self.assertEqual(_decide(_movie(_disk(12)), rules)["decision"], "no_match")
        near = _decide(_movie(_disk(11.9)), rules)
        self.assertEqual(near["decision"], "would_leave_soon")
        self.assertEqual(near["decisive_rule_id"], "delete-low-disk")
        self.assertEqual(near["rules"][0]["status"], "leaving_soon")
        self.assertFalse(near["rules"][0]["matched"])
        self.assertIn("within 2% of free_percent_below 10", near["reason"])
        self.assertEqual(_decide(_movie(_disk(10)), rules)["decision"], "would_leave_soon")
        self.assertEqual(_decide(_movie(_disk(9.9)), rules)["decision"], "would_delete")

    def test_other_conditions_must_still_match(self):
        rules = [_rule(self.CONDITION, extra=[self.OLD])]
        self.assertEqual(_decide(_movie(_disk(11), added=5), rules)["decision"], "no_match")

    def test_gb_offset_is_a_share_of_total_space(self):
        condition = {"op": "free_gb_below", "value": 100, "leaving_soon_offset_percent": 5}
        rules = [_rule(condition)]
        # 1000 GB disk: band is 100 GB up to 150 GB.
        self.assertEqual(_decide(_movie(_disk(14.9)), rules)["decision"], "would_leave_soon")
        self.assertEqual(_decide(_movie(_disk(15)), rules)["decision"], "no_match")

    def test_earlier_keep_beats_leaving_soon(self):
        keep = {
            "id": "keep-tags",
            "priority": 10,
            "action": "keep",
            "conditions": [{"type": "tag", "op": "has_any", "labels": ["keep"]}],
        }
        rules = [keep, _rule(self.CONDITION)]
        item = _decide(_movie(_disk(11), tags=["keep"]), rules)
        self.assertEqual(item["decision"], "would_keep")
        self.assertEqual(item["rules"][1]["status"], "leaving_soon")
        self.assertFalse(item["rules"][1]["decisive"])

    def test_later_keep_does_not_hide_leaving_soon(self):
        keep = {
            "id": "keep-recent",
            "priority": 200,
            "action": "keep",
            "conditions": [{"type": "age", "field": "added", "op": "older_than", "days": 1}],
        }
        later_delete = {
            "id": "delete-anything",
            "priority": 300,
            "action": "delete",
            "conditions": [{"type": "age", "field": "added", "op": "older_than", "days": 1}],
        }
        item = _decide(_movie(_disk(11)), [_rule(self.CONDITION), keep, later_delete])
        self.assertEqual(item["decision"], "would_leave_soon")
        self.assertEqual(item["decisive_rule_id"], "delete-low-disk")
        self.assertEqual([rule["status"] for rule in item["rules"]], ["leaving_soon", "shadowed", "shadowed"])

    def test_report_counts_and_markdown(self):
        rules = [_rule(self.CONDITION)]
        facts = [_movie(_disk(11)), _movie(_disk(5), title="Arrival"), _movie(_disk(50), title="Tenet")]
        report = evaluate_rules_dry_run(facts, rules, now=NOW, generated_at="t")
        self.assertEqual(report["summary"]["would_leave_soon"], 1)
        self.assertEqual(report["summary"]["would_delete"], 1)
        self.assertEqual(report["summary"]["no_match"], 1)
        counts = report["rules"][0]["counts"]
        self.assertEqual(counts["leaving_soon"], 1)
        self.assertEqual(counts["would_leave_soon"], 1)
        self.assertEqual(counts["decisive"], 2)
        self.assertEqual(counts["matched"], 1)
        markdown = render_rule_dry_run_markdown(report)
        self.assertIn("- Would enter Leaving Soon: **1**", markdown)
        self.assertIn("Approaching a disk threshold", markdown)
        self.assertIn("[movie] Dune — decisive", markdown)


class TestFetchDiskFacts(unittest.TestCase):
    PAYLOAD = [
        {"path": "/media/", "label": "", "freeSpace": 50 * GB, "totalSpace": 1000 * GB},
        {"path": "/config", "freeSpace": 5 * GB, "totalSpace": 10 * GB},
        {"path": "", "freeSpace": 1, "totalSpace": 2},
        {"path": "/bad", "freeSpace": None, "totalSpace": 2},
        "junk",
    ]

    def test_normalizes_the_arr_diskspace_response(self):
        self.assertEqual(
            normalize_diskspace(self.PAYLOAD),
            [
                {"path": "/media", "free_bytes": 50 * GB, "total_bytes": 1000 * GB},
                {"path": "/config", "free_bytes": 5 * GB, "total_bytes": 10 * GB},
            ],
        )
        with self.assertRaises(ValueError):
            normalize_diskspace({"message": "Unauthorized"})

    def test_fetches_each_source_once_and_records_failures(self):
        radarr = MagicMock()
        radarr.request.return_value = self.PAYLOAD
        sonarr = MagicMock()
        sonarr.request.return_value = None
        sonarr.last_failure_unreachable = True
        facts = fetch_disk_facts({"radarr": radarr, "sonarr": sonarr})
        radarr.request.assert_called_once_with("diskspace")
        sonarr.request.assert_called_once_with("diskspace")
        self.assertEqual(len(facts["radarr"]["entries"]), 2)
        self.assertEqual(facts["sonarr"], {"error": "unreachable"})

        rules = [_rule({"source": "radarr", "op": "free_percent_below", "value": 10})]
        fact = attach_disk_facts(
            {"media_type": "movie", "title": "Dune", "arr_item": {"rootFolderPath": "/media/movies"}},
            facts,
        )
        self.assertEqual(fact["root_folder"], "/media/movies")
        self.assertEqual(_decide(fact, rules)["decision"], "would_delete")
        sonarr_rules = [_rule({"source": "sonarr", "op": "free_percent_below", "value": 10})]
        self.assertEqual(_decide(fact, sonarr_rules)["decision"], "error")

    def test_callable_sources_exceptions_and_skipped_sources(self):
        def boom(_endpoint):
            raise RuntimeError("connection refused")

        facts = fetch_disk_facts({"radarr": lambda endpoint: self.PAYLOAD, "sonarr": boom})
        self.assertEqual(len(facts["radarr"]["entries"]), 2)
        self.assertEqual(facts["sonarr"], {"error": "connection refused"})
        self.assertEqual(fetch_disk_facts({"radarr": None}), {})
        self.assertEqual(fetch_disk_facts({"radarr": lambda endpoint: "<html>"}), {"radarr": {"error": "diskspace response was not a list"}})
        with self.assertRaises(ValueError):
            fetch_disk_facts({"lidarr": lambda endpoint: []})

    def test_cleanup_runtime_fetches_disk_facts_once_per_run(self):
        from cleanarr import cleanup

        runtime = cleanup.MediaCleanup.__new__(cleanup.MediaCleanup)
        runtime._arr_cache = {"disk_facts": None}
        runtime.radarr_client = MagicMock()
        runtime.radarr_client.request.return_value = self.PAYLOAD
        runtime.sonarr_client = MagicMock()
        runtime.sonarr_client.request.return_value = None
        first = runtime.get_disk_facts()
        second = runtime.get_disk_facts()
        self.assertIs(first, second)
        runtime.radarr_client.request.assert_called_once_with("diskspace")
        runtime.sonarr_client.request.assert_called_once_with("diskspace")
        self.assertIn("error", first["sonarr"])


if __name__ == "__main__":
    unittest.main()
