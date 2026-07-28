"""Decision reporting utilities for machine-readable cleanup/webhook records."""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

DEFAULT_DECISION_REPORT_FILE = os.path.join(
    os.environ.get("CLEANARR_DECISION_REPORT_FILE", "/logs/cleanarr-decision-reports.jsonl")
)

DEFAULT_DRY_RUN_REPORT_DIR = os.environ.get(
    "CLEANARR_DRY_RUN_REPORT_DIR",
    "/logs/dry-run-reports",
)

DRY_RUN_REPORT_SCHEMA_VERSION = 1

REASON_CODES = {
    "delete",
    "skip",
    "unmatched",
    "protected",
    "dry-run",
    "error",
}

# Skip categories exposed in per-user dry-run artifacts.
SKIP_CATEGORIES = {
    "safe",
    "kids",
    "policy",
    "protected",
    "unmatched",
    "error",
}

_POLICY_SKIP_REASONS = {
    "not_watched",
    "tagged_users_not_all_watched",
    "missing_file_path",
    "outside_managed_paths",
    "no_movie_file_id",
    "watch_ahead_no_sonarr_file",
}

_SENSITIVE_KEY_HINTS = (
    "token",
    "secret",
    "password",
    "apikey",
    "api_key",
    "authorization",
)


def _load_sensitive_values() -> set:
    values = {
        os.environ.get("CLEANARR_PLEX_TOKEN"),
        os.environ.get("PLEX_TOKEN"),
        os.environ.get("CLEANARR_SONARR_APIKEY"),
        os.environ.get("CLEANARR_RADARR_APIKEY"),
        os.environ.get("CLEANARR_WEBHOOK_SECRET"),
        os.environ.get("WEBHOOK_SECRET"),
        os.environ.get("CLEANARR_NTFY_TOKEN"),
        os.environ.get("NTFY_TOKEN"),
    }
    return {value for value in values if value}


def _is_sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SENSITIVE_KEY_HINTS)


def _collect_sensitive_values(payload: Any) -> set[str]:
    values: set[str] = set()

    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if _is_sensitive_key(lowered):
                if isinstance(value, str) and value:
                    values.add(value)
                elif isinstance(value, (list, tuple, set)):
                    for item in value:
                        if isinstance(item, str) and item:
                            values.add(item)
                elif isinstance(value, dict):
                    nested_sensitive = _collect_sensitive_values(value)
                    values.update(nested_sensitive)
            else:
                values.update(_collect_sensitive_values(value))
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            values.update(_collect_sensitive_values(item))

    return values


def redact_sensitive_data(payload: Any, *, extra_secrets: Iterable[str] | None = None) -> Any:
    sensitive_values = set(_load_sensitive_values())
    sensitive_values.update(_collect_sensitive_values(payload))
    if extra_secrets:
        sensitive_values.update(v for v in extra_secrets if isinstance(v, str) and v)

    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                if _is_sensitive_key(key):
                    redacted[key] = "[REDACTED]"
                else:
                    redacted[key] = _redact(item)
            return redacted
        if isinstance(value, (list, tuple, set)):
            return [_redact(item) for item in value]
        if isinstance(value, str):
            if value in sensitive_values:
                return "[REDACTED]"
            return value
        return value

    return _redact(payload)


@dataclass(frozen=True)
class DecisionRecord:
    recorded_at: str
    component: str
    reason_code: str
    media_type: str
    media_title: str
    reason: str
    details: Dict[str, Any]


class DecisionReporter:
    """Persist decision records in JSONL for machine-readable runbook audits."""

    def __init__(self, component: str, report_file: str | None = None):
        self.component = component
        self.report_file = report_file or DEFAULT_DECISION_REPORT_FILE

    def emit(
        self,
        *,
        reason_code: str,
        media_type: str,
        media_title: str,
        reason: str,
        details: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if reason_code not in REASON_CODES:
            raise ValueError(f"unsupported decision reason code: {reason_code}")

        record = DecisionRecord(
            recorded_at=datetime.now(timezone.utc).isoformat(),
            component=self.component,
            reason_code=reason_code,
            media_type=media_type,
            media_title=media_title,
            reason=reason,
            details=details or {},
        )

        payload = redact_sensitive_data(record.__dict__)
        self._persist(payload)
        return payload

    def _persist(self, record: Dict[str, Any]):
        path = Path(self.report_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(
                f"cleanarr decision report write skipped for {path}: {exc}",
                file=sys.stderr,
            )


def classify_skip_category(
    reason_code: str,
    reason: str,
    details: Dict[str, Any] | None = None,
) -> str | None:
    """Map a decision to a dry-run skip category, or None when it is a would-delete."""
    details = details or {}
    if reason_code in {"delete", "dry-run"}:
        return None

    explicit = details.get("skip_category")
    if isinstance(explicit, str) and explicit in SKIP_CATEGORIES:
        return explicit

    protected_labels = details.get("protected_labels")
    if isinstance(protected_labels, (list, tuple, set)):
        normalized = {
            str(label).strip().lower()
            for label in protected_labels
            if str(label).strip()
        }
        if "safe" in normalized and "kids" not in normalized:
            return "safe"
        if "kids" in normalized and "safe" not in normalized:
            return "kids"
        if "safe" in normalized or "kids" in normalized:
            # Both or other protected labels present: surface both via protected.
            return "protected"

    if reason_code == "protected":
        reason_lower = (reason or "").lower()
        if "safe" in reason_lower and "kids" not in reason_lower:
            return "safe"
        if "kids" in reason_lower and "safe" not in reason_lower:
            return "kids"
        return "protected"

    if reason_code == "unmatched":
        return "unmatched"

    if reason_code == "error":
        return "error"

    if reason_code == "skip" or reason in _POLICY_SKIP_REASONS:
        return "policy"

    return "policy"


def extract_related_users(details: Dict[str, Any] | None = None) -> List[str]:
    """Derive profile/usernames associated with a decision for per-user grouping."""
    details = details or {}
    users: list[str] = []
    seen: set[str] = set()

    def _add(name: Any) -> None:
        if name is None:
            return
        text = str(name).strip()
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        users.append(text)

    for key in ("user", "watcher", "profile"):
        _add(details.get(key))

    watched_by = details.get("watched_by")
    if isinstance(watched_by, dict):
        for user, watched in watched_by.items():
            if watched:
                _add(user)
    elif isinstance(watched_by, (list, tuple, set)):
        for user in watched_by:
            _add(user)

    user_tags = details.get("user_tags")
    if isinstance(user_tags, (list, tuple, set)):
        for user in user_tags:
            _add(user)

    related = details.get("related_users")
    if isinstance(related, (list, tuple, set)):
        for user in related:
            _add(user)

    return users


def decision_to_report_item(record: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a decision payload into a dry-run report item."""
    reason_code = str(record.get("reason_code") or "")
    reason = str(record.get("reason") or "")
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    skip_category = classify_skip_category(reason_code, reason, details)
    related_users = extract_related_users(details)
    action = "would_delete" if skip_category is None else "skip"

    item = {
        "action": action,
        "reason_code": reason_code,
        "media_type": str(record.get("media_type") or "unknown"),
        "media_title": str(record.get("media_title") or "unknown"),
        "reason": reason,
        "related_users": related_users,
        "recorded_at": record.get("recorded_at"),
        "details": details,
    }
    if skip_category is not None:
        item["skip_category"] = skip_category
    return item


@dataclass
class DryRunReportCollector:
    """Accumulate cleanup decisions and emit per-user dry-run report artifacts."""

    component: str = "cleanup"
    mode: str = "dry-run"
    items: List[Dict[str, Any]] = field(default_factory=list)

    def add_decision(self, record: Dict[str, Any]) -> Dict[str, Any]:
        item = decision_to_report_item(record)
        self.items.append(item)
        return item

    def build(self) -> Dict[str, Any]:
        return build_dry_run_report(
            self.items,
            component=self.component,
            mode=self.mode,
        )

    def write_artifacts(self, report_dir: str | None = None) -> Dict[str, str]:
        report = self.build()
        return write_dry_run_report_artifacts(report, report_dir=report_dir)


def build_dry_run_report(
    items: Sequence[Dict[str, Any]],
    *,
    component: str = "cleanup",
    mode: str = "dry-run",
    generated_at: str | None = None,
) -> Dict[str, Any]:
    """Build the structured dry-run report shape (JSON schema version 1)."""
    normalized_items = [dict(item) for item in items]
    users: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    def _user_bucket(name: str) -> Dict[str, List[Dict[str, Any]]]:
        if name not in users:
            users[name] = {"would_delete": [], "skipped": []}
        return users[name]

    for item in normalized_items:
        action = item.get("action")
        related = item.get("related_users") or []
        profiles = list(related) if related else ["_unattributed"]
        for profile in profiles:
            bucket = _user_bucket(str(profile))
            if action == "would_delete":
                bucket["would_delete"].append(item)
            else:
                bucket["skipped"].append(item)

    would_delete_count = sum(1 for item in normalized_items if item.get("action") == "would_delete")
    skipped_count = sum(1 for item in normalized_items if item.get("action") != "would_delete")
    skip_breakdown: Dict[str, int] = {}
    for item in normalized_items:
        category = item.get("skip_category")
        if not category:
            continue
        skip_breakdown[category] = skip_breakdown.get(category, 0) + 1

    # Stable ordering for deterministic artifacts.
    ordered_users = {
        name: users[name]
        for name in sorted(users.keys(), key=lambda value: value.lower())
    }

    return redact_sensitive_data(
        {
            "schema_version": DRY_RUN_REPORT_SCHEMA_VERSION,
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "component": component,
            "summary": {
                "would_delete": would_delete_count,
                "skipped": skipped_count,
                "errors": skip_breakdown.get("error", 0),
                "users": len(ordered_users),
                "skip_breakdown": skip_breakdown,
            },
            "users": ordered_users,
            "items": normalized_items,
        }
    )


def render_dry_run_markdown(report: Dict[str, Any]) -> str:
    """Render a human-readable Markdown dry-run report."""
    summary = report.get("summary") or {}
    lines = [
        "# Cleanarr dry-run report",
        "",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Component: `{report.get('component')}`",
        f"- Schema version: `{report.get('schema_version')}`",
        "",
        "## Summary",
        "",
        f"- Would delete: **{summary.get('would_delete', 0)}**",
        f"- Skipped: **{summary.get('skipped', 0)}**",
        f"- Errors: **{summary.get('errors', 0)}**",
        f"- Users/profiles: **{summary.get('users', 0)}**",
    ]

    breakdown = summary.get("skip_breakdown") or {}
    if breakdown:
        lines.append("- Skip breakdown:")
        for category in sorted(breakdown.keys()):
            lines.append(f"  - `{category}`: {breakdown[category]}")

    lines.extend(["", "## Per user / profile", ""])
    users = report.get("users") or {}
    if not users:
        lines.append("_No decisions recorded._")
        lines.append("")
    else:
        for user_name, buckets in users.items():
            lines.append(f"### {user_name}")
            lines.append("")
            would_delete = buckets.get("would_delete") or []
            skipped = buckets.get("skipped") or []
            lines.append(f"- Would delete ({len(would_delete)}):")
            if would_delete:
                for item in would_delete:
                    lines.append(
                        f"  - [{item.get('media_type')}] {item.get('media_title')} "
                        f"— {item.get('reason')} (`{item.get('reason_code')}`)"
                    )
            else:
                lines.append("  - _none_")
            lines.append(f"- Skipped ({len(skipped)}):")
            if skipped:
                for item in skipped:
                    category = item.get("skip_category") or "policy"
                    lines.append(
                        f"  - [{item.get('media_type')}] {item.get('media_title')} "
                        f"— skip=`{category}` reason={item.get('reason')} "
                        f"(`{item.get('reason_code')}`)"
                    )
            else:
                lines.append("  - _none_")
            lines.append("")

    lines.extend(
        [
            "## Notes",
            "",
            "- `skip_category` values: `safe`, `kids`, `policy`, `protected`, `unmatched`, `error`.",
            "- Items without user attribution appear under `_unattributed`.",
            "- Full structured data is available in the companion JSON artifact.",
            "",
        ]
    )
    return "\n".join(lines)


def write_dry_run_report_artifacts(
    report: Dict[str, Any],
    *,
    report_dir: str | None = None,
    basename: str = "cleanarr-dry-run-report",
) -> Dict[str, str]:
    """Write JSON and Markdown dry-run report artifacts. Returns written paths."""
    target_dir = Path(report_dir or DEFAULT_DRY_RUN_REPORT_DIR)
    paths = {
        "json": str(target_dir / f"{basename}.json"),
        "markdown": str(target_dir / f"{basename}.md"),
    }
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with open(paths["json"], "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        with open(paths["markdown"], "w", encoding="utf-8") as handle:
            handle.write(render_dry_run_markdown(report))
    except OSError as exc:
        print(
            f"cleanarr dry-run report write skipped for {target_dir}: {exc}",
            file=sys.stderr,
        )
        return {}
    return paths
