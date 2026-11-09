"""Unit tests for declarative cleanup rules and per-rule dry-run reports."""

import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.append(repo_root)

os.environ["CLEANARR_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "cleanarr_rules_test.log")

from cleanarr.policy import (  # noqa: E402
    decide_exact_item_delete,
    decide_tagged_user_delete,
    household_policy_allows_delete,
)
from cleanarr.rules import (  # noqa: E402
    CONDITION_TYPES,
    RuleSchemaError,
    default_ruleset,
    default_ruleset_from_config,
    dumps_ruleset,
    evaluate_rules_dry_run,
    load_ruleset,
    render_rule_dry_run_markdown,
    ruleset_from_env,
    write_rule_dry_run_artifacts,
)


# Mirrors tests/test_policy.py EXACT_ITEM_CASES. Empty household means unset.
EXACT_ITEM_CASES = [
    ([], {"alice": True, "bob": False}, "require_all_users", "", set(), False),
    (["alice"], {"alice": True, "bob": False}, "require_all_users", "", set(), True),
    (["alice", "bob"], {"alice": True, "bob": False}, "require_all_users", "", set(), False),
    (
        ["alice", "bob", "carol"],
        {"alice": True, "bob": True, "carol": False},
        "majority",
        "",
        set(),
        True,
    ),
    (["alice", "bob"], {"bob": True}, "primary_user_only", "alice", set(), False),
    (["alice", "bob"], {"alice": True}, "primary_user_only", "alice", set(), True),
    ([], {"alice": False}, "require_all_users", "", set(), False),
    (["alice"], {"alice": True}, "require_all_users", "", {"alice", "bob"}, False),
    ([], {"alice": True}, "require_all_users", "", {"alice"}, True),
]

GENERATED_AT = "2026-11-09T00:00:00+00:00"
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def _evaluate(facts, ruleset, **kwargs):
    kwargs.setdefault("generated_at", GENERATED_AT)
    kwargs.setdefault("now", NOW)
    return evaluate_rules_dry_run(facts, ruleset, **kwargs)


def _movie_request(*, status=2, created="2026-11-01T00:00:00Z", username="alice", tmdb=1):
    return {
        "id": 7,
        "status": status,
        "createdAt": created,
        "requestedBy": {"username": username, "plexUsername": username},
        "media": {"mediaType": "movie", "tmdbId": tmdb, "title": "Dune"},
    }


class TestRuleSchema(unittest.TestCase):
    def test_default_ruleset_round_trips_and_orders_protections_first(self):
        ruleset = default_ruleset()
        self.assertEqual(ruleset["version"], 1)
        self.assertEqual(ruleset["name"], "cleanarr-defaults")
        self.assertEqual(
            [rule["id"] for rule in ruleset["rules"]],
            [
                "protect-tags",
                "protect-seerr",
                "delete-household-watched",
                "delete-watched-ahead",
            ],
        )
        self.assertEqual([rule["action"] for rule in ruleset["rules"]], ["keep", "keep", "delete", "delete"])
        self.assertFalse(ruleset["rules"][1]["enabled"])
        self.assertEqual(
            ruleset["rules"][0]["conditions"][0]["labels"],
            ["keep", "kids", "safe"],
        )
        again = load_ruleset(dumps_ruleset(ruleset))
        self.assertEqual(again, ruleset)

    def test_policy_alias_is_canonical_and_unknown_policy_is_rejected(self):
        ruleset = default_ruleset(policy="all", primary_user="alice")
        watched = ruleset["rules"][2]["conditions"][0]
        self.assertEqual(watched["policy"], "require_all_users")
        self.assertEqual(watched["primary_user"], "alice")
        with self.assertRaises(RuleSchemaError):
            default_ruleset(policy="not-a-real-policy")

    def test_custom_exclusion_tags_and_seerr_settings_are_baked_into_rules(self):
        ruleset = default_ruleset(
            exclusion_tags=["Archive", "keep"],
            seerr_enabled=True,
            seerr_window_days=14,
            seerr_protected_users=["Carol", "carol"],
        )
        self.assertEqual(
            ruleset["rules"][0]["conditions"][0]["labels"],
            ["archive", "keep", "kids", "safe"],
        )
        seerr_rule = ruleset["rules"][1]
        self.assertTrue(seerr_rule["enabled"])
        condition = seerr_rule["conditions"][0]
        self.assertEqual(condition["window_days"], 14)
        self.assertEqual(condition["users"], ["Carol"])

    def test_tag_rules_scope_replaces_exact_watch_and_disables_watched_ahead(self):
        ruleset = default_ruleset(watch_scope="tag_rules", include_watched_ahead=True)
        delete_rule = ruleset["rules"][2]
        self.assertEqual(delete_rule["conditions"][0]["scope"], "tag_rules")
        self.assertEqual(delete_rule["media_types"], ["movie", "episode"])
        self.assertFalse(ruleset["rules"][3]["enabled"])

    def test_rejects_invalid_documents(self):
        cases = [
            {"version": 2, "rules": []},
            {"version": 1, "rules": [{"id": "dup", "action": "keep", "conditions": [{"type": "tag", "op": "has_any", "labels": ["safe"]}]}, {"id": "dup", "action": "keep", "conditions": [{"type": "tag", "op": "has_any", "labels": ["kids"]}]}]},
            {"version": 1, "rules": [{"id": "Bad", "action": "keep", "conditions": [{"type": "tag", "op": "has_any", "labels": ["safe"]}]}]},
            {"version": 1, "rules": [{"id": "empty", "action": "delete", "conditions": []}]},
            {"version": 1, "rules": [{"id": "extra", "action": "keep", "surprise": True, "conditions": [{"type": "tag", "op": "has_any", "labels": ["safe"]}]}]},
            {"version": 1, "rules": [{"id": "media", "action": "keep", "media_types": ["any", "movie"], "conditions": [{"type": "tag", "op": "has_any", "labels": ["safe"]}]}]},
            {"version": 1, "rules": [{"id": "age", "action": "delete", "conditions": [{"type": "age", "field": "added", "op": "older_than", "days": -1}]}]},
            {"version": 1, "rules": [{"id": "age-bool", "action": "delete", "conditions": [{"type": "age", "field": "added", "op": "older_than", "days": True}]}]},
            {"version": 1, "rules": [{"id": "who", "action": "keep", "conditions": [{"type": "request", "source": "seerr", "op": "requested_by", "users": []}]}]},
            {"version": 1, "rules": [{"id": "watts", "action": "delete", "conditions": [{"type": "filesize", "op": "gt"}]}]},
            {"version": 1, "rules": [{"id": "nope", "action": "archive", "conditions": [{"type": "tag", "op": "has_any", "labels": ["safe"]}]}]},
        ]
        for document in cases:
            with self.assertRaises(RuleSchemaError):
                load_ruleset(document)

    def test_loads_json_string_file_and_env_with_json_winning(self):
        document = {
            "version": 1,
            "name": "from-json",
            "rules": [
                {
                    "id": "keep-safe",
                    "action": "keep",
                    "conditions": [{"type": "tag", "op": "has_any", "labels": ["safe"]}],
                }
            ],
        }
        loaded = load_ruleset(json.dumps(document))
        self.assertEqual(loaded["name"], "from-json")
        self.assertEqual(loaded["rules"][0]["priority"], 100)
        self.assertEqual(loaded["rules"][0]["match"], "all")
        self.assertTrue(loaded["rules"][0]["enabled"])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "rules.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "name": "from-file", "rules": document["rules"]}, handle)
            self.assertEqual(load_ruleset(path)["name"], "from-file")
            inline = json.dumps({"version": 1, "name": "inline", "rules": document["rules"]})
            chosen = ruleset_from_env(
                {"CLEANARR_RULES_JSON": inline, "CLEANARR_RULES_FILE": path}
            )
            self.assertEqual(chosen["name"], "inline")
            self.assertEqual(ruleset_from_env({"CLEANARR_RULES_FILE": path})["name"], "from-file")

        self.assertEqual(ruleset_from_env({}).get("name"), "cleanarr-defaults")
        with self.assertRaises(RuleSchemaError):
            load_ruleset("{")
        with self.assertRaises(RuleSchemaError):
            load_ruleset(os.path.join(tempfile.gettempdir(), "cleanarr-missing-rules.json"))

    def test_config_shaped_settings_build_defaults(self):
        ruleset = default_ruleset_from_config(
            {
                "multi_user_delete_policy": "majority",
                "primary_user": "",
                "exclusion_tags": {"keep"},
                "seerr": {
                    "enabled": True,
                    "window_days": 7,
                    "protected_users": {"sam"},
                },
            }
        )
        self.assertEqual(ruleset["rules"][2]["conditions"][0]["policy"], "majority")
        self.assertTrue(ruleset["rules"][1]["enabled"])
        self.assertEqual(ruleset["rules"][1]["conditions"][0]["window_days"], 7)
        self.assertEqual(ruleset["rules"][1]["conditions"][0]["users"], ["sam"])

        from cleanarr.cleanup import CONFIG

        live = default_ruleset_from_config(CONFIG)
        self.assertEqual(live["name"], "cleanarr-defaults")
        self.assertIn("protect-tags", [rule["id"] for rule in live["rules"]])


class TestDefaultRulesMatchLegacyPolicy(unittest.TestCase):
    def test_exact_item_cases_match_decide_exact_item_delete(self):
        for user_tags, watched_by, policy, primary, household, expected in EXACT_ITEM_CASES:
            ruleset = default_ruleset(policy=policy, primary_user=primary)
            fact = {
                "media_type": "episode",
                "title": "Show S01E01",
                "tags": list(user_tags),
                "user_tags": list(user_tags),
                "watched_by": watched_by,
                "household_users": household,
                "users_ahead": [],
            }
            legacy = decide_exact_item_delete(
                user_tags,
                watched_by,
                policy=policy,
                primary_user=primary,
                household_users=household,
            )
            report = _evaluate([fact], ruleset)
            decision = report["items"][0]["decision"]
            self.assertEqual(legacy.allowed, expected)
            if expected:
                self.assertEqual(decision, "would_delete", msg=str(fact))
                self.assertEqual(report["items"][0]["decisive_rule_id"], "delete-household-watched")
            else:
                self.assertEqual(decision, "no_match", msg=str(fact))
            self.assertEqual(report["summary"]["would_delete"], 1 if expected else 0)

    def test_protected_tags_keep_even_when_household_policy_would_delete(self):
        ruleset = default_ruleset()
        fact = {
            "media_type": "movie",
            "title": "Kids Movie",
            "tags": [{"label": "10 - kids"}, "alice"],
            "user_tags": ["alice"],
            "watched_by": {"alice": True},
        }
        before = copy.deepcopy(fact)
        report = _evaluate([fact], ruleset)
        self.assertEqual(fact, before)
        item = report["items"][0]
        self.assertEqual(item["decision"], "would_keep")
        self.assertEqual(item["decisive_rule_id"], "protect-tags")
        delete_row = next(row for row in item["rules"] if row["id"] == "delete-household-watched")
        self.assertEqual(delete_row["status"], "shadowed")
        self.assertTrue(delete_row["matched"])
        self.assertFalse(delete_row["decisive"])
        legacy = decide_exact_item_delete(["alice"], {"alice": True})
        self.assertTrue(legacy.allowed)

    def test_exclusion_tag_and_safe_label_are_keeps(self):
        ruleset = default_ruleset(exclusion_tags=["archive"])
        for label in ("archive", "safe", "kids"):
            report = _evaluate(
                [{
                    "media_type": "movie",
                    "title": label,
                    "tags": [label],
                    "user_tags": ["alice"],
                    "watched_by": {"alice": True},
                }],
                ruleset,
            )
            self.assertEqual(report["items"][0]["decision"], "would_keep", msg=label)

        stock = _evaluate(
            [{
                "media_type": "movie",
                "title": "Held",
                "tags": ["KEEP"],
                "user_tags": ["alice"],
                "watched_by": {"alice": True},
            }],
            default_ruleset(),
        )
        self.assertEqual(stock["items"][0]["decision"], "would_keep")

    def test_watched_ahead_matches_household_policy_and_yields_to_tags(self):
        ruleset = default_ruleset()
        fact = {
            "media_type": "episode",
            "title": "Show S01E01",
            "user_tags": ["alice", "bob"],
            "watched_by": {},
            "household_users": ["alice", "bob"],
            "users_ahead": ["alice", "bob"],
        }
        allowed, _detail = household_policy_allows_delete(
            ["alice", "bob"],
            ["alice", "bob"],
            policy="require_all_users",
        )
        self.assertTrue(allowed)
        report = _evaluate([fact], ruleset)
        self.assertEqual(report["items"][0]["decision"], "would_delete")
        self.assertEqual(report["items"][0]["decisive_rule_id"], "delete-watched-ahead")

        partial = dict(fact, users_ahead=["alice"])
        partial_allowed, _detail = household_policy_allows_delete(
            ["alice", "bob"], ["alice"], policy="require_all_users"
        )
        self.assertFalse(partial_allowed)
        partial_report = _evaluate([partial], ruleset)
        self.assertEqual(partial_report["items"][0]["decision"], "no_match")

        protected = dict(fact, tags=["kids"])
        protected_report = _evaluate([protected], ruleset)
        self.assertEqual(protected_report["items"][0]["decision"], "would_keep")
        self.assertEqual(protected_report["items"][0]["decisive_rule_id"], "protect-tags")

    def test_tag_rules_scope_matches_decide_tagged_user_delete(self):
        ruleset = default_ruleset(watch_scope="tag_rules")
        cases = [
            ([], {"alice": True}, False),
            (["alice"], {"alice": True, "bob": False}, True),
            (["alice", "bob"], {"alice": True}, False),
        ]
        for user_tags, watched_by, expected in cases:
            fact = {
                "media_type": "episode",
                "title": "Tagged",
                "tags": list(user_tags),
                "user_tags": list(user_tags),
                "watched_by": watched_by,
            }
            legacy = decide_tagged_user_delete(user_tags, watched_by)
            report = _evaluate([fact], ruleset)
            self.assertEqual(legacy.allowed, expected)
            decision = report["items"][0]["decision"]
            if expected:
                self.assertEqual(decision, "would_delete")
            else:
                self.assertEqual(decision, "no_match")

    def test_seerr_protection_keeps_and_fail_closed_when_unreachable(self):
        ruleset = default_ruleset(seerr_enabled=True, seerr_window_days=30)
        base = {
            "media_type": "movie",
            "title": "Dune",
            "user_tags": ["alice"],
            "watched_by": {"alice": True},
            "arr_item": {"title": "Dune", "tmdbId": 1, "year": 2021},
        }
        within = dict(base, requests=[_movie_request()])
        report = _evaluate([within], ruleset)
        self.assertEqual(report["items"][0]["decision"], "would_keep")
        self.assertEqual(report["items"][0]["decisive_rule_id"], "protect-seerr")
        self.assertIn("seerr_request:", report["items"][0]["reason"])

        declined = dict(base, requests=[_movie_request(status=3)])
        self.assertEqual(_evaluate([declined], ruleset)["items"][0]["decision"], "would_delete")

        old = dict(base, requests=[_movie_request(created="2025-01-01T00:00:00Z")])
        self.assertEqual(_evaluate([old], ruleset)["items"][0]["decision"], "would_delete")

        unreachable = dict(base, requests_unavailable=True, requests=[])
        unreachable_report = _evaluate([unreachable], ruleset)
        self.assertEqual(unreachable_report["items"][0]["decision"], "would_keep")
        self.assertIn("seerr_unreachable", unreachable_report["items"][0]["reason"])

        disabled = default_ruleset(seerr_enabled=False)
        self.assertEqual(_evaluate([unreachable], disabled)["items"][0]["decision"], "would_delete")

    def test_protected_requester_is_kept_outside_the_window(self):
        ruleset = default_ruleset(
            seerr_enabled=True,
            seerr_protected_users=["carol"],
            seerr_window_days=30,
        )
        fact = {
            "media_type": "movie",
            "title": "Dune",
            "user_tags": ["alice"],
            "watched_by": {"alice": True},
            "arr_item": {"title": "Dune", "tmdbId": 1},
            "requests": [_movie_request(username="carol", created="2024-01-01T00:00:00Z")],
        }
        report = _evaluate([fact], ruleset)
        self.assertEqual(report["items"][0]["decision"], "would_keep")
        self.assertIn("protected_user", report["items"][0]["reason"])


class TestComposableConditions(unittest.TestCase):
    def test_age_rating_and_watch_must_all_match_to_delete(self):
        ruleset = load_ruleset(
            {
                "version": 1,
                "name": "low-rated-old-movies",
                "rules": [
                    {
                        "id": "delete-old-low-rated",
                        "priority": 100,
                        "action": "delete",
                        "media_types": ["movie"],
                        "match": "all",
                        "conditions": [
                            {"type": "age", "field": "added", "op": "older_than", "days": 30},
                            {"type": "rating", "source": "imdb", "op": "lt", "value": 6},
                            {"type": "watched", "scope": "exact", "policy": "require_all_users"},
                        ],
                    }
                ],
            }
        )
        watched = {"user_tags": ["alice"], "watched_by": {"alice": True}, "media_type": "movie"}
        delete_fact = dict(watched, title="Old", age_days={"added": 30}, ratings={"imdb": 5.5})
        self.assertEqual(_evaluate([delete_fact], ruleset)["items"][0]["decision"], "would_delete")

        too_new = dict(delete_fact, age_days={"added": 29})
        new_report = _evaluate([too_new], ruleset)
        self.assertEqual(new_report["items"][0]["decision"], "no_match")
        self.assertEqual(new_report["items"][0]["reason"], "no_rule_matched")
        age_row = new_report["items"][0]["rules"][0]
        self.assertEqual(age_row["status"], "not_matched")
        self.assertIn("age:", age_row["reason"])
        self.assertNotIn("watched:", age_row["reason"])

        high_rating = dict(delete_fact, ratings={"imdb": 6})
        self.assertEqual(_evaluate([high_rating], ruleset)["items"][0]["decision"], "no_match")

        missing_rating = dict(delete_fact, ratings={})
        self.assertEqual(_evaluate([missing_rating], ruleset)["items"][0]["decision"], "no_match")

        unwatched = dict(delete_fact, watched_by={"alice": False})
        self.assertEqual(_evaluate([unwatched], ruleset)["items"][0]["decision"], "no_match")

        stamped = dict(watched, title="Stamped", added_at="2026-10-01T00:00:00Z", ratings={"imdb": 1})
        self.assertEqual(_evaluate([stamped], ruleset)["items"][0]["decision"], "would_delete")

    def test_rating_any_watched_by_and_match_any(self):
        ruleset = load_ruleset(
            {
                "rules": [
                    {
                        "id": "keep-fresh-or-safe",
                        "action": "keep",
                        "match": "any",
                        "conditions": [
                            {"type": "tag", "op": "has_none", "labels": ["safe"]},
                            {"type": "age", "field": "aired", "op": "newer_than", "days": 10},
                        ],
                    }
                ]
            }
        )
        fresh = {"media_type": "episode", "title": "Fresh", "tags": ["safe"], "age_days": {"aired": 3}}
        self.assertEqual(_evaluate([fresh], ruleset)["items"][0]["decision"], "would_keep")
        old_safe = {"media_type": "episode", "title": "Old", "tags": ["safe"], "age_days": {"aired": 10}}
        # has_none safe fails, newer_than 10 fails (age is not < 10). No match.
        self.assertEqual(_evaluate([old_safe], ruleset)["items"][0]["decision"], "no_match")

        rating_rule = load_ruleset(
            {
                "rules": [
                    {
                        "id": "low-any",
                        "action": "delete",
                        "media_types": ["movie"],
                        "conditions": [
                            {"type": "rating", "source": "any", "op": "lt", "value": 6},
                            {"type": "watched_by", "op": "all", "users": ["Alice"]},
                        ],
                    }
                ]
            }
        )
        fact = {
            "media_type": "movie",
            "title": "Low",
            "ratings": {"tmdb": 5},
            "watched_by": {"alice": True},
        }
        report = _evaluate([fact], rating_rule)
        self.assertEqual(report["items"][0]["decision"], "would_delete")
        self.assertIn("tmdb 5 lt 6", report["items"][0]["reason"])

        not_alice = dict(fact, watched_by={"bob": True})
        self.assertEqual(_evaluate([not_alice], rating_rule)["items"][0]["decision"], "no_match")

    def test_lower_priority_number_wins_over_document_order(self):
        ruleset = load_ruleset(
            {
                "rules": [
                    {
                        "id": "keep-later",
                        "priority": 200,
                        "action": "keep",
                        "conditions": [{"type": "watched_by", "op": "any", "users": ["alice"]}],
                    },
                    {
                        "id": "delete-sooner",
                        "priority": 50,
                        "action": "delete",
                        "conditions": [{"type": "watched_by", "op": "any", "users": ["alice"]}],
                    },
                ]
            }
        )
        self.assertEqual([rule["id"] for rule in ruleset["rules"]], ["delete-sooner", "keep-later"])
        fact = {"media_type": "movie", "title": "Race", "watched_by": {"alice": True}}
        report = _evaluate([fact], ruleset)
        self.assertEqual(report["items"][0]["decisive_rule_id"], "delete-sooner")
        self.assertEqual(report["items"][0]["decision"], "would_delete")
        shadowed = next(row for row in report["items"][0]["rules"] if row["id"] == "keep-later")
        self.assertEqual(shadowed["status"], "shadowed")

    def test_equal_priority_keeps_document_order(self):
        ruleset = load_ruleset(
            {
                "rules": [
                    {
                        "id": "keep-first",
                        "priority": 10,
                        "action": "keep",
                        "conditions": [{"type": "tag", "op": "has_any", "labels": ["safe"]}],
                    },
                    {
                        "id": "delete-second",
                        "priority": 10,
                        "action": "delete",
                        "conditions": [{"type": "tag", "op": "has_any", "labels": ["safe"]}],
                    },
                ]
            }
        )
        fact = {"media_type": "movie", "title": "Safe", "tags": ["safe"]}
        report = _evaluate([fact], ruleset)
        self.assertEqual(report["items"][0]["decision"], "would_keep")
        self.assertEqual(report["items"][0]["decisive_rule_id"], "keep-first")

    def test_requested_by_keep_beats_a_later_delete(self):
        ruleset = load_ruleset(
            {
                "rules": [
                    {
                        "id": "keep-requested",
                        "priority": 10,
                        "action": "keep",
                        "media_types": ["movie"],
                        "conditions": [
                            {
                                "type": "request",
                                "source": "seerr",
                                "op": "requested_by",
                                "users": ["alice"],
                                "window_days": 30,
                            }
                        ],
                    },
                    {
                        "id": "delete-watched",
                        "priority": 100,
                        "action": "delete",
                        "conditions": [{"type": "watched_by", "op": "any", "users": ["alice"]}],
                    },
                ]
            }
        )
        fact = {
            "media_type": "movie",
            "title": "Dune",
            "watched_by": {"alice": True},
            "arr_item": {"tmdbId": 1, "title": "Dune"},
            "requests": [_movie_request()],
        }
        report = _evaluate([fact], ruleset)
        self.assertEqual(report["items"][0]["decision"], "would_keep")
        self.assertEqual(report["items"][0]["decisive_rule_id"], "keep-requested")

        other = dict(fact, requests=[_movie_request(username="bob")])
        self.assertEqual(_evaluate([other], ruleset)["items"][0]["decision"], "would_delete")

    def test_rule_error_never_reports_would_delete(self):
        ruleset = default_ruleset()
        fact = {
            "media_type": "movie",
            "title": "Boom",
            "user_tags": ["alice"],
            "watched_by": {"alice": True},
        }
        with patch("cleanarr.rules._eval_condition", side_effect=RuntimeError("boom")):
            report = _evaluate([fact], ruleset)
        self.assertEqual(report["items"][0]["decision"], "error")
        self.assertEqual(report["summary"]["would_delete"], 0)
        self.assertEqual(report["summary"]["errors"], 1)

    def test_non_object_fact_is_an_error(self):
        report = _evaluate(["nope"], default_ruleset())
        self.assertEqual(report["items"][0]["decision"], "error")
        self.assertEqual(report["items"][0]["reason"], "fact_not_object")
        self.assertEqual(report["summary"]["would_delete"], 0)


class TestPerRuleDryRunReport(unittest.TestCase):
    def test_report_groups_every_item_under_every_rule(self):
        facts = [
            {
                "media_type": "movie",
                "title": "Kids Movie",
                "media_id": "kids-1",
                "tags": ["kids"],
                "user_tags": ["alice"],
                "watched_by": {"alice": True},
            },
            {
                "media_type": "movie",
                "title": "Watched Movie",
                "tags": ["alice"],
                "user_tags": ["alice"],
                "watched_by": {"alice": True},
            },
            {
                "media_type": "series",
                "title": "Whole Show",
                "user_tags": ["alice"],
                "watched_by": {"alice": True},
            },
        ]
        report = _evaluate(facts, default_ruleset())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["generated_at"], GENERATED_AT)
        self.assertEqual(report["summary"]["items"], 3)
        self.assertEqual(report["summary"]["would_delete"], 1)
        self.assertEqual(report["summary"]["would_keep"], 1)
        self.assertEqual(report["summary"]["no_match"], 1)
        self.assertEqual(report["items"][0]["media_id"], "kids-1")

        decisions = [item["decision"] for item in report["items"]]
        self.assertEqual(decisions, ["would_keep", "would_delete", "no_match"])

        self.assertEqual(len(report["rules"]), 4)
        for rule in report["rules"]:
            self.assertEqual(len(rule["items"]), 3)
            counts = rule["counts"]
            accounted = (
                counts["not_matched"]
                + counts["disabled"]
                + counts["media_type_mismatch"]
                + counts["error"]
                + counts["matched"]
            )
            # shadowed rows are included in matched, so they are not added again.
            self.assertEqual(accounted, 3, msg=rule["id"])
            self.assertEqual(
                report["summary"]["per_rule"][rule["id"]]["decisive"],
                counts["decisive"],
            )

        tags = next(rule for rule in report["rules"] if rule["id"] == "protect-tags")
        self.assertEqual(tags["counts"]["would_keep"], 1)
        self.assertEqual(tags["counts"]["decisive"], 1)
        ahead = next(rule for rule in report["rules"] if rule["id"] == "delete-watched-ahead")
        self.assertGreater(ahead["counts"]["media_type_mismatch"], 0)
        seerr = next(rule for rule in report["rules"] if rule["id"] == "protect-seerr")
        self.assertEqual(seerr["counts"]["disabled"], 3)

        markdown = render_rule_dry_run_markdown(report)
        self.assertIn("# Cleanarr rule dry-run report", markdown)
        for rule_id in ("protect-tags", "protect-seerr", "delete-household-watched", "delete-watched-ahead"):
            self.assertIn(f"### {rule_id}", markdown)
        self.assertIn("Kids Movie", markdown)
        self.assertIn("Watched Movie", markdown)
        self.assertIn("dry-run only", markdown.lower())

    def test_writes_json_and_markdown_artifacts(self):
        report = _evaluate(
            [{
                "media_type": "movie",
                "title": "Watched Movie",
                "user_tags": ["alice"],
                "watched_by": {"alice": True},
            }],
            default_ruleset(),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = write_rule_dry_run_artifacts(report, report_dir=tmpdir)
            self.assertTrue(paths["json"].endswith("cleanarr-rule-dry-run-report.json"))
            with open(paths["json"], encoding="utf-8") as handle:
                written = json.load(handle)
            self.assertEqual(written["mode"], "dry-run")
            self.assertEqual(written["summary"]["would_delete"], 1)
            with open(paths["markdown"], encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("### delete-household-watched", text)
            self.assertIn("Watched Movie", text)

    def test_report_redacts_secret_values(self):
        token = "rule-report-secret-token"
        previous = os.environ.get("CLEANARR_PLEX_TOKEN")
        os.environ["CLEANARR_PLEX_TOKEN"] = token
        try:
            report = _evaluate(
                [{
                    "media_type": "movie",
                    "title": token,
                    "user_tags": ["alice"],
                    "watched_by": {"alice": True},
                }],
                default_ruleset(),
            )
        finally:
            if previous is None:
                os.environ.pop("CLEANARR_PLEX_TOKEN", None)
            else:
                os.environ["CLEANARR_PLEX_TOKEN"] = previous
        self.assertEqual(report["items"][0]["media_title"], "[REDACTED]")
        self.assertNotIn(token, json.dumps(report))

    def test_documented_condition_types_exist(self):
        self.assertEqual(
            CONDITION_TYPES,
            frozenset({"tag", "watched", "age", "rating", "request", "watched_by"}),
        )

    def test_documented_rulesets_match_the_evaluator(self):
        doc_path = os.path.join(repo_root, "docs", "rules.md")
        with open(doc_path, encoding="utf-8") as handle:
            text = handle.read()
        for condition_type in sorted(CONDITION_TYPES):
            self.assertIn(f"### Condition `{condition_type}`", text)
        documented_default = load_ruleset(_fenced_json(text, "default-ruleset"))
        self.assertEqual(documented_default, default_ruleset())

        example = load_ruleset(_fenced_json(text, "example-ruleset"))
        base = {
            "media_type": "movie",
            "title": "Dune",
            "user_tags": ["alice"],
            "watched_by": {"alice": True},
            "age_days": {"added": 40},
            "ratings": {"imdb": 5.5},
            "arr_item": {"title": "Dune", "tmdbId": 1},
        }
        delete_report = _evaluate([base], example)
        self.assertEqual(delete_report["items"][0]["decision"], "would_delete")
        self.assertEqual(delete_report["items"][0]["decisive_rule_id"], "delete-old-low-rated")

        requested = dict(base, requests=[_movie_request()])
        keep_report = _evaluate([requested], example)
        self.assertEqual(keep_report["items"][0]["decision"], "would_keep")
        self.assertEqual(keep_report["items"][0]["decisive_rule_id"], "keep-requested")

        liked = dict(base, ratings={"imdb": 8})
        self.assertEqual(_evaluate([liked], example)["items"][0]["decision"], "no_match")


def _fenced_json(text, name):
    start = f"<!-- {name}:start -->"
    end = f"<!-- {name}:end -->"
    body = text.split(start, 1)[1].split(end, 1)[0]
    raw = body.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(raw)


if __name__ == "__main__":
    unittest.main()
