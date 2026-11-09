"""Declarative cleanup rules evaluated by ``cleanarr.policy``.

Rules describe when media would be kept or deleted. Evaluation is dry-run
only: it never deletes, never contacts a service, and never reads the
cleanup runtime. The schema is documented in ``docs/rules.md``.

A ruleset is a JSON object (schema version 1). Rules run in ascending
``priority`` order. Every rule is evaluated so the dry-run report can show
what each rule would have done. The first match decides the item outcome.
Later matches are reported as shadowed.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from loguru import logger

from cleanarr.policy import (
    PROTECTED_TAG_LABELS,
    canonicalize_multi_user_delete_policy,
    decide_exact_item_delete,
    decide_tagged_user_delete,
    household_policy_allows_delete,
    is_protected_tag_label,
    is_seerr_request_eligible,
    normalize_tag_label,
    resolve_household_members,
    seerr_request_matches_episode,
    seerr_request_matches_movie,
    user_in_collection,
    watched_users_from_map,
)
from cleanarr.reporting import redact_sensitive_data


RULESET_VERSION = 1
RULE_DRY_RUN_SCHEMA_VERSION = 1

DEFAULT_RULESET_NAME = "cleanarr-defaults"
MAX_RULES = 128
MAX_CONDITIONS = 32
MAX_ID_LENGTH = 64
MAX_SHORT_TEXT = 120
MAX_DESCRIPTION = 400

MEDIA_TYPES = frozenset({"any", "movie", "episode", "series", "season", "track"})
ACTIONS = frozenset({"keep", "delete"})
MATCH_MODES = frozenset({"all", "any"})
CONDITION_TYPES = frozenset({"tag", "watched", "age", "rating", "request", "watched_by"})

TAG_OPS = frozenset({"has_any", "has_all", "has_none"})
WATCHED_SCOPES = frozenset({"exact", "watched_ahead", "tag_rules"})
AGE_FIELDS = frozenset({"added", "aired", "watched", "requested"})
AGE_OPS = frozenset({"older_than", "newer_than"})
RATING_SOURCES = frozenset({"imdb", "tmdb", "rt", "audience", "any"})
RATING_OPS = frozenset({"lt", "lte", "gt", "gte", "eq"})
REQUEST_SOURCES = frozenset({"seerr"})
REQUEST_OPS = frozenset({"protected", "unprotected", "requested_by"})
WATCHED_BY_OPS = frozenset({"any", "all", "none"})

_AGE_TIMESTAMP_KEYS = {
    "added": "added_at",
    "aired": "aired_at",
    "watched": "last_watched_at",
    "requested": "requested_at",
}

_RULESET_KEYS = frozenset({"version", "name", "description", "rules"})
_RULE_KEYS = frozenset({
    "id",
    "name",
    "description",
    "enabled",
    "priority",
    "action",
    "media_types",
    "match",
    "conditions",
})


class RuleSchemaError(ValueError):
    """A ruleset document does not match the version 1 schema."""


def parse_ruleset(document: Any) -> dict:
    """Validate and normalize a ruleset document. Raises ``RuleSchemaError``."""
    if isinstance(document, list):
        document = {"version": RULESET_VERSION, "name": "custom", "rules": document}
    if not isinstance(document, dict):
        raise RuleSchemaError("ruleset must be a JSON object or a list of rules")
    _reject_unknown(document, _RULESET_KEYS, "ruleset")

    version = document.get("version", RULESET_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise RuleSchemaError("version: expected integer 1")
    if version != RULESET_VERSION:
        raise RuleSchemaError(
            f"version: unsupported ruleset version {version}; expected {RULESET_VERSION}"
        )

    name = document.get("name", "custom")
    if not isinstance(name, str) or not name.strip():
        raise RuleSchemaError("name: expected a non-empty string")
    name = name.strip()
    if len(name) > MAX_SHORT_TEXT:
        raise RuleSchemaError(f"name: longer than {MAX_SHORT_TEXT} characters")

    description = _optional_text(document.get("description"), "description", MAX_DESCRIPTION)

    rules_raw = document.get("rules")
    if not isinstance(rules_raw, list):
        raise RuleSchemaError("rules: expected a list")
    if len(rules_raw) > MAX_RULES:
        raise RuleSchemaError(f"rules: more than {MAX_RULES} rules")

    normalized = []
    seen_ids: set[str] = set()
    for index, rule in enumerate(rules_raw):
        parsed = _parse_rule(rule, f"rules[{index}]")
        if parsed["id"] in seen_ids:
            raise RuleSchemaError(f"rules[{index}].id: duplicate id '{parsed['id']}'")
        seen_ids.add(parsed["id"])
        parsed["_order"] = index
        normalized.append(parsed)

    normalized.sort(key=lambda rule: (rule["priority"], rule["_order"]))
    for rule in normalized:
        rule.pop("_order", None)

    result = {
        "version": RULESET_VERSION,
        "name": name,
        "rules": normalized,
    }
    if description is not None:
        result["description"] = description
    return result


def load_ruleset(source: Any) -> dict:
    """Load a ruleset from a dict, a JSON string, or a filesystem path."""
    if isinstance(source, dict) or isinstance(source, list):
        return parse_ruleset(source)
    if isinstance(source, os.PathLike):
        return _load_ruleset_path(Path(source))
    if isinstance(source, str):
        text = source.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuleSchemaError(f"invalid rules JSON: {exc}") from exc
            return parse_ruleset(data)
        path = Path(source)
        if path.is_file():
            return _load_ruleset_path(path)
        raise RuleSchemaError(f"ruleset not found: {source}")
    raise RuleSchemaError("ruleset must be a dict, JSON string, or file path")


def dumps_ruleset(ruleset: Mapping[str, Any]) -> str:
    """Serialize a normalized ruleset as JSON."""
    return json.dumps(parse_ruleset(dict(ruleset)), indent=2, ensure_ascii=False) + "\n"


def default_ruleset(
    *,
    policy: str = "require_all_users",
    primary_user: str = "",
    exclusion_tags: Iterable[str] = ("keep",),
    seerr_enabled: bool = False,
    seerr_window_days: int = 30,
    seerr_protected_users: Iterable[str] = (),
    include_watched_ahead: bool = True,
    watch_scope: str = "exact",
) -> dict:
    """Rules that express Cleanarr's current keep and delete behavior.

    Protections run first. A delete is proposed only when a later rule matches.
    ``watch_scope="tag_rules"`` swaps the exact-watch delete rule for the
    Jellyfin finished-event tag rules (untagged items never delete).

    Seerr and watched-ahead rules stay in the document when disabled so a
    dry-run report still lists them.
    """
    if watch_scope not in {"exact", "tag_rules"}:
        raise RuleSchemaError("watch_scope: expected 'exact' or 'tag_rules'")
    canonical_policy = canonicalize_multi_user_delete_policy(policy)
    if canonical_policy is None:
        raise RuleSchemaError(f"policy: unknown multi-user delete policy '{policy}'")

    labels = sorted({
        normalize_tag_label(str(label))
        for label in list(PROTECTED_TAG_LABELS) + list(exclusion_tags or ())
        if normalize_tag_label(str(label))
    })
    protected_users = sorted({
        str(user).strip()
        for user in (seerr_protected_users or ())
        if str(user).strip()
    })
    primary = str(primary_user or "").strip()
    watched_condition: dict[str, Any] = {
        "type": "watched",
        "scope": watch_scope,
        "policy": canonical_policy,
    }
    ahead_condition: dict[str, Any] = {
        "type": "watched",
        "scope": "watched_ahead",
        "policy": canonical_policy,
    }
    if primary:
        watched_condition["primary_user"] = primary
        ahead_condition["primary_user"] = primary

    if watch_scope == "tag_rules":
        delete_name = "Delete when tag rules and the household watch policy are satisfied"
        delete_description = (
            "Jellyfin finished events: household members are the Plex user tags. "
            "Untagged items do not match."
        )
        delete_media = ["movie", "episode"]
    else:
        delete_name = "Delete when the household watch policy is satisfied"
        delete_description = "Delete when every required watcher has an exact watch."
        delete_media = ["movie", "episode", "track"]

    document = {
        "version": RULESET_VERSION,
        "name": DEFAULT_RULESET_NAME,
        "description": (
            "Stock Cleanarr policy: protected tags, optional Seerr requests, "
            "household watch status, and watched-ahead episodes."
        ),
        "rules": [
            {
                "id": "protect-tags",
                "name": "Keep safe, kids, and exclusion tags",
                "description": "Keep media tagged safe, kids, or an exclusion tag.",
                "enabled": True,
                "priority": 10,
                "action": "keep",
                "media_types": ["any"],
                "match": "all",
                "conditions": [
                    {"type": "tag", "op": "has_any", "labels": labels},
                ],
            },
            {
                "id": "protect-seerr",
                "name": "Keep media covered by an eligible Seerr request",
                "description": (
                    "Keep a matching pending or approved Seerr request inside the "
                    "window, or from a protected requester. Disabled unless Seerr "
                    "protection is enabled. Unreachable Seerr matches this keep rule."
                ),
                "enabled": bool(seerr_enabled),
                "priority": 20,
                "action": "keep",
                "media_types": ["movie", "episode", "series"],
                "match": "all",
                "conditions": [
                    {
                        "type": "request",
                        "source": "seerr",
                        "op": "protected",
                        "window_days": int(seerr_window_days),
                        "users": protected_users,
                    },
                ],
            },
            {
                "id": "delete-household-watched",
                "name": delete_name,
                "description": delete_description,
                "enabled": True,
                "priority": 100,
                "action": "delete",
                "media_types": delete_media,
                "match": "all",
                "conditions": [watched_condition],
            },
            {
                "id": "delete-watched-ahead",
                "name": "Delete earlier episodes when the household is watched ahead",
                "description": (
                    "Delete an episode when watched-ahead viewers satisfy the same "
                    "household policy. History gap checks stay with the caller, which "
                    "passes users_ahead."
                ),
                "enabled": bool(include_watched_ahead) and watch_scope == "exact",
                "priority": 110,
                "action": "delete",
                "media_types": ["episode"],
                "match": "all",
                "conditions": [ahead_condition],
            },
        ],
    }
    return parse_ruleset(document)


def default_ruleset_from_config(config: Mapping[str, Any] | None) -> dict:
    """Build default rules from a Cleanarr CONFIG-shaped mapping.

    Does not import the cleanup runtime. Missing keys use the same stock
    defaults as an unset environment (``require_all_users``, exclusion tag
    ``keep``, Seerr off, 30-day window).
    """
    config = config or {}
    seerr = config.get("seerr") or {}
    if not isinstance(seerr, Mapping):
        raise RuleSchemaError("seerr: expected a mapping")
    window = seerr.get("window_days", 30)
    if window is None:
        window = 30
    if isinstance(window, bool) or not isinstance(window, int):
        raise RuleSchemaError("seerr.window_days: expected an integer")
    return default_ruleset(
        policy=str(config.get("multi_user_delete_policy") or "require_all_users"),
        primary_user=str(config.get("primary_user") or ""),
        exclusion_tags=config.get("exclusion_tags") or ("keep",),
        seerr_enabled=bool(seerr.get("enabled")),
        seerr_window_days=window,
        seerr_protected_users=seerr.get("protected_users") or (),
    )


def ruleset_from_env(environ: Mapping[str, str] | None = None, *, settings: Mapping[str, Any] | None = None) -> dict:
    """Load ``CLEANARR_RULES_JSON`` or ``CLEANARR_RULES_FILE``, else default rules.

    Inline JSON wins when both are set. When neither is set, defaults are built
    from ``settings`` (a CONFIG-shaped mapping) or from stock defaults.
    """
    env = os.environ if environ is None else environ
    raw_json = (env.get("CLEANARR_RULES_JSON") or "").strip()
    path = (env.get("CLEANARR_RULES_FILE") or "").strip()
    if raw_json:
        return load_ruleset(raw_json)
    if path:
        return load_ruleset(path)
    if settings is None:
        return default_ruleset()
    return default_ruleset_from_config(settings)


def evaluate_rules_dry_run(
    facts: Iterable[Mapping[str, Any]],
    ruleset: Mapping[str, Any],
    *,
    now: datetime | None = None,
    generated_at: str | None = None,
    component: str = "rules",
) -> dict:
    """Evaluate ``facts`` against ``ruleset`` and return a per-rule dry-run report.

    This function does not delete media and does not perform I/O. ``now`` fixes
    the clock for age and Seerr window checks. One clock value is used for the
    whole report.
    """
    if isinstance(ruleset, list):
        normalized = parse_ruleset(ruleset)
    else:
        normalized = parse_ruleset(dict(ruleset))
    clock = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    if not isinstance(facts, Iterable) or isinstance(facts, (str, bytes, Mapping)):
        raise RuleSchemaError("facts: expected a list of media fact objects")

    items = []
    for index, fact in enumerate(facts):
        items.append(_evaluate_fact(fact, normalized, clock, index))

    report = _build_report(
        items,
        normalized,
        generated_at=generated_at,
        component=component,
    )
    return redact_sensitive_data(report)


def render_rule_dry_run_markdown(report: Mapping[str, Any]) -> str:
    """Render a human-readable per-rule dry-run report."""
    summary = report.get("summary") or {}
    ruleset = report.get("ruleset") or {}
    lines = [
        "# Cleanarr rule dry-run report",
        "",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Component: `{report.get('component')}`",
        f"- Schema version: `{report.get('schema_version')}`",
        f"- Ruleset: `{ruleset.get('name')}` (version {ruleset.get('version')})",
        "",
        "## Summary",
        "",
        f"- Items: **{summary.get('items', 0)}**",
        f"- Would delete: **{summary.get('would_delete', 0)}**",
        f"- Would keep: **{summary.get('would_keep', 0)}**",
        f"- No match: **{summary.get('no_match', 0)}**",
        f"- Errors: **{summary.get('errors', 0)}**",
        "",
        "## Rules",
        "",
    ]
    rules = report.get("rules") or []
    if not rules:
        lines.append("_No rules._")
        lines.append("")
    for rule in rules:
        lines.extend(_render_rule_section(rule))
    lines.extend(
        [
            "## Notes",
            "",
            "- Evaluation is dry-run only. Nothing is deleted.",
            "- Rules run in ascending priority. The first match decides the item.",
            "- A later rule that also matches is shadowed and does not change the outcome.",
            "- `no_match` does not delete. Missing age, rating, or watch evidence does not match.",
            "- Grace period and Leaving Soon are applied by the cleanup runtime after a delete decision, not by this report.",
            "",
        ]
    )
    return "\n".join(lines)


def write_rule_dry_run_artifacts(
    report: Mapping[str, Any],
    *,
    report_dir: str | None = None,
    basename: str = "cleanarr-rule-dry-run-report",
) -> dict[str, str]:
    """Write JSON and Markdown per-rule dry-run artifacts. Returns written paths."""
    from cleanarr.reporting import DEFAULT_DRY_RUN_REPORT_DIR

    target_dir = Path(report_dir or DEFAULT_DRY_RUN_REPORT_DIR)
    paths = {
        "json": str(target_dir / f"{basename}.json"),
        "markdown": str(target_dir / f"{basename}.md"),
    }
    safe_report = redact_sensitive_data(dict(report))
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with open(paths["json"], "w", encoding="utf-8") as handle:
            json.dump(safe_report, handle, indent=2, ensure_ascii=False, default=str)
            handle.write("\n")
        with open(paths["markdown"], "w", encoding="utf-8") as handle:
            handle.write(render_rule_dry_run_markdown(safe_report))
    except OSError as exc:
        print(
            f"cleanarr rule dry-run report write skipped for {target_dir}: {exc}",
            file=sys.stderr,
        )
        return {}
    return paths


def _load_ruleset_path(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleSchemaError(f"cannot read ruleset {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuleSchemaError(f"invalid rules JSON in {path}: {exc}") from exc
    return parse_ruleset(data)


def _parse_rule(rule: Any, path: str) -> dict:
    if not isinstance(rule, dict):
        raise RuleSchemaError(f"{path}: expected an object")
    _reject_unknown(rule, _RULE_KEYS, path)

    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not _valid_id(rule_id):
        raise RuleSchemaError(
            f"{path}.id: expected an id matching "
            "[a-z][a-z0-9_-]{{0,{0}}}".format(MAX_ID_LENGTH - 1)
        )

    name = rule.get("name", rule_id)
    if not isinstance(name, str) or not name.strip():
        raise RuleSchemaError(f"{path}.name: expected a non-empty string")
    name = name.strip()
    if len(name) > MAX_SHORT_TEXT:
        raise RuleSchemaError(f"{path}.name: longer than {MAX_SHORT_TEXT} characters")

    description = _optional_text(rule.get("description"), f"{path}.description", MAX_DESCRIPTION)
    enabled = True if "enabled" not in rule else _require_bool(rule.get("enabled"), f"{path}.enabled")
    priority = 100 if "priority" not in rule else _require_int(
        rule.get("priority"), f"{path}.priority", minimum=-10000, maximum=10000
    )

    action = rule.get("action")
    if action not in ACTIONS:
        raise RuleSchemaError(f"{path}.action: expected one of {sorted(ACTIONS)}")

    media_types = _parse_media_types(rule.get("media_types", ["any"]), f"{path}.media_types")
    match_mode = rule.get("match", "all")
    if match_mode not in MATCH_MODES:
        raise RuleSchemaError(f"{path}.match: expected one of {sorted(MATCH_MODES)}")

    conditions_raw = rule.get("conditions")
    if not isinstance(conditions_raw, list) or not conditions_raw:
        raise RuleSchemaError(f"{path}.conditions: expected a non-empty list")
    if len(conditions_raw) > MAX_CONDITIONS:
        raise RuleSchemaError(f"{path}.conditions: more than {MAX_CONDITIONS} conditions")
    conditions = [
        _parse_condition(condition, f"{path}.conditions[{index}]")
        for index, condition in enumerate(conditions_raw)
    ]

    parsed = {
        "id": rule_id,
        "name": name,
        "enabled": enabled,
        "priority": priority,
        "action": action,
        "media_types": media_types,
        "match": match_mode,
        "conditions": conditions,
    }
    if description is not None:
        parsed["description"] = description
    return parsed


def _parse_condition(condition: Any, path: str) -> dict:
    if not isinstance(condition, dict):
        raise RuleSchemaError(f"{path}: expected an object")
    condition_type = condition.get("type")
    if condition_type not in CONDITION_TYPES:
        raise RuleSchemaError(
            f"{path}.type: unknown condition {condition_type!r}; "
            f"expected one of {sorted(CONDITION_TYPES)}"
        )
    if condition_type == "tag":
        return _parse_tag_condition(condition, path)
    if condition_type == "watched":
        return _parse_watched_condition(condition, path)
    if condition_type == "age":
        return _parse_age_condition(condition, path)
    if condition_type == "rating":
        return _parse_rating_condition(condition, path)
    if condition_type == "request":
        return _parse_request_condition(condition, path)
    return _parse_watched_by_condition(condition, path)


def _parse_tag_condition(condition: Mapping[str, Any], path: str) -> dict:
    _reject_unknown(condition, {"type", "op", "labels"}, path)
    op = condition.get("op")
    if op not in TAG_OPS:
        raise RuleSchemaError(f"{path}.op: expected one of {sorted(TAG_OPS)}")
    labels = _label_list(condition.get("labels"), f"{path}.labels")
    return {"type": "tag", "op": op, "labels": labels}


def _parse_watched_condition(condition: Mapping[str, Any], path: str) -> dict:
    _reject_unknown(condition, {"type", "scope", "policy", "primary_user"}, path)
    scope = condition.get("scope")
    if scope not in WATCHED_SCOPES:
        raise RuleSchemaError(f"{path}.scope: expected one of {sorted(WATCHED_SCOPES)}")
    policy_raw = condition.get("policy", "require_all_users")
    policy = canonicalize_multi_user_delete_policy(policy_raw)
    if policy is None:
        raise RuleSchemaError(f"{path}.policy: unknown multi-user delete policy {policy_raw!r}")
    parsed: dict[str, Any] = {"type": "watched", "scope": scope, "policy": policy}
    if "primary_user" in condition:
        primary = condition.get("primary_user")
        if not isinstance(primary, str):
            raise RuleSchemaError(f"{path}.primary_user: expected a string")
        primary = primary.strip()
        if len(primary) > MAX_SHORT_TEXT:
            raise RuleSchemaError(f"{path}.primary_user: longer than {MAX_SHORT_TEXT} characters")
        parsed["primary_user"] = primary
    return parsed


def _parse_age_condition(condition: Mapping[str, Any], path: str) -> dict:
    _reject_unknown(condition, {"type", "field", "op", "days"}, path)
    field = condition.get("field")
    if field not in AGE_FIELDS:
        raise RuleSchemaError(f"{path}.field: expected one of {sorted(AGE_FIELDS)}")
    op = condition.get("op")
    if op not in AGE_OPS:
        raise RuleSchemaError(f"{path}.op: expected one of {sorted(AGE_OPS)}")
    days = _require_number(condition.get("days"), f"{path}.days", minimum=0)
    return {"type": "age", "field": field, "op": op, "days": days}


def _parse_rating_condition(condition: Mapping[str, Any], path: str) -> dict:
    _reject_unknown(condition, {"type", "source", "op", "value"}, path)
    source = condition.get("source")
    if source not in RATING_SOURCES:
        raise RuleSchemaError(f"{path}.source: expected one of {sorted(RATING_SOURCES)}")
    op = condition.get("op")
    if op not in RATING_OPS:
        raise RuleSchemaError(f"{path}.op: expected one of {sorted(RATING_OPS)}")
    value = _require_number(condition.get("value"), f"{path}.value")
    return {"type": "rating", "source": source, "op": op, "value": value}


def _parse_request_condition(condition: Mapping[str, Any], path: str) -> dict:
    _reject_unknown(condition, {"type", "source", "op", "window_days", "users"}, path)
    source = condition.get("source")
    if source not in REQUEST_SOURCES:
        raise RuleSchemaError(
            f"{path}.source: expected one of {sorted(REQUEST_SOURCES)}"
        )
    op = condition.get("op")
    if op not in REQUEST_OPS:
        raise RuleSchemaError(f"{path}.op: expected one of {sorted(REQUEST_OPS)}")
    window = condition.get("window_days", 30)
    window_days = _require_int(window, f"{path}.window_days", minimum=0, maximum=36500)
    if op == "requested_by":
        users = _string_list(condition.get("users"), f"{path}.users", allow_empty=False)
    else:
        users = _string_list(condition.get("users", []), f"{path}.users", allow_empty=True)
    return {
        "type": "request",
        "source": source,
        "op": op,
        "window_days": window_days,
        "users": users,
    }


def _parse_watched_by_condition(condition: Mapping[str, Any], path: str) -> dict:
    _reject_unknown(condition, {"type", "op", "users"}, path)
    op = condition.get("op")
    if op not in WATCHED_BY_OPS:
        raise RuleSchemaError(f"{path}.op: expected one of {sorted(WATCHED_BY_OPS)}")
    users = _string_list(condition.get("users"), f"{path}.users", allow_empty=False)
    return {"type": "watched_by", "op": op, "users": users}


def _evaluate_fact(fact: Any, ruleset: Mapping[str, Any], now: datetime, index: int) -> dict:
    if not isinstance(fact, Mapping):
        return {
            "index": index,
            "media_type": "unknown",
            "media_title": "unknown",
            "decision": "error",
            "decisive_rule_id": None,
            "reason": "fact_not_object",
            "rules": [],
        }

    title = _fact_title(fact)
    media_type = _fact_media_type(fact)
    rule_results = []
    decisive = None
    saw_error = False
    for rule in ruleset["rules"]:
        result = _evaluate_rule(rule, fact, media_type, now)
        if result["status"] == "error":
            saw_error = True
        if result["matched"] and decisive is None and result["status"] != "error":
            result["decisive"] = True
            decisive = rule
            result["would"] = rule["action"]
        elif result["matched"]:
            result["decisive"] = False
            result["status"] = "shadowed"
            result["would"] = rule["action"]
        else:
            result["decisive"] = False
            result["would"] = None
        rule_results.append(result)

    if saw_error:
        decision = "error"
        decisive_id = None
        reason = "rule_evaluation_error"
        for result in rule_results:
            result["decisive"] = False
    elif decisive is None:
        decision = "no_match"
        decisive_id = None
        reason = "no_rule_matched"
    elif decisive["action"] == "delete":
        decision = "would_delete"
        decisive_id = decisive["id"]
        reason = next(item["reason"] for item in rule_results if item["decisive"])
    else:
        decision = "would_keep"
        decisive_id = decisive["id"]
        reason = next(item["reason"] for item in rule_results if item["decisive"])

    item = {
        "index": index,
        "media_type": media_type,
        "media_title": title,
        "decision": decision,
        "decisive_rule_id": decisive_id,
        "reason": reason,
        "related_users": _related_users(fact),
        "rules": [
            {
                "id": result["id"],
                "status": result["status"],
                "matched": result["matched"],
                "decisive": result["decisive"],
                "reason": result["reason"],
            }
            for result in rule_results
        ],
    }
    media_id = fact.get("media_id")
    if isinstance(media_id, str) or (isinstance(media_id, int) and not isinstance(media_id, bool)):
        item["media_id"] = media_id
    return item


def _evaluate_rule(rule: Mapping[str, Any], fact: Mapping[str, Any], media_type: str, now: datetime) -> dict:
    base = {
        "id": rule["id"],
        "name": rule["name"],
        "priority": rule["priority"],
        "action": rule["action"],
        "enabled": rule["enabled"],
        "matched": False,
        "would": None,
        "reason": "",
        "status": "not_matched",
    }
    if not rule["enabled"]:
        base["status"] = "disabled"
        base["reason"] = "disabled"
        return base
    if not _media_type_matches(media_type, rule["media_types"]):
        base["status"] = "media_type_mismatch"
        base["reason"] = f"media_type {media_type} not in {rule['media_types']}"
        return base
    try:
        matched, reason = _match_conditions(rule, fact, now)
    except Exception as exc:
        logger.warning(f"Rule {rule['id']} failed to evaluate: {exc}")
        base["status"] = "error"
        base["reason"] = "evaluation_error"
        return base
    base["matched"] = matched
    base["reason"] = reason
    base["status"] = "matched" if matched else "not_matched"
    return base


def _match_conditions(rule: Mapping[str, Any], fact: Mapping[str, Any], now: datetime) -> tuple[bool, str]:
    parts = []
    for condition in rule["conditions"]:
        ok, detail = _eval_condition(condition, fact, now)
        parts.append((ok, f"{condition['type']}: {detail}"))
    if rule["match"] == "any":
        matched = any(ok for ok, _detail in parts)
        chosen = [detail for ok, detail in parts if ok] if matched else [detail for _ok, detail in parts]
    else:
        matched = all(ok for ok, _detail in parts)
        if matched:
            chosen = [detail for _ok, detail in parts]
        else:
            chosen = [detail for ok, detail in parts if not ok]
    return matched, "; ".join(chosen)


def _eval_condition(condition: Mapping[str, Any], fact: Mapping[str, Any], now: datetime) -> tuple[bool, str]:
    condition_type = condition["type"]
    if condition_type == "tag":
        return _eval_tag(condition, fact)
    if condition_type == "watched":
        return _eval_watched(condition, fact)
    if condition_type == "age":
        return _eval_age(condition, fact, now)
    if condition_type == "rating":
        return _eval_rating(condition, fact)
    if condition_type == "request":
        return _eval_request(condition, fact, now)
    if condition_type == "watched_by":
        return _eval_watched_by(condition, fact)
    raise RuleSchemaError(f"unknown condition {condition_type}")


def _eval_tag(condition: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[bool, str]:
    present = set(_fact_tag_labels(fact))
    wanted = list(condition["labels"])
    found = [label for label in wanted if label in present]
    op = condition["op"]
    if op == "has_any":
        if found:
            return True, f"has_any matched {found}"
        return False, f"has_any none of {wanted}"
    if op == "has_all":
        missing = [label for label in wanted if label not in present]
        if not missing:
            return True, f"has_all matched {wanted}"
        return False, f"has_all missing {missing}"
    if found:
        return False, f"has_none found {found}"
    return True, "has_none clear"


def _eval_watched(condition: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[bool, str]:
    policy = condition["policy"]
    if "primary_user" in condition:
        primary = condition.get("primary_user") or ""
    else:
        primary = str(fact.get("primary_user") or "")
    user_tags = _fact_user_tags(fact)
    watched_by = fact.get("watched_by") if isinstance(fact.get("watched_by"), Mapping) else {}
    household = fact.get("household_users") if "household_users" in fact else None
    scope = condition["scope"]
    if scope == "exact":
        decision = decide_exact_item_delete(
            user_tags,
            watched_by,
            policy=policy,
            primary_user=primary,
            household_users=household,
        )
        return decision.allowed, decision.detail or decision.reason
    if scope == "tag_rules":
        decision = decide_tagged_user_delete(
            user_tags,
            watched_by,
            policy=policy,
            primary_user=primary,
            extra_protected_tags=fact.get("exclusion_tags"),
        )
        return decision.allowed, decision.detail or decision.reason
    users_ahead = fact.get("users_ahead") or []
    if not isinstance(users_ahead, (list, tuple, set)) or not list(users_ahead):
        return False, "no_users_ahead"
    members = resolve_household_members(user_tags, watched_by, household)
    allowed, detail = household_policy_allows_delete(
        members,
        users_ahead,
        policy=policy,
        primary_user=primary,
    )
    return allowed, detail


def _eval_age(condition: Mapping[str, Any], fact: Mapping[str, Any], now: datetime) -> tuple[bool, str]:
    field = condition["field"]
    age = _age_days(fact, field, now)
    days = float(condition["days"])
    if age is None:
        return False, f"{field} missing"
    if condition["op"] == "older_than":
        if age >= days:
            return True, f"{field} {age:.4g}d older_than {days:g}"
        return False, f"{field} {age:.4g}d not older_than {days:g}"
    if age < days:
        return True, f"{field} {age:.4g}d newer_than {days:g}"
    return False, f"{field} {age:.4g}d not newer_than {days:g}"


def _eval_rating(condition: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[bool, str]:
    ratings = fact.get("ratings") if isinstance(fact.get("ratings"), Mapping) else {}
    source = condition["source"]
    op = condition["op"]
    value = float(condition["value"])
    sources = sorted(RATING_SOURCES - {"any"}) if source == "any" else [source]
    considered = []
    for name in sources:
        number = _as_number(ratings.get(name))
        if number is None:
            continue
        considered.append((name, number))
        if _compare_number(op, number, value):
            return True, f"{name} {number:g} {op} {value:g}"
    if not considered:
        return False, f"{source} missing"
    return False, f"{source} none matched {op} {value:g}"


def _eval_request(condition: Mapping[str, Any], fact: Mapping[str, Any], now: datetime) -> tuple[bool, str]:
    op = condition["op"]
    if fact.get("requests_unavailable"):
        if op == "protected":
            return True, "seerr_unreachable"
        return False, "seerr_unreachable"

    requests = fact.get("requests") or []
    if not isinstance(requests, (list, tuple)):
        return False, "requests_invalid"
    window_days = int(condition["window_days"])
    users = condition.get("users") or []
    media_type = _fact_media_type(fact)
    arr_item = fact.get("arr_item") if isinstance(fact.get("arr_item"), Mapping) else None
    episode_item = fact.get("episode_item") if isinstance(fact.get("episode_item"), Mapping) else None
    prematched = bool(fact.get("requests_matched"))
    if not prematched and arr_item is None:
        return False, "no_media_identity"

    eligible: list[tuple[Mapping[str, Any], str]] = []
    for request in requests:
        ok, detail = is_seerr_request_eligible(
            request,
            window_days=window_days,
            protected_users=users,
            now=now,
        )
        if not ok:
            continue
        if not prematched:
            if not arr_item:
                continue
            if media_type == "movie":
                if not seerr_request_matches_movie(request, dict(arr_item)):
                    continue
            elif media_type in {"episode", "series", "season"}:
                if not seerr_request_matches_episode(request, dict(arr_item), episode_item):
                    continue
            else:
                continue
        eligible.append((request, detail))

    if op == "protected":
        if eligible:
            return True, f"seerr_request: {eligible[0][1]}"
        return False, "no_protecting_request"
    if op == "unprotected":
        if eligible:
            return False, f"seerr_request: {eligible[0][1]}"
        return True, "no_protecting_request"

    for request, detail in eligible:
        requested_by = request.get("requestedBy") if isinstance(request, Mapping) else {}
        if not isinstance(requested_by, Mapping):
            requested_by = {}
        candidates = [
            requested_by.get("username"),
            requested_by.get("displayName"),
            requested_by.get("plexUsername"),
            requested_by.get("email"),
        ]
        for candidate in candidates:
            if candidate and user_in_collection(str(candidate), users):
                return True, f"requested_by {candidate} ({detail})"
    return False, "requester_not_matched"


def _eval_watched_by(condition: Mapping[str, Any], fact: Mapping[str, Any]) -> tuple[bool, str]:
    watched_by = fact.get("watched_by") if isinstance(fact.get("watched_by"), Mapping) else {}
    watched = watched_users_from_map(watched_by)
    users = list(condition["users"])
    op = condition["op"]
    if op == "any":
        found = [user for user in users if user_in_collection(user, watched)]
        if found:
            return True, f"any matched {found}"
        return False, f"any none of {users}"
    if op == "all":
        missing = [user for user in users if not user_in_collection(user, watched)]
        if not missing:
            return True, f"all matched {users}"
        return False, f"all missing {missing}"
    found = [user for user in users if user_in_collection(user, watched)]
    if found:
        return False, f"none found {found}"
    return True, "none clear"


def _build_report(
    items: list[dict],
    ruleset: Mapping[str, Any],
    *,
    generated_at: str | None,
    component: str,
) -> dict:
    rules_out = []
    per_rule_summary = {}
    for rule in ruleset["rules"]:
        rows = []
        counts = {
            "matched": 0,
            "decisive": 0,
            "shadowed": 0,
            "not_matched": 0,
            "disabled": 0,
            "media_type_mismatch": 0,
            "error": 0,
            "would_delete": 0,
            "would_keep": 0,
        }
        for item in items:
            row = next(entry for entry in item["rules"] if entry["id"] == rule["id"]) if item["rules"] else None
            if row is None:
                status = "error" if item["decision"] == "error" and not item["rules"] else "not_matched"
                row = {
                    "id": rule["id"],
                    "status": status,
                    "matched": False,
                    "decisive": False,
                    "reason": item["reason"],
                }
            status = row["status"]
            if status == "shadowed":
                counts["shadowed"] += 1
                counts["matched"] += 1
            elif status == "matched":
                counts["matched"] += 1
            elif status in counts:
                counts[status] += 1
            else:
                counts["error"] += 1
            if row["decisive"] and item["decision"] == "would_delete":
                counts["decisive"] += 1
                counts["would_delete"] += 1
            elif row["decisive"] and item["decision"] == "would_keep":
                counts["decisive"] += 1
                counts["would_keep"] += 1
            rows.append({
                "index": item["index"],
                "media_type": item["media_type"],
                "media_title": item["media_title"],
                "status": row["status"],
                "matched": row["matched"],
                "decisive": row["decisive"],
                "would": item["decision"] if row["decisive"] else None,
                "reason": row["reason"],
                **({"media_id": item["media_id"]} if "media_id" in item else {}),
            })
        rule_report = {
            "id": rule["id"],
            "name": rule["name"],
            "enabled": rule["enabled"],
            "priority": rule["priority"],
            "action": rule["action"],
            "media_types": list(rule["media_types"]),
            "counts": counts,
            "items": rows,
        }
        if rule.get("description"):
            rule_report["description"] = rule["description"]
        rules_out.append(rule_report)
        per_rule_summary[rule["id"]] = {
            "action": rule["action"],
            "enabled": rule["enabled"],
            "priority": rule["priority"],
            **counts,
        }

    summary = {
        "items": len(items),
        "would_delete": sum(1 for item in items if item["decision"] == "would_delete"),
        "would_keep": sum(1 for item in items if item["decision"] == "would_keep"),
        "no_match": sum(1 for item in items if item["decision"] == "no_match"),
        "errors": sum(1 for item in items if item["decision"] == "error"),
        "rules": len(rules_out),
        "per_rule": per_rule_summary,
    }
    return {
        "schema_version": RULE_DRY_RUN_SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run",
        "component": component,
        "ruleset": {
            "name": ruleset["name"],
            "version": ruleset["version"],
            "rule_ids": [rule["id"] for rule in ruleset["rules"]],
        },
        "summary": summary,
        "rules": rules_out,
        "items": items,
    }


def _render_rule_section(rule: Mapping[str, Any]) -> list[str]:
    counts = rule.get("counts") or {}
    lines = [
        f"### {rule.get('id')} ({rule.get('action')}, priority {rule.get('priority')})",
        "",
        f"- Name: {rule.get('name')}",
        f"- Enabled: `{rule.get('enabled')}`",
        f"- Matched: **{counts.get('matched', 0)}**",
        f"- Decisive: **{counts.get('decisive', 0)}**",
        f"- Shadowed: **{counts.get('shadowed', 0)}**",
        f"- Not matched: **{counts.get('not_matched', 0)}**",
        f"- Would delete: **{counts.get('would_delete', 0)}**",
        f"- Would keep: **{counts.get('would_keep', 0)}**",
        "",
    ]
    matched_rows = [
        row for row in (rule.get("items") or [])
        if row.get("status") in {"matched", "shadowed"}
    ]
    if not matched_rows:
        lines.append("_No items matched._")
        lines.append("")
        return lines
    lines.append("Matched items:")
    lines.append("")
    for row in matched_rows:
        decisive = "decisive" if row.get("decisive") else "shadowed"
        lines.append(
            f"- [{row.get('media_type')}] {row.get('media_title')} — {decisive} "
            f"`{row.get('would') or rule.get('action')}` — {row.get('reason')}"
        )
    lines.append("")
    return lines


def _fact_title(fact: Mapping[str, Any]) -> str:
    for key in ("title", "media_title", "name"):
        value = fact.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _fact_media_type(fact: Mapping[str, Any]) -> str:
    value = str(fact.get("media_type") or "unknown").strip().lower()
    return value or "unknown"


def _fact_tag_labels(fact: Mapping[str, Any]) -> list[str]:
    labels = []
    tags = fact.get("tags") or []
    if not isinstance(tags, (list, tuple, set)):
        return labels
    for tag in tags:
        if isinstance(tag, Mapping):
            raw = tag.get("label") or tag.get("name") or ""
        else:
            raw = tag
        label = normalize_tag_label(str(raw))
        if label:
            labels.append(label)
    return labels


def _fact_user_tags(fact: Mapping[str, Any]) -> list[str]:
    if "user_tags" in fact and fact.get("user_tags") is not None:
        raw_tags = fact.get("user_tags") or []
        tags = []
        for tag in raw_tags:
            label = normalize_tag_label(str(tag))
            if label:
                tags.append(label)
        return tags
    exclusion = fact.get("exclusion_tags") or ()
    return [
        label
        for label in _fact_tag_labels(fact)
        if not is_protected_tag_label(label, extra_tags=exclusion)
    ]


def _related_users(fact: Mapping[str, Any]) -> list[str]:
    if not isinstance(fact, Mapping):
        return []
    seen: set[str] = set()
    users = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        users.append(text)

    for tag in _fact_user_tags(fact):
        add(tag)
    watched_by = fact.get("watched_by")
    if isinstance(watched_by, Mapping):
        for user in watched_users_from_map(watched_by):
            add(user)
    ahead = fact.get("users_ahead") or []
    if isinstance(ahead, (list, tuple, set)):
        for user in ahead:
            add(user)
    return users


def _age_days(fact: Mapping[str, Any], field: str, now: datetime) -> float | None:
    age_days = fact.get("age_days")
    if isinstance(age_days, Mapping) and field in age_days:
        number = _as_number(age_days.get(field))
        if number is not None:
            return number
    timestamp = fact.get(_AGE_TIMESTAMP_KEYS[field])
    parsed = _parse_datetime(timestamp)
    if parsed is None:
        return None
    return (_as_utc(now) - parsed).total_seconds() / 86400.0


def _media_type_matches(media_type: str, media_types: Iterable[str]) -> bool:
    allowed = set(media_types)
    if "any" in allowed:
        return True
    return media_type in allowed


def _parse_media_types(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RuleSchemaError(f"{path}: expected a non-empty list")
    parsed = []
    for entry in value:
        if not isinstance(entry, str):
            raise RuleSchemaError(f"{path}: expected strings")
        name = entry.strip().lower()
        if name not in MEDIA_TYPES:
            raise RuleSchemaError(f"{path}: unknown media type {entry!r}")
        if name not in parsed:
            parsed.append(name)
    if "any" in parsed and len(parsed) > 1:
        raise RuleSchemaError(f"{path}: 'any' cannot be combined with other media types")
    return parsed


def _label_list(value: Any, path: str) -> list[str]:
    raw = _string_list(value, path, allow_empty=False)
    labels = sorted({normalize_tag_label(label) for label in raw if normalize_tag_label(label)})
    if not labels:
        raise RuleSchemaError(f"{path}: expected at least one tag label")
    return labels


def _string_list(value: Any, path: str, *, allow_empty: bool) -> list[str]:
    if value is None and allow_empty:
        return []
    if isinstance(value, (set, tuple)):
        value = list(value)
    if not isinstance(value, list):
        raise RuleSchemaError(f"{path}: expected a list of strings")
    parsed = []
    seen = set()
    for entry in value:
        if not isinstance(entry, str):
            raise RuleSchemaError(f"{path}: expected strings")
        text = entry.strip()
        if not text:
            continue
        if len(text) > MAX_SHORT_TEXT:
            raise RuleSchemaError(f"{path}: entry longer than {MAX_SHORT_TEXT} characters")
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        parsed.append(text)
    if not parsed and not allow_empty:
        raise RuleSchemaError(f"{path}: expected at least one value")
    return parsed


def _optional_text(value: Any, path: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuleSchemaError(f"{path}: expected a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise RuleSchemaError(f"{path}: longer than {limit} characters")
    return text


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise RuleSchemaError(f"{path}: expected a boolean")
    return value


def _require_int(value: Any, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleSchemaError(f"{path}: expected an integer")
    if minimum is not None and value < minimum:
        raise RuleSchemaError(f"{path}: must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise RuleSchemaError(f"{path}: must be <= {maximum}")
    return value


def _require_number(value: Any, path: str, *, minimum: float | None = None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleSchemaError(f"{path}: expected a number")
    if minimum is not None and value < minimum:
        raise RuleSchemaError(f"{path}: must be >= {minimum}")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise RuleSchemaError(f"{path}: unknown keys {unknown}")


def _valid_id(value: str) -> bool:
    if not value or len(value) > MAX_ID_LENGTH:
        return False
    if not ("a" <= value[0] <= "z"):
        return False
    for char in value[1:]:
        if char not in "abcdefghijklmnopqrstuvwxyz0123456789_-":
            return False
    return True


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _compare_number(op: str, left: float, right: float) -> bool:
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    return abs(left - right) <= 1e-6


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
