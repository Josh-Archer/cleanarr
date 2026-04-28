"""Decision reporting utilities for machine-readable cleanup/webhook records."""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable


DEFAULT_DECISION_REPORT_FILE = os.path.join(
    os.environ.get("CLEANARR_DECISION_REPORT_FILE", "/logs/cleanarr-decision-reports.jsonl")
)

REASON_CODES = {
    "delete",
    "skip",
    "unmatched",
    "protected",
    "dry-run",
    "error",
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
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
