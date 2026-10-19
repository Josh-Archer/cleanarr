#!/usr/bin/env python3
"""Core Cleanarr cleanup logic shared by the job and webhook harnesses.

Deletion policy lives in ``cleanarr.policy``. Plex, Jellyfin/Emby, Servarr,
and Transmission I/O live in ``cleanarr.adapters``. This module wires those
pieces together and keeps dry-run reporting.
"""

import os
import shutil
from pathlib import Path
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
except ImportError:
    pass
import sys
import json
import time
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import requests
from plexapi.server import PlexServer
from transmission_rpc import Client as TransmissionClient
# Kept on this module so callers can patch cleanarr.cleanup.emby_client.
# JellyfinMediaSource looks up load_watched_state on that same module.
from . import emby_client
from cleanarr.adapters.jellyfin import JellyfinMediaSource
from cleanarr.adapters.plex import PlexMediaSource
from cleanarr.adapters.servarr import ServarrClient
from cleanarr.adapters.transmission import (
    TransmissionClientAdapter,
    get_torrent_category as _get_torrent_category,
    iter_expected_incomplete_names as _iter_expected_incomplete_names,
    normalize_incomplete_name as _normalize_incomplete_name,
    transmission_labels,
)
from cleanarr.policy import (
    collect_watch_ahead_candidates,
    decide_exact_item_delete,
    household_policy_allows_delete as _policy_allows_delete,
    ids_include_any_label,
    is_protected_tag_label as _is_protected_tag_label,
    normalize_multi_user_delete_policy as _normalize_multi_user_delete_policy,
    normalize_tag_label as _normalize_tag_label,
    protected_ids_from_tags,
    protected_labels_for_ids as _policy_protected_labels_for_ids,
    repeated_io_error_disposition,
    resolve_household_members as _policy_resolve_household_members,
    torrent_cleanup_decision,
    user_tags_from_ids,
)
from cleanarr.reporting import DecisionReporter, DryRunReportCollector
from loguru import logger

# Run outcome taxonomy: full success vs partial failure vs aborted.
OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL_FAILURE = "partial_failure"
OUTCOME_ABORTED = "aborted"
_LEAVING_SOON_LOCK = threading.RLock()


@dataclass
class CleanupResult:
    """Structured result of a cleanup run for callers and exit codes."""

    outcome: str = OUTCOME_SUCCESS
    errors: list = field(default_factory=list)
    tv_deletions: list = field(default_factory=list)
    movie_deletions: list = field(default_factory=list)
    protected_skips: list = field(default_factory=list)
    message: str = ""

    @property
    def failed(self) -> bool:
        """True when the run was not a full success."""
        return self.outcome != OUTCOME_SUCCESS

    def exit_code(self) -> int:
        """Non-zero when partial failures or aborts occurred."""
        return 0 if self.outcome == OUTCOME_SUCCESS else 1

def _get_env(*keys, default=None):
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return default


def _env_flag(*keys, default="false"):
    return _get_env(*keys, default=default).lower() in ("true", "1", "yes")


def _env_csv_set(*keys, default=""):
    raw = _get_env(*keys, default=default) or ""
    return {
        value.strip().lower()
        for value in raw.split(",")
        if value.strip()
    }


CONFIG = {
    "media_source": (
        _get_env("CLEANARR_MEDIA_SOURCE", default="plex") or "plex"
    ).strip().lower(),
    "plex": {
        "baseurl": _get_env(
            "CLEANARR_PLEX_BASEURL",
            "PLEX_URL",
            default="http://plex:32400",
        ),
        "token": _get_env("CLEANARR_PLEX_TOKEN", "PLEX_TOKEN", "token"),
    },
    "sonarr": {
        "baseurl": _get_env(
            "CLEANARR_SONARR_BASEURL",
            default="http://sonarr:8989/api/v3/",
        ),
        "apikey": _get_env("CLEANARR_SONARR_APIKEY"),
    },
    "radarr": {
        "baseurl": _get_env(
            "CLEANARR_RADARR_BASEURL",
            default="http://radarr:7878/api/v3/",
        ),
        "apikey": _get_env("CLEANARR_RADARR_APIKEY"),
    },
    "readarr": {
        "baseurl": _get_env(
            "CLEANARR_READARR_BASEURL",
            default="http://readarr:8787/api/v1/",
        ),
        "apikey": _get_env("CLEANARR_READARR_APIKEY"),
    },
    "emby": {
        "baseurl": _get_env(
            "CLEANARR_EMBY_BASEURL",
            "EMBY_URL",
            default="http://emby:8096",
        ),
        "apikey": _get_env(
            "CLEANARR_EMBY_APIKEY",
            "CLEANARR_EMBY_TOKEN",
            "EMBY_APIKEY",
            "EMBY_TOKEN",
        ),
        # Optional comma-separated usernames; empty means all non-disabled users.
        "users": _env_csv_set("CLEANARR_EMBY_USERS", default=""),
    },
    # Optional Lidarr music cleanup (default OFF). When disabled, no Lidarr API
    # calls are made and music libraries are left untouched.
    "lidarr": {
        "enabled": _env_flag("CLEANARR_LIDARR_ENABLE", default="false"),
        "baseurl": _get_env(
            "CLEANARR_LIDARR_BASEURL",
            default="http://lidarr:8686/api/v1/",
        ),
        "apikey": _get_env("CLEANARR_LIDARR_APIKEY"),
    },
    "transmission": {
        "host": _get_env("CLEANARR_TRANSMISSION_HOST", default="transmission"),
        "port": int(_get_env("CLEANARR_TRANSMISSION_PORT", default="9091")),
        "username": _get_env("CLEANARR_TRANSMISSION_USERNAME"),
        "password": _get_env("CLEANARR_TRANSMISSION_PASSWORD"),
        "rpc_timeout_seconds": int(
            _get_env("CLEANARR_TRANSMISSION_RPC_TIMEOUT_SECONDS", default="90")
        ),
    },
    "log_file": _get_env("CLEANARR_LOG_FILE", default="/logs/plex-cleanup.log"),
    "debug": _env_flag("CLEANARR_DEBUG", default="true"),
    "stale_torrent_hours": int(_get_env("CLEANARR_STALE_TORRENT_HOURS", default="8")),
    "stale_torrent_require_import": _env_flag(
        "CLEANARR_STALE_TORRENT_REQUIRE_IMPORT",
        default="false",
    ),
    "dry_run": _env_flag("CLEANARR_DRY_RUN", default="false"),
    "dry_run_report_dir": _get_env(
        "CLEANARR_DRY_RUN_REPORT_DIR",
        default="/logs/dry-run-reports",
    ),
    "disable_torrent_cleanup": _env_flag("CLEANARR_DISABLE_TORRENT_CLEANUP", default="false"),
    "remove_failed_downloads": _env_flag("CLEANARR_REMOVE_FAILED_DOWNLOADS", default="false"),
    "remove_sonarr_executable_downloads": _env_flag(
        "CLEANARR_REMOVE_SONARR_EXECUTABLE_DOWNLOADS", default="false"
    ),
    "remove_orphan_incomplete_downloads": _env_flag(
        "CLEANARR_REMOVE_ORPHAN_INCOMPLETE_DOWNLOADS",
        default=_get_env("CLEANARR_REMOVE_FAILED_DOWNLOADS", default="false"),
    ),
    "remove_stale_torrents": _env_flag("CLEANARR_REMOVE_STALE_TORRENTS", default="true"),
    "torrent_cleanup_allowed_categories": _env_csv_set(
        "CLEANARR_TORRENT_CLEANUP_ALLOWED_CATEGORIES",
        default="",
    ),
    # When non-empty, ONLY torrents whose Transmission label list contains at least
    # one of these values will be considered for cleanup.  Torrents with no label
    # or an unrecognised label (e.g. adult content) are left untouched.
    # Example: "sonarr,radarr,readarr,lidarr"
    "torrent_cleanup_required_labels": _env_csv_set(
        "CLEANARR_TORRENT_CLEANUP_REQUIRED_LABELS",
        default="",
    ),
    "transmission_io_error_cleanup_enabled": _env_flag(
        "CLEANARR_TRANSMISSION_IO_ERROR_CLEANUP_ENABLED",
        default="false",
    ),
    "transmission_io_error_threshold": int(
        _get_env("CLEANARR_TRANSMISSION_IO_ERROR_THRESHOLD", default="3")
    ),
    "transmission_io_error_state_file": _get_env(
        "CLEANARR_TRANSMISSION_IO_ERROR_STATE_FILE",
        default="/logs/transmission-io-error-state.json",
    ),
    "grace_days": max(0, int(_get_env("CLEANARR_GRACE_DAYS", default="0"))),
    "leaving_soon_collection": _get_env("CLEANARR_LEAVING_SOON_COLLECTION"),
    "leaving_soon_state_file": _get_env(
        "CLEANARR_LEAVING_SOON_STATE_FILE",
        default="/logs/leaving-soon-state.json",
    ),
    "ntfy": {
        "baseurl": _get_env(
            "CLEANARR_NTFY_BASEURL",
            "CLEANARR_NTFY_URL",
            default="https://ntfy.sh",
        ).rstrip("/"),
        "topic": _get_env("CLEANARR_NTFY_TOPIC"),
        "token": _get_env("CLEANARR_NTFY_TOKEN"),
        "tags": _get_env("CLEANARR_NTFY_TAGS", default="warning,clapper"),
        "priority": _get_env("CLEANARR_NTFY_PRIORITY", default="default"),
    },
    # Household multi-user delete policy (issue #20).
    # require_all_users is the safe default so one watcher's progress cannot
    # delete media another household member still needs.
    "multi_user_delete_policy": _normalize_multi_user_delete_policy(
        _get_env("CLEANARR_MULTI_USER_DELETE_POLICY", default="require_all_users")
    ),
    "primary_user": (_get_env("CLEANARR_PRIMARY_USER", default="") or "").strip().lower(),
    "household_users": _env_csv_set("CLEANARR_HOUSEHOLD_USERS", default=""),
}

IO_ERROR_PATTERNS = (
    re.compile(r"input/output error", re.IGNORECASE),
    re.compile(r"stale file handle", re.IGNORECASE),
)


def _resolve_household_members(user_tags, watched_by, household_users=None):
    """Resolve household members, reading CONFIG when the roster is omitted."""
    if household_users is None:
        household_users = CONFIG.get("household_users") or set()
    return _policy_resolve_household_members(user_tags, watched_by, household_users)


def household_policy_allows_delete(
    household_members,
    satisfied_users,
    *,
    policy=None,
    primary_user=None,
):
    """Apply multi-user delete policy, filling omitted settings from CONFIG."""
    if policy is None:
        policy = CONFIG.get("multi_user_delete_policy")
    if primary_user is None:
        primary_user = CONFIG.get("primary_user") or ""
    return _policy_allows_delete(
        household_members,
        satisfied_users,
        policy=policy,
        primary_user=primary_user,
    )


# Setup logger
logger.remove()
logger.add(sys.stderr, level="DEBUG" if CONFIG["debug"] else "INFO")

# Optional file logging
if os.environ.get("CLEANARR_LOG_TO_FILE", "true").lower() in ("true", "1", "yes"):
    try:
        logger.add(CONFIG["log_file"], rotation="10 MB", retention="1 week", level="INFO")
    except Exception:
        logger.warning(f"Failed to initialize file logging at {CONFIG['log_file']}: permission denied or invalid path")


class MediaCleanup:
    """Main class for handling Plex media cleanup operations.

    ``CONFIG`` is the module mapping so webhook callers can read ``mc.CONFIG``
    for dry-run reason codes. ``patch.dict`` on the module dict stays visible.
    """

    CONFIG = CONFIG

    def __init__(self):
        """Initialize API connections."""
        logger.info("Initializing Plex Media Cleanup script")
        self.media_source = (CONFIG.get("media_source") or "plex").strip().lower()
        if self.media_source not in ("plex", "emby"):
            raise ValueError(
                f"Unsupported CLEANARR_MEDIA_SOURCE={self.media_source!r}; "
                "expected 'plex' or 'emby'"
            )
        self.watch_evidence_by_rating_key = {}
        self.run_summary = {
            "tv_deletions": [],
            "movie_deletions": [],
            "music_deletions": [],
            "protected_skips": [],
            "errors": [],
            "leaving_soon_entering": [],
            "deleted_after_grace": [],
        }
        self._leaving_soon_eligible = {}
        self.run_outcome = OUTCOME_SUCCESS
        self.run_abort_reason = None
        self.decision_reporter = DecisionReporter(component="cleanup")
        self.dry_run_report = DryRunReportCollector(
            component="cleanup",
            mode="dry-run" if CONFIG.get("dry_run") else "live",
        )
        self._arr_cache = {
            "sonarr_tags": None,
            "lidarr_tags": None,
            "radarr_tags": None,
            "sonarr_series": None,
            "radarr_movies": None,
            "sonarr_episodes_by_series_id": {},
            "radarr_managed_movie_roots": None,
            "lidarr_artists": None,
            "lidarr_albums_by_artist_id": {},
            "lidarr_tracks_by_album_id": {},
            "lidarr_tracks_by_artist_id": {},
        }
        # Prepare headers for Cloudflare Access if present
        self.cf_headers = {}
        cf_id = os.environ.get("CF_ACCESS_CLIENT_ID")
        cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
        if cf_id and cf_secret:
            self.cf_headers = {
                "CF-Access-Client-Id": cf_id,
                "CF-Access-Client-Secret": cf_secret
            }
            logger.debug("Cloudflare Access headers configured")

        self._emby_watched_cache = None
        self.plex = None
        self.emby_session = None
        if self.media_source == "emby":
            if not CONFIG.get("emby", {}).get("apikey"):
                logger.error(
                    "Missing Emby API key. Set CLEANARR_EMBY_APIKEY "
                    "(or CLEANARR_EMBY_TOKEN / EMBY_APIKEY)."
                )
                sys.exit(1)
            self.emby_session = requests.Session()
            if self.cf_headers:
                self.emby_session.headers.update(self.cf_headers)
        else:
            if not CONFIG["plex"]["token"]:
                logger.error("Missing Plex token. Set CLEANARR_PLEX_TOKEN or PLEX_TOKEN.")
                sys.exit(1)

        if self.media_source != "emby":
            try:
                # Create session and inject headers *before* PlexServer connects
                session = requests.Session()
                if self.cf_headers:
                    session.headers.update(self.cf_headers)
                    logger.debug("Injected Cloudflare Access headers into Plex session")

                self.plex = PlexServer(
                    CONFIG["plex"]["baseurl"],
                    CONFIG["plex"]["token"],
                    session=session,
                )
                logger.info(f"Connected to Plex server: {self.plex.friendlyName}")
            except Exception as e:
                logger.error(f"Failed to connect to Plex: {str(e)}")
        else:
            logger.info("Media source is Emby; skipping Plex client connection")

        # Initialize Sonarr Session
        self.sonarr_session = requests.Session()
        if self.cf_headers:
            self.sonarr_session.headers.update(self.cf_headers)

        # Initialize Radarr Session
        self.radarr_session = requests.Session()
        if self.cf_headers:
            self.radarr_session.headers.update(self.cf_headers)

        # Transmission is optional. In some environments (e.g., Cloud Run) we may not have
        # network reachability to the torrent client, but still want Plex/Sonarr/Radarr
        # integrations to work.
        self.transmission = None
        transmission_maintenance_enabled = (
            not CONFIG.get("disable_torrent_cleanup", False)
            or CONFIG.get("transmission_io_error_cleanup_enabled", False)
        )
        if not transmission_maintenance_enabled:
            logger.info("Transmission maintenance disabled; skipping Transmission client initialization")
        else:
            try:
                self.transmission = TransmissionClient(
                    host=CONFIG["transmission"]["host"],
                    port=CONFIG["transmission"]["port"],
                    username=CONFIG["transmission"]["username"],
                    password=CONFIG["transmission"]["password"],
                    timeout=CONFIG["transmission"]["rpc_timeout_seconds"],
                )
                logger.info(
                    "Connected to Transmission client "
                    f"(timeout={CONFIG['transmission']['rpc_timeout_seconds']}s)"
                )
            except Exception as e:
                logger.error(f"Failed to connect to Transmission: {str(e)}")
                sys.exit(1)

        self.plex_source = PlexMediaSource(self.plex, self.watch_evidence_by_rating_key)
        self.jellyfin = (
            JellyfinMediaSource(self.emby_session, CONFIG["emby"])
            if self.emby_session is not None else None
        )
        self.sonarr_client = ServarrClient(
            "Sonarr",
            CONFIG["sonarr"],
            session=self.sonarr_session,
            missing_key_env="CLEANARR_SONARR_APIKEY",
        )
        self.radarr_client = ServarrClient(
            "Radarr",
            CONFIG["radarr"],
            session=self.radarr_session,
            missing_key_env="CLEANARR_RADARR_APIKEY",
        )
        self.readarr_client = ServarrClient(
            "Readarr",
            CONFIG["readarr"],
            session_factory=requests.Session,
            missing_key_env="CLEANARR_READARR_APIKEY",
        )
        # Lidarr sessions stay lazy and do not inherit Cloudflare headers.
        self.lidarr_client = ServarrClient(
            "Lidarr",
            CONFIG["lidarr"],
            session_factory=requests.Session,
            missing_key_env="CLEANARR_LIDARR_APIKEY",
        )

    @property
    def transmission_io(self):
        """Transmission adapter for the current client.

        Tests and callers may replace ``self.transmission`` after init.
        The adapter always wraps that object.
        """
        if self.transmission is None:
            return None
        return TransmissionClientAdapter(self.transmission)

    def _mark_partial_failure(self):
        """Mark the run as a partial failure unless already aborted."""
        if self.run_outcome != OUTCOME_ABORTED:
            self.run_outcome = OUTCOME_PARTIAL_FAILURE

    def _mark_aborted(self, reason):
        """Mark the run as aborted and record the reason."""
        self.run_outcome = OUTCOME_ABORTED
        self.run_abort_reason = reason
        self.run_summary.setdefault("errors", []).append(f"aborted: {reason}")

    def _record_summary(self, category, message):
        self.run_summary.setdefault(category, []).append(message)
        # Errors indicate at least a partial failure for exit/reporting.
        if category == "errors":
            self._mark_partial_failure()

    def _load_leaving_soon_state(self):
        path = CONFIG.get("leaving_soon_state_file")
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as state_file:
                value = json.load(state_file)
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.error(f"Failed to load Leaving Soon state: {exc}")
            self._record_summary("errors", f"Leaving Soon state load failed: {exc}")
            return {}

    def _save_leaving_soon_state(self, state):
        path = CONFIG.get("leaving_soon_state_file")
        if not path:
            return
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(target)
        except Exception as exc:
            logger.error(f"Failed to persist Leaving Soon state: {exc}")
            self._record_summary("errors", f"Leaving Soon state persist failed: {exc}")

    @staticmethod
    def leaving_soon_key(item):
        rating_key = item.get("rating_key")
        if rating_key:
            return f"plex:{rating_key}"
        guid = item.get("guid")
        return f"guid:{guid}" if guid else None

    def leaving_soon_allows_delete(self, key, title="media", rating_key=None):
        """Record eligible media and return whether its grace period has elapsed."""
        grace_days = max(0, int(CONFIG.get("grace_days", 0) or 0))
        if grace_days == 0 or not key:
            return True
        now = datetime.now(timezone.utc)
        with _LEAVING_SOON_LOCK:
            state = self._load_leaving_soon_state()
            record = state.get(key)
            if not isinstance(record, str):
                state[key] = now.isoformat()
                self.run_summary.setdefault("leaving_soon_entering", []).append(title)
                self._save_leaving_soon_state(state)
                self._leaving_soon_eligible[key] = state[key]
                self._sync_leaving_soon_collection(state)
                return False
            self._leaving_soon_eligible[key] = record
            try:
                first = datetime.fromisoformat(record)
                if first.tzinfo is None:
                    first = first.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                first = now
                state[key] = now.isoformat()
                record = state[key]
                self._save_leaving_soon_state(state)
            expired = now >= first + timedelta(days=grace_days)
            return expired

    def _sync_leaving_soon_collection(self, state):
        collection_name = CONFIG.get("leaving_soon_collection")
        if self.media_source != "plex" or not self.plex_source or not collection_name:
            return
        try:
            self.plex_source.sync_leaving_soon_collection(
                collection_name,
                [key.removeprefix("plex:") for key in state if key.startswith("plex:")],
                dry_run=bool(CONFIG.get("dry_run")),
            )
        except Exception as exc:
            logger.error(f"Failed to sync Leaving Soon collection: {exc}")
            self._record_summary("errors", f"Leaving Soon collection sync failed: {exc}")

    def _sync_leaving_soon(self):
        """Drop entries that are no longer eligible and sync their Plex collection."""
        if int(CONFIG.get("grace_days", 0) or 0) <= 0:
            return
        with _LEAVING_SOON_LOCK:
            state = self._load_leaving_soon_state()
            retained = {key: value for key, value in state.items() if key in self._leaving_soon_eligible}
            self._save_leaving_soon_state(retained)
        self._sync_leaving_soon_collection(retained)

    def _record_decision(
        self,
        reason_code,
        media_type,
        media_title,
        reason,
        details=None,
    ):
        payload = self.decision_reporter.emit(
            reason_code=reason_code,
            media_type=media_type,
            media_title=media_title,
            reason=reason,
            details=details or {},
        )
        # Always collect for optional artifact emission; written only for dry-run jobs.
        self.dry_run_report.add_decision(payload)
        return payload

    def _protected_labels_for_ids(self, tags, tag_ids):
        """Return protected tag labels (safe/kids) present in the given id set."""
        return _policy_protected_labels_for_ids(tags, tag_ids)

    def _summarize_entries(self, category, label):
        entries = self.run_summary.get(category, [])
        if not entries:
            return f"{label}: none"
        return f"{label} ({len(entries)}): " + "; ".join(entries[:10])

    def _outcome_status_line(self):
        error_count = len(self.run_summary.get("errors", []))
        if self.run_outcome == OUTCOME_ABORTED:
            reason = self.run_abort_reason or "error"
            return f"Outcome: {OUTCOME_ABORTED} ({reason})"
        if self.run_outcome == OUTCOME_PARTIAL_FAILURE:
            return f"Outcome: {OUTCOME_PARTIAL_FAILURE} ({error_count} error(s))"
        return f"Outcome: {OUTCOME_SUCCESS}"

    def _build_result(self) -> CleanupResult:
        return CleanupResult(
            outcome=self.run_outcome,
            errors=list(self.run_summary.get("errors", [])),
            tv_deletions=list(self.run_summary.get("tv_deletions", [])),
            movie_deletions=list(self.run_summary.get("movie_deletions", [])),
            protected_skips=list(self.run_summary.get("protected_skips", [])),
            message=self.run_abort_reason or "",
        )

    def _send_ntfy_summary(self, title, lines, priority="default", tags=None):
        topic = CONFIG.get("ntfy", {}).get("topic")
        if not topic:
            return

        url = f"{CONFIG['ntfy']['baseurl']}/{topic}"
        headers = {
            "Title": title,
            "Priority": str(priority or CONFIG["ntfy"]["priority"]),
        }
        token = CONFIG["ntfy"].get("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if tags is None:
            tags = CONFIG["ntfy"].get("tags")
        if tags:
            headers["Tags"] = tags

        try:
            response = requests.post(url, data="\n".join(lines).encode("utf-8"), headers=headers, timeout=10)
            response.raise_for_status()
            logger.info("Sent Cleanarr summary notification to ntfy")
        except Exception as exc:
            logger.error(f"Failed to send ntfy notification: {exc}")
            # Do not use _record_summary here: ntfy is secondary reporting and
            # should not rewrite the cleanup outcome after the fact.
            self.run_summary.setdefault("errors", []).append(f"ntfy failed: {exc}")
            self._mark_partial_failure()

    def _write_dry_run_report_artifacts(self):
        """Emit per-user JSON/Markdown dry-run report artifacts (job mode)."""
        if not CONFIG.get("dry_run"):
            return None
        report_dir = CONFIG.get("dry_run_report_dir") or "/logs/dry-run-reports"
        paths = self.dry_run_report.write_artifacts(report_dir=report_dir)
        if not paths:
            logger.warning("Dry-run report artifacts were not written")
            return None
        logger.info(
            "Wrote dry-run report artifacts: "
            f"json={paths.get('json')} markdown={paths.get('markdown')}"
        )
        return paths

    def _flush_run_summary(self):
        lines = [
            self._outcome_status_line(),
            self._summarize_entries("tv_deletions", "TV deletions"),
            self._summarize_entries("movie_deletions", "Movie deletions"),
            self._summarize_entries("protected_skips", "Protected skips"),
            self._summarize_entries("errors", "Errors"),
        ]
        if int(CONFIG.get("grace_days", 0) or 0) > 0:
            lines.insert(
                4,
                f"{len(self.run_summary.get('leaving_soon_entering', []))} items entering Leaving Soon / "
                f"{len(self.run_summary.get('deleted_after_grace', []))} items deleted after grace",
            )
        logger.info("Cleanup summary | " + " | ".join(lines))

        dry_run_paths = self._write_dry_run_report_artifacts()
        if dry_run_paths:
            lines.append(f"Dry-run report JSON: {dry_run_paths.get('json')}")
            lines.append(f"Dry-run report Markdown: {dry_run_paths.get('markdown')}")

        deletion_count = (
            len(self.run_summary.get("tv_deletions", []))
            + len(self.run_summary.get("movie_deletions", []))
        )
        error_count = len(self.run_summary.get("errors", []))
        # Always report when there are failures, even if no deletions succeeded.
        if deletion_count == 0 and error_count == 0 and not CONFIG.get("dry_run"):
            return

        title_prefix = "[DRY RUN] " if CONFIG.get("dry_run") else ""
        if self.run_outcome == OUTCOME_ABORTED:
            title = f"{title_prefix}Cleanarr FAILED (aborted)"
            priority = "urgent"
            tags = "x,warning"
        elif self.run_outcome == OUTCOME_PARTIAL_FAILURE:
            title = f"{title_prefix}Cleanarr FAILED (partial)"
            priority = "high"
            tags = "warning,x"
        else:
            title = f"{title_prefix}Cleanarr summary"
            priority = "high" if deletion_count else "default"
            tags = None

        self._send_ntfy_summary(title, lines, priority=priority, tags=tags)

    def _torrent_cleanup_allowed(self, torrent, reason):
        """Apply label and category policy. Transmission I/O stays with the caller."""
        torrent_name = getattr(torrent, "name", "unknown torrent")
        # Required labels restrict cleanup to *arr-managed torrents and leave
        # untagged content untouched. A download-directory name counts as a
        # label because Readarr does not send Transmission labels.
        required_labels = CONFIG.get("torrent_cleanup_required_labels") or set()
        allowed_categories = CONFIG.get("torrent_cleanup_allowed_categories") or set()
        category = _get_torrent_category(torrent)
        allowed, skip_kind, effective_labels = torrent_cleanup_decision(
            labels=transmission_labels(torrent),
            category=category,
            required_labels=required_labels,
            allowed_categories=allowed_categories,
        )
        if allowed:
            return True
        if skip_kind == "labels":
            logger.info(
                f"Skipping torrent cleanup for {torrent_name} "
                f"during {reason}: labels {effective_labels or set()} do not "
                f"match required {sorted(required_labels)}"
            )
            return False
        logger.info(
            f"Skipping torrent cleanup for {torrent_name} "
            f"during {reason}: category '{category or 'unknown'}' is not in "
            f"{sorted(allowed_categories)}"
        )
        return False

    def _load_io_error_state(self):
        """Load repeated Transmission I/O error counters from the shared logs volume."""
        state_file = CONFIG["transmission_io_error_state_file"]
        if not os.path.exists(state_file):
            return {}
        try:
            with open(state_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Failed to read Transmission I/O error state {state_file}: {exc}")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_io_error_state(self, state):
        """Persist repeated Transmission I/O error counters atomically."""
        state_file = CONFIG["transmission_io_error_state_file"]
        state_dir = os.path.dirname(state_file)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        tmp_file = f"{state_file}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp_file, state_file)

    def _torrent_state_key(self, torrent):
        """Return a stable key for tracking the same torrent across cleanup runs."""
        torrent_hash = getattr(torrent, "hashString", None) or getattr(torrent, "hash_string", None)
        if isinstance(torrent_hash, str) and torrent_hash:
            return torrent_hash
        return f"{torrent.id}:{torrent.name}"

    def _get_torrent_error_string(self, torrent):
        error_string = getattr(torrent, "error_string", None)
        if not isinstance(error_string, str):
            error_string = str(error_string or "")
        return error_string.strip()

    def _is_repeated_io_error(self, torrent):
        error_string = self._get_torrent_error_string(torrent)
        if getattr(torrent, "error", 0) == 0 or not error_string:
            return False, error_string
        return any(pattern.search(error_string) for pattern in IO_ERROR_PATTERNS), error_string

    def clean_repeated_io_error_torrents(self):
        """Remove Transmission metadata for torrents that repeatedly hit storage I/O errors."""
        if not CONFIG.get("transmission_io_error_cleanup_enabled", False):
            logger.info("Transmission repeated I/O error cleanup is disabled")
            return
        if not self.transmission:
            logger.warning("Transmission repeated I/O error cleanup enabled, but client is unavailable")
            return

        logger.info("Checking Transmission for repeated storage I/O errors")
        threshold = max(1, CONFIG.get("transmission_io_error_threshold", 3))
        current_time = datetime.now(timezone.utc).isoformat()
        prior_state = self._load_io_error_state()
        next_state = {}

        try:
            torrents = self.transmission_io.get_torrents()
        except Exception as exc:
            logger.error(f"Failed to list Transmission torrents for I/O error cleanup: {exc}")
            self._record_summary("errors", f"I/O error cleanup list failed: {exc}")
            return

        for torrent in torrents:
            torrent_key = self._torrent_state_key(torrent)
            matched, error_string = self._is_repeated_io_error(torrent)
            if not matched:
                continue

            previous = prior_state.get(torrent_key, {})
            count = int(previous.get("count", 0)) + 1
            active = (
                getattr(torrent, "rate_download", 0) > 0
                or getattr(torrent, "rate_upload", 0) > 0
                or getattr(torrent, "status", None) in (3, 4, 5, 6)
            )

            entry = {
                "count": count,
                "name": torrent.name,
                "error_string": error_string,
                "download_dir": getattr(torrent, "download_dir", ""),
                "first_seen": previous.get("first_seen", current_time),
                "last_seen": current_time,
            }

            disposition = repeated_io_error_disposition(
                count=count,
                threshold=threshold,
                active=active,
                dry_run=bool(CONFIG["dry_run"]),
            )
            if disposition == "leave_active":
                logger.warning(
                    f"Torrent {torrent.name} is reporting storage I/O errors but is still active; "
                    f"leaving it alone for now ({count}/{threshold})"
                )
                next_state[torrent_key] = entry
                continue

            if disposition == "below_threshold":
                logger.warning(
                    f"Torrent {torrent.name} hit storage I/O error {count}/{threshold}: {error_string}"
                )
                next_state[torrent_key] = entry
                continue

            if disposition == "dry_run":
                logger.info(
                    f"[DRY RUN] Would remove torrent metadata for repeated storage I/O errors: "
                    f"{torrent.name} ({error_string})"
                )
                next_state[torrent_key] = entry
                continue

            logger.warning(
                f"Removing torrent metadata after repeated storage I/O errors: "
                f"{torrent.name} ({error_string})"
            )
            try:
                self.transmission_io.remove_torrent(torrent.id, delete_data=False)
            except Exception as exc:
                logger.error(f"Failed to remove torrent {torrent.name} after repeated I/O errors: {exc}")
                self._record_summary(
                    "errors",
                    f"I/O error torrent remove failed: {torrent.name}: {exc}",
                )
                next_state[torrent_key] = entry

        try:
            self._save_io_error_state(next_state)
        except OSError as exc:
            logger.error(f"Failed to persist Transmission I/O error cleanup state: {exc}")
            self._record_summary("errors", f"I/O error state persist failed: {exc}")

    def _sonarr_request(self, endpoint, method="GET", data=None):
        """Make a request to the Sonarr API."""
        return self.sonarr_client.request(endpoint, method=method, data=data)

    def _radarr_request(self, endpoint, method="GET", data=None):
        """Make a request to the Radarr API."""
        return self.radarr_client.request(endpoint, method=method, data=data)

    def _readarr_request(self, endpoint, method="GET", data=None):
        """Make a request to the Readarr API."""
        return self.readarr_client.request(endpoint, method=method, data=data)

    def _arr_request(self, service_name, session, url, endpoint, method="GET", data=None):
        """Arr request helper retained for callers that already built a URL."""
        client = ServarrClient(
            service_name,
            {"baseurl": "", "apikey": "provided"},
            session=session,
        )
        return client._request(session, url, endpoint, method=method, data=data)

    def get_watched_movies(self):
        """Get all watched movies from the configured media source."""
        if self.media_source == "emby":
            logger.info("Checking for watched movies in Emby")
            movies, _ = self._load_emby_watched_state()
            logger.info(f"Found {len(movies)} watched movies")
            return movies
        return self.plex_source.list_watched_movies()

    def _load_emby_watched_state(self):
        """Load and cache Jellyfin/Emby watched movies and episodes for this run."""
        if self._emby_watched_cache is not None:
            return self._emby_watched_cache
        if self.jellyfin is None:
            logger.error("Emby session is not initialized")
            self._emby_watched_cache = ([], [])
            return self._emby_watched_cache
        try:
            movies, episodes = self.jellyfin.load_watched_state()
            logger.info(
                "Loaded Emby watched state: %s movies, %s episodes",
                len(movies),
                len(episodes),
            )
            self._emby_watched_cache = (movies, episodes)
        except Exception as e:
            logger.error(f"Error loading Emby watched state: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            self._emby_watched_cache = ([], [])
        return self._emby_watched_cache

    def get_watched_episodes(self):
        """Get all watched episodes from the configured media source."""
        if self.media_source == "emby":
            logger.info("Checking for watched episodes in Emby")
            _, episodes = self._load_emby_watched_state()
            logger.info(f"Found {len(episodes)} watched episodes")
            return episodes
        return self.plex_source.list_watched_episodes()

    def _get_watch_status(self, media_item):
        return self.plex_source.watch_status(media_item)

    def get_sonarr_tags(self):
        """Get all tags from Sonarr. Returns None on error."""
        logger.debug("Getting Sonarr tags")
        if self._arr_cache["sonarr_tags"] is not None:
            return self._arr_cache["sonarr_tags"]

        tags = self._sonarr_request("tag")
        if tags is not None:
            self._arr_cache["sonarr_tags"] = tags
        return tags

    def get_radarr_tags(self):
        """Get all tags from Radarr. Returns None on error."""
        logger.debug("Getting Radarr tags")
        if self._arr_cache["radarr_tags"] is not None:
            return self._arr_cache["radarr_tags"]

        tags = self._radarr_request("tag")
        if tags is not None:
            self._arr_cache["radarr_tags"] = tags
        return tags

    def get_sonarr_series(self):
        """Get all series from Sonarr."""
        logger.debug("Getting Sonarr series")
        if self._arr_cache["sonarr_series"] is not None:
            return self._arr_cache["sonarr_series"]

        series = self._sonarr_request("series")
        if series is not None:
            self._arr_cache["sonarr_series"] = series or []
            return self._arr_cache["sonarr_series"]
        return []

    def get_radarr_movies(self):
        """Get all movies from Radarr."""
        logger.debug("Getting Radarr movies")
        if self._arr_cache["radarr_movies"] is not None:
            return self._arr_cache["radarr_movies"]

        movies = self._radarr_request("movie")
        if movies is not None:
            self._arr_cache["radarr_movies"] = movies or []
            return self._arr_cache["radarr_movies"]
        return []

    def get_sonarr_episodes_for_series(self, series_id):
        """Get Sonarr episodes for a series, cached per series ID."""
        cache = self._arr_cache["sonarr_episodes_by_series_id"]
        if series_id in cache:
            return cache[series_id]

        episodes = self._sonarr_request(f"episode?seriesId={series_id}") or []
        cache[series_id] = episodes
        return episodes

    def get_sonarr_episode(self, series_id, season_number, episode_number):
        """Get episode from Sonarr by series ID, season, and episode numbers."""
        logger.debug(f"Getting Sonarr episode: series={series_id}, S{season_number}E{episode_number}")
        episodes = self.get_sonarr_episodes_for_series(series_id)
        if episodes:
            for episode in episodes:
                try:
                    if int(episode["seasonNumber"]) == int(season_number) and int(episode["episodeNumber"]) == int(episode_number):
                        return episode
                except (ValueError, TypeError):
                    if episode["seasonNumber"] == season_number and episode["episodeNumber"] == episode_number:
                        return episode
        return None

    def _extract_radarr_movie_external_ids(self, movie):
        """Build a normalized external-id set for a Radarr movie."""
        if not isinstance(movie, dict):
            return set()
        ids = set()

        for key, prefix in (("imdbId", "imdb"), ("tmdbId", "tmdb"), ("tvdbId", "tvdb")):
            value = movie.get(key)
            if value:
                ids.add(f"{prefix}:{str(value).strip().lower()}")

        if not ids:
            # Plex sometimes references TVDB through external IDs in nested metadata.
            external_ids = movie.get("ids", {})
            if isinstance(external_ids, dict):
                for key, value in external_ids.items():
                    if value:
                        ids.add(f"{str(key).lower()}:{str(value).strip().lower()}")

        return ids

    def _extract_plex_movie_external_ids(self, movie):
        """Build a normalized external-id set for a Plex movie item."""
        if not isinstance(movie, dict):
            return set()
        ids = set()

        def add_guid_values(raw_value):
            for match in re.finditer(r"([a-z0-9]+)://([^/?]+)", str(raw_value or ""), re.IGNORECASE):
                source = match.group(1).lower()
                value = match.group(2).strip().lower()
                if value:
                    ids.add(f"{source}:{value}")

        add_guid_values(movie.get("guid"))
        for guid in movie.get("guids") or []:
            add_guid_values(guid)

        for key in ("imdbId", "tmdbId", "tvdbId", "tvmazeId"):
            value = movie.get(key)
            if value:
                ids.add(f"{key[:-2].lower()}:{str(value).strip().lower()}")

        return ids

    def _radarr_managed_movie_roots(self):
        """Return cached Radarr-managed movie paths for fast membership checks."""
        cached_roots = self._arr_cache["radarr_managed_movie_roots"]
        if cached_roots is not None:
            return cached_roots

        movies = self.get_radarr_movies()
        roots = []
        for movie in movies if isinstance(movies, list) else []:
            if not isinstance(movie, dict):
                continue
            for raw_path in (movie.get("path"), movie.get("rootFolderPath")):
                if not raw_path:
                    continue
                root = os.path.normpath(str(raw_path))
                if root and root not in roots:
                    roots.append(root)

        self._arr_cache["radarr_managed_movie_roots"] = roots
        return roots

    def _is_movie_file_in_radarr_path(self, file_path):
        """Return True when a movie file path is within cached Radarr-managed movie paths."""
        if not file_path:
            return False

        normalized_file = os.path.normcase(os.path.normpath(file_path))
        for root in self._radarr_managed_movie_roots():
            normalized_root = os.path.normcase(os.path.normpath(root))
            if not normalized_root:
                continue
            if normalized_file == normalized_root:
                return True
            prefix = normalized_root + os.sep
            if normalized_file.startswith(prefix):
                return True
        return False

    def match_episode_to_sonarr(self, episode):
        """Match a Plex episode to Sonarr."""
        logger.info(f"Matching episode to Sonarr: {episode['show_title']} S{episode['season']}E{episode['episode']}")
        series_list = self.get_sonarr_series()
        # Exact match first
        series = next((s for s in series_list if s["title"].lower() == episode["show_title"].lower()), None)
        if not series:
            # Log candidate titles for debugging
            try:
                candidate_titles = [s.get("title", "") for s in series_list]
                logger.debug(f"Sonarr series titles ({len(candidate_titles)}): {candidate_titles[:50]}")
            except Exception:
                logger.debug("Failed to enumerate Sonarr series for debug output")

            # Fallback: try normalized comparisons (strip punctuation, leading 'the', and parenthetical years)
            def normalize(t):
                t = (t or "").lower()
                # remove content in parentheses (years)
                t = re.sub(r"\(.*?\)", "", t)
                # remove leading 'the '
                t = re.sub(r"^the\s+", "", t)
                # remove non-alphanumeric
                t = re.sub(r"[^a-z0-9]", "", t)
                return t.strip()

            target_norm = normalize(episode["show_title"])
            # try exact normalized match
            series = next((s for s in series_list if normalize(s.get("title")) == target_norm), None)
            if series:
                logger.info(f"Matched by normalized title fallback: Sonarr='{series.get('title')}' for Plex='{episode['show_title']}'")
            else:
                # try containment both ways
                series = next((s for s in series_list if target_norm in normalize(s.get("title")) or normalize(s.get("title")) in target_norm), None)
                if series:
                    logger.info(f"Matched by normalized containment: Sonarr='{series.get('title')}' for Plex='{episode['show_title']}'")
                else:
                    logger.warning(f"Series not found in Sonarr after fallback: {episode['show_title']}")
                    return None
        sonarr_episode = self.get_sonarr_episode(series["id"], episode["season"], episode["episode"])
        if not sonarr_episode:
            logger.warning(f"Episode not found in Sonarr: {episode['show_title']} S{episode['season']}E{episode['episode']}")
            return None
        return {
            "series": series,
            "episode": sonarr_episode,
            "file_id": sonarr_episode.get("episodeFileId")
        }

    def _years_match(self, y1, y2):
        try:
            return y1 is not None and y2 is not None and int(y1) == int(y2)
        except (ValueError, TypeError):
            return y1 == y2

    def match_movie_to_radarr(self, movie, *, log_unmatched=True):
        """Match a Plex movie to Radarr with fuzzy matching."""
        logger.info(f"Matching movie to Radarr: {movie['title']} ({movie['year']})")
        movie_list = self.get_radarr_movies()

        # Helper function to normalize titles for fuzzy matching
        def normalize(t):
            t = (t or "").lower()
            # remove content in parentheses (years, subtitles, etc.)
            t = re.sub(r"\(.*?\)", "", t)
            # remove leading 'the '
            t = re.sub(r"^the\s+", "", t)
            # remove possessives like "'s"
            t = re.sub(r"'s\b", "", t)
            # remove non-alphanumeric
            t = re.sub(r"[^a-z0-9]", "", t)
            return t.strip()

        def tokenize(t):
            return {
                token
                for token in re.split(r"[^a-z0-9]+", (t or "").lower())
                if len(token) >= 3 and token not in {"the", "and"}
            }

        # Try exact match first (with year)
        exact_title_matches = [m for m in movie_list if m["title"].lower() == movie["title"].lower()]
        if len(exact_title_matches) == 1 and movie.get("year") is None:
            logger.info(f"Matched by title only (unique match): Radarr='{exact_title_matches[0]['title']}' for Plex='{movie['title']}'")
            return {
                "movie": exact_title_matches[0],
                "file_id": exact_title_matches[0].get("movieFile", {}).get("id")
            }

        for m in exact_title_matches:
            if self._years_match(m.get("year"), movie.get("year")):
                return {
                    "movie": m,
                    "file_id": m.get("movieFile", {}).get("id")
                }

        # Fallback: try normalized matching
        target_norm = normalize(movie["title"])
        norm_title_matches = [m for m in movie_list if normalize(m.get("title")) == target_norm]
        if len(norm_title_matches) == 1 and movie.get("year") is None:
            logger.info(f"Matched by normalized title only (unique match): Radarr='{norm_title_matches[0]['title']}' for Plex='{movie['title']}'")
            return {
                "movie": norm_title_matches[0],
                "file_id": norm_title_matches[0].get("movieFile", {}).get("id")
            }
            
        for m in norm_title_matches:
            if self._years_match(m.get("year"), movie.get("year")):
                logger.info(f"Matched by normalized title: Radarr='{m['title']}' for Plex='{movie['title']}'")
                return {
                    "movie": m,
                    "file_id": m.get("movieFile", {}).get("id")
                }

        # Final fallback: containment matching (with year)
        target_tokens = tokenize(movie["title"])
        plex_external_ids = self._extract_plex_movie_external_ids(movie)
        candidate_matches = []
        for m in movie_list:
            if self._years_match(m["year"], movie["year"]):
                candidate_tokens = tokenize(m["title"])
                if (
                    len(target_tokens) >= 2
                    and len(candidate_tokens) >= 2
                    and (
                        target_tokens.issubset(candidate_tokens)
                        or candidate_tokens.issubset(target_tokens)
                    )
                ):
                    candidate_matches.append(m)

        for candidate in candidate_matches:
            radarr_external_ids = self._extract_radarr_movie_external_ids(candidate)
            if plex_external_ids and radarr_external_ids and plex_external_ids.intersection(radarr_external_ids):
                logger.info(
                    f"Matched by token subset + external IDs: Radarr='{candidate['title']}' for Plex='{movie['title']}'"
                )
                return {
                    "movie": candidate,
                    "file_id": candidate.get("movieFile", {}).get("id")
                }

        if candidate_matches:
            logger.debug(
                "Skipping token-subset movie match because external IDs do not overlap: "
                f"Plex='{movie['title']}' ({movie['year']})"
            )

        if log_unmatched:
            logger.warning(f"Movie not found in Radarr: {movie['title']} ({movie['year']})")
        return None

    def get_user_tags(self, tags, tag_ids):
        """Get usernames from tag IDs, excluding 'safe' and 'kids'."""
        return user_tags_from_ids(tags, tag_ids)

    def should_delete_media(self, media_item, user_tags, watched_by):
        """Determine if media should be deleted based on watch status and household policy.

        Exact-episode / movie deletion accepts both Plex history and
        ``isWatched_fallback`` evidence. History is frequently empty for
        legitimate watches (local account mapping, clients that never
        scrobble), while ``isWatched`` / viewCount still reflect a real
        completion. Watched-ahead inference remains history-only and is
        handled separately in ``process_watched_episodes``.

        Multi-user / household policy (issue #20):
        - ``require_all_users`` (default): every household member must have watched
        - ``majority``: strict majority of household members must have watched
        - ``primary_user_only``: only ``CLEANARR_PRIMARY_USER`` must have watched

        Household members are resolved from ``CLEANARR_HOUSEHOLD_USERS``, then
        Sonarr/Radarr user tags, then every account present in ``watched_by``.
        One user's watched state must not delete content another household
        member still needs (e.g. mid-season progress).
        """
        logger.info("should_delete_media called")
        # Determine the media name for logging
        media_name = media_item.get('title', 'Unknown Media')
        if 'season' in media_item and 'episode' in media_item:
            media_name = f"{media_item['show_title']} S{media_item['season']}E{media_item['episode']}"

        # Log users who have watched the media
        logger.info(f"Watched by items: {watched_by.items()}")
        watched_users = [
            str(user).lower() for user, watched in (watched_by or {}).items() if watched
        ]
        watch_evidence = {
            str(user).lower(): evidence
            for user, evidence in (media_item.get("watch_evidence") or {}).items()
        }
        fallback_users = [
            user for user in watched_users if watch_evidence.get(user) == "isWatched_fallback"
        ]
        if watched_users:
            logger.info(f"Users who have watched {media_name}: {', '.join(watched_users)}")
            if fallback_users:
                logger.info(
                    f"Accepting isWatched_fallback evidence for exact-item delete on {media_name}: "
                    f"{fallback_users}"
                )
        else:
            logger.info(f"No users have watched {media_name} yet")
            return False

        policy = CONFIG.get("multi_user_delete_policy", "require_all_users")
        decision = decide_exact_item_delete(
            user_tags,
            watched_by,
            policy=policy,
            primary_user=CONFIG.get("primary_user") or "",
            household_users=CONFIG.get("household_users") or set(),
        )
        household = list(decision.household)
        detail = decision.detail
        if decision.allowed:
            logger.info(
                f"Household policy allows delete for {media_name}: "
                f"policy={policy} {detail} household={household}"
            )
            return True

        logger.info(
            f"Household policy blocks delete for {media_name}: "
            f"policy={policy} {detail} watched={watched_users} user_tags={user_tags}"
        )
        return False

    def _describe_episode(self, episode):
        return f"{episode['show_title']} S{episode['season']}E{episode['episode']}"

    def _delete_episode_and_cleanup(
        self,
        episode_label,
        reason,
        file_id,
        episode_id,
        file_path=None,
        rating_key=None,
        related_users=None,
        watched_by=None,
    ):
        logger.info(f"Deleting {episode_label} via {reason}")
        decision_details = {"episode_id": episode_id, "file_id": file_id}
        if related_users:
            decision_details["related_users"] = list(related_users)
        if watched_by is not None:
            decision_details["watched_by"] = watched_by
        if self.delete_sonarr_episode_file(file_id):
            if int(CONFIG.get("grace_days", 0) or 0) > 0:
                self.run_summary.setdefault("deleted_after_grace", []).append(episode_label)
            delete_reason = "dry-run" if CONFIG["dry_run"] else "delete"
            self._record_decision(
                reason_code=delete_reason,
                media_type="episode",
                media_title=episode_label,
                reason=reason,
                details=decision_details,
            )
            self._record_summary("tv_deletions", f"{episode_label} [{reason}]")
            self.unmonitor_sonarr_episode(episode_id)
            if file_path:
                self.remove_torrent_by_file_path(file_path)
            if rating_key:
                self.remove_from_plex_watchlist(rating_key)
            return True
        self._record_decision(
            reason_code="error",
            media_type="episode",
            media_title=episode_label,
            reason="delete_failed",
            details={**decision_details, "delete_reason": reason},
        )
        self._record_summary("errors", f"{episode_label} delete failed [{reason}]")
        return False

    def delete_sonarr_episode_file(self, file_id):
        """Delete an episode file from Sonarr."""
        if not file_id:
            logger.warning("No file ID provided for Sonarr episode deletion")
            return False
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would delete episode file from Sonarr: {file_id}")
            return True
        logger.info(f"Deleting episode file from Sonarr: {file_id}")
        result = self._sonarr_request(f"episodefile/{file_id}", method="DELETE")
        if result is None:
            return False
        logger.info(f"Successfully deleted episode file: {file_id}")
        return True

    def delete_radarr_movie_file(self, file_id):
        """Delete a movie file from Radarr."""
        if not file_id:
            logger.warning("No file ID provided for Radarr movie deletion")
            return False
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would delete movie file from Radarr: {file_id}")
            return True
        logger.info(f"Deleting movie file from Radarr: {file_id}")
        result = self._radarr_request(f"moviefile/{file_id}", method="DELETE")
        if result is None:
            return False
        logger.info(f"Successfully deleted movie file: {file_id}")
        return True

    def unmonitor_sonarr_episode(self, episode_id):
        """Unmonitor an episode in Sonarr."""
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would unmonitor episode {episode_id} in Sonarr")
            return True
        logger.info(f"Attempting to unmonitor episode {episode_id} in Sonarr")
        episode = self._sonarr_request(f"episode/{episode_id}")
        if episode:
            episode["monitored"] = False
            result = self._sonarr_request(f"episode/{episode_id}", method="PUT", data=episode)
            if result is not None:
                logger.info(f"Unmonitored episode {episode_id}")
                return True
            else:
                logger.error(f"Failed to unmonitor episode {episode_id}")
                return False
        else:
            logger.error(f"Failed to retrieve episode {episode_id} for unmonitoring")
            return False

    def unmonitor_radarr_movie(self, movie_id):
        """Unmonitor a movie in Radarr."""
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would unmonitor movie {movie_id} in Radarr")
            return True
        logger.info(f"Attempting to unmonitor movie {movie_id} in Radarr")
        movie = self._radarr_request(f"movie/{movie_id}")
        if movie:
            movie["monitored"] = False
            result = self._radarr_request("movie", method="PUT", data=movie)
            if result is not None:
                logger.info(f"Unmonitored movie {movie_id}")
                return True
            else:
                logger.error(f"Failed to unmonitor movie {movie_id}")
                return False
        else:
            logger.error(f"Failed to retrieve movie {movie_id} for unmonitoring")
            return False

    def delete_sonarr_series(self, series_id):
        """Delete an entire series from Sonarr."""
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would delete series {series_id} from Sonarr")
            return True
        logger.info(f"Deleting series {series_id} from Sonarr")
        result = self._sonarr_request(f"series/{series_id}?deleteFiles=true&addImportListExclusion=false", method="DELETE")
        if result is None:
            return False
        logger.info(f"Successfully deleted series: {series_id}")
        return True

    def delete_radarr_movie(self, movie_id):
        """Delete an entire movie from Radarr."""
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would delete movie {movie_id} from Radarr")
            return True
        logger.info(f"Deleting movie {movie_id} from Radarr")
        result = self._radarr_request(f"movie/{movie_id}?deleteFiles=true&addImportListExclusion=false", method="DELETE")
        if result is None:
            return False
        logger.info(f"Successfully deleted movie: {movie_id}")
        return True

    def remove_torrent_by_file_path(self, file_path):
        """Remove a torrent from Transmission based on its file path."""
        if not file_path:
            logger.warning("No file path provided for Transmission torrent removal")
            return False
        if CONFIG.get("disable_torrent_cleanup", False) or not self.transmission:
            logger.info("Torrent cleanup is disabled; skipping Transmission torrent removal")
            return False
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would remove torrent with file: {file_path}")
            return True
        logger.info(f"Searching for torrent with file: {file_path}")
        try:
            torrents = self.transmission_io.get_torrents()
            for torrent in torrents:
                for tf in torrent.files():
                    norm_tf_path = os.path.normpath(tf["name"])
                    norm_file_path = os.path.normpath(file_path)
                    if norm_file_path.endswith(norm_tf_path) or norm_tf_path.endswith(norm_file_path):
                        logger.info(f"Found matching torrent: {torrent.name}")
                        if not self._torrent_cleanup_allowed(torrent, "file-path cleanup"):
                            continue
                        
                        # Check if torrent is actively downloading before removal
                        if torrent.rate_download > 0:
                            logger.info(f"Skipping removal of torrent {torrent.name} - actively downloading ({torrent.rate_download} B/s)")
                            return False
                        
                        self.transmission_io.remove_torrent(torrent.id, delete_data=True)
                        logger.info(f"Successfully removed torrent: {torrent.name}")
                        return True
            logger.warning(f"No matching torrent found for file: {file_path}")
            return False
        except Exception as e:
            logger.error(f"Error removing torrent by file path {file_path}: {str(e)}")
            self._record_summary("errors", f"torrent remove by path failed: {file_path}: {e}")
            return False

    def clean_failed_downloads(self):
        """Clean up failed downloads and orphaned incomplete files in Transmission."""
        if CONFIG.get("disable_torrent_cleanup", False) or not self.transmission:
            logger.info("Torrent cleanup is disabled via CLEANARR_DISABLE_TORRENT_CLEANUP")
            return

        remove_failed_downloads = CONFIG.get("remove_failed_downloads", False)
        remove_orphan_incomplete_downloads = CONFIG.get("remove_orphan_incomplete_downloads", False)

        if not remove_failed_downloads and not remove_orphan_incomplete_downloads:
            logger.info("Failed download and orphan incomplete cleanup are disabled")
            return

        logger.info("Checking for failed downloads and orphaned incomplete files")
        try:
            # 1. Clean errored torrents
            torrents = self.transmission_io.get_torrents()
            removed_torrent_ids = set()
            if remove_failed_downloads:
                for torrent in torrents:
                    if torrent.error != 0:
                        if not self._torrent_cleanup_allowed(torrent, "failed-download cleanup"):
                            continue
                        logger.info(f"Found errored torrent: {torrent.name} (ID: {torrent.id}, Error: {torrent.error_string})")
                        if CONFIG["dry_run"]:
                            logger.info(f"[DRY RUN] Would remove errored torrent: {torrent.name}")
                        else:
                            logger.info(f"Removing errored torrent: {torrent.name}")
                            try:
                                self.transmission_io.remove_torrent(torrent.id, delete_data=True)
                                removed_torrent_ids.add(torrent.id)
                            except Exception as e:
                                logger.error(f"Failed to remove errored torrent {torrent.name}: {e}")
                                self._record_summary(
                                    "errors",
                                    f"errored torrent remove failed: {torrent.name}: {e}",
                                )
            else:
                logger.info("Failed download cleanup is disabled")

            # 2. Clean orphaned files in incomplete directory
            if not remove_orphan_incomplete_downloads:
                logger.info("Orphan incomplete cleanup is disabled")
                return

            session = self.transmission_io.get_session()
            if not session.incomplete_dir_enabled:
                logger.info("Incomplete download directory is disabled in Transmission, skipping orphan cleanup")
                return

            incomplete_dir = session.incomplete_dir
            if not os.path.exists(incomplete_dir):
                logger.warning(f"Incomplete directory does not exist: {incomplete_dir}")
                return

            # Refresh torrents after removal (or filter the list)
            active_torrents = [t for t in torrents if t.id not in removed_torrent_ids]

            # Build a set of expected file/directory names in the incomplete directory
            expected_names = set()
            for torrent in active_torrents:
                expected_names.update(_iter_expected_incomplete_names(torrent))

            logger.debug(f"Expected files in incomplete dir (from active torrents): {expected_names}")

            expected_normalized = {_normalize_incomplete_name(name) for name in expected_names}

            # List actual files/directories in incomplete_dir
            try:
                actual_files = os.listdir(incomplete_dir)
            except OSError as e:
                logger.error(f"Failed to list incomplete directory {incomplete_dir}: {e}")
                self._record_summary("errors", f"incomplete dir list failed: {incomplete_dir}: {e}")
                return

            for filename in actual_files:
                file_path = os.path.join(incomplete_dir, filename)

                # Check if file is an orphan
                is_orphan = True

                # 1. Exact match
                if filename in expected_names:
                    is_orphan = False

                # 2. Check normalized match (handles .part and sanitization)
                elif _normalize_incomplete_name(filename) in expected_normalized:
                    is_orphan = False

                if is_orphan:
                    if CONFIG["dry_run"]:
                        logger.info(f"[DRY RUN] Would delete orphaned file/directory in incomplete dir: {filename}")
                    else:
                        logger.info(f"Deleting orphaned file/directory in incomplete dir: {filename}")
                        try:
                            if os.path.isdir(file_path):
                                shutil.rmtree(file_path)
                            else:
                                os.remove(file_path)
                            logger.info(f"Successfully deleted orphan: {filename}")
                        except Exception as e:
                            logger.error(f"Failed to delete orphan {filename}: {e}")
                            self._record_summary("errors", f"orphan delete failed: {filename}: {e}")
                else:
                    logger.debug(f"Keeping file {filename} (belongs to active torrent)")

        except Exception as e:
            logger.error(f"Error cleaning failed downloads: {e}")
            self._record_summary("errors", f"failed download cleanup error: {e}")

    def clean_sonarr_executable_downloads(self):
        """Blocklist a completed, Sonarr-owned torrent containing only one .exe.

        Sonarr leaves these as import warnings rather than failed downloads. Its
        queue DELETE action removes the client payload and records the blocklist
        entry together, so never remove the torrent directly from Transmission.
        """
        if not CONFIG.get("remove_sonarr_executable_downloads", False):
            return
        if CONFIG.get("disable_torrent_cleanup", False) or not self.transmission:
            logger.info("Sonarr executable cleanup skipped: torrent cleanup is disabled")
            return

        queue = self._sonarr_request(
            "queue?page=1&pageSize=1000&includeUnknownSeriesItems=true"
        )
        if not isinstance(queue, dict) or not isinstance(queue.get("records"), list):
            self._record_summary("errors", "Sonarr executable cleanup: queue unavailable")
            return
        records = queue["records"]
        if queue.get("totalRecords") != len(records):
            self._record_summary("errors", "Sonarr executable cleanup: queue is incomplete")
            return

        try:
            torrents = self.transmission_io.get_torrents()
        except Exception as exc:
            self._record_summary("errors", f"Sonarr executable cleanup: Transmission unavailable: {exc}")
            return
        by_hash = {}
        for torrent in torrents:
            hash_string = str(getattr(torrent, "hash_string", "") or "").casefold()
            if hash_string:
                by_hash.setdefault(hash_string, []).append(torrent)

        for record in records:
            if record.get("trackedDownloadStatus") != "warning":
                continue
            messages = [
                message
                for group in (record.get("statusMessages") or [])
                for message in (group.get("messages") or [])
            ]
            if "Caution: Found executable file with extension: '.exe'" not in messages:
                continue

            queue_id = record.get("id")
            download_id = str(record.get("downloadId") or "")
            output_path = record.get("outputPath")
            if (not isinstance(queue_id, int) or queue_id < 1
                    or not re.fullmatch(r"[0-9a-fA-F]{40}", download_id)
                    or not isinstance(output_path, str)
                    or os.path.splitext(output_path)[1].casefold() != ".exe"):
                logger.warning("Sonarr executable warning lacks a safe queue identity; skipping")
                continue
            matching = by_hash.get(download_id.casefold(), [])
            if len(matching) != 1:
                logger.warning("Sonarr executable warning has no unique Transmission torrent; skipping")
                continue
            torrent = matching[0]
            if not self._torrent_cleanup_allowed(torrent, "Sonarr executable cleanup"):
                continue
            if "sonarr" not in {str(label).casefold() for label in (getattr(torrent, "labels", None) or [])}:
                continue
            if getattr(torrent, "percent_done", 0) < 1:
                continue
            download_dir = getattr(torrent, "download_dir", "")
            if download_dir != "/media/downloads/sonarr":
                continue
            try:
                files = torrent.get_files()
            except Exception as exc:
                logger.warning(f"Cannot inspect Sonarr executable torrent files: {exc}")
                continue
            if len(files) != 1:
                continue
            file = files[0]
            relative = file.name
            if (not isinstance(relative, str) or not relative
                    or relative.startswith("/") or ".." in Path(relative).parts
                    or os.path.splitext(relative)[1].casefold() != ".exe"
                    or file.completed != file.size
                    or os.path.normpath(os.path.join(download_dir, relative)) != output_path):
                continue

            if CONFIG["dry_run"]:
                logger.info(f"[DRY RUN] Would Sonarr-blocklist executable-only torrent {download_id}")
                continue
            result = self._sonarr_request(
                f"queue/{queue_id}?removeFromClient=true&blocklist=true&skipRedownload=true",
                method="DELETE",
            )
            if result is None:
                self._record_summary("errors", f"Sonarr executable blocklist failed for queue {queue_id}")
            else:
                logger.info(f"Sonarr blocklisted executable-only torrent {download_id}")

    def _arr_import_history_contains(self, app, title, added_date, download_id=None):
        """Check an Arr import history for one release title, failing closed."""
        caches = getattr(self, "_stale_torrent_import_history", None)
        if caches is None:
            caches = self._stale_torrent_import_history = {}
        state = caches.setdefault(app, {
            "titles": set(),
            "download_ids": set(),
            "page": 1,
            "oldest_date": None,
            "total_records": None,
            "exhausted": False,
            "unavailable": False,
        })
        normalized_title = str(title or "").strip().casefold()
        normalized_id = str(download_id or "").strip().casefold()
        if normalized_title in state["titles"] or normalized_id in state["download_ids"]:
            return True
        if state["unavailable"]:
            return None

        requesters = {
            "sonarr": self._sonarr_request,
            "radarr": self._radarr_request,
            "lidarr": self._lidarr_request,
            "readarr": self._readarr_request,
        }
        request = requesters.get(app)
        if request is None:
            return None

        page_size = 100
        oldest_relevant_date = added_date - timedelta(days=1)
        while state["page"] <= 100 and not state["exhausted"]:
            if state["oldest_date"] and state["oldest_date"] < oldest_relevant_date:
                return False
            page = state["page"]
            response = request(
                f"history?eventType=3&page={page}&pageSize={page_size}"
                "&sortKey=date&sortDirection=descending"
            )
            if not isinstance(response, dict) or not isinstance(response.get("records"), list):
                logger.warning(f"{app.title()} import history unavailable; stale torrent cleanup will skip this app")
                state["unavailable"] = True
                return None

            records = response["records"]
            for event in records:
                event_type = event.get("eventType")
                event_type_name = str(event_type or "").replace("_", "").casefold()
                # Arr API event type 3 is DownloadFolderImported. Querying it
                # server-side keeps large histories bounded and excludes
                # DownloadIgnored (event type 7).
                if event_type != 3 and event_type_name not in (
                    "downloadfolderimported",
                    "downloadimported",
                    "moviefolderimported",
                    "trackfileimported",
                ):
                    continue

                event_id = event.get("downloadId")
                if isinstance(event_id, str) and event_id.strip():
                    state["download_ids"].add(event_id.strip().casefold())
                source_title = event.get("sourceTitle")
                event_data = event.get("data")
                if isinstance(event_data, str):
                    try:
                        event_data = json.loads(event_data)
                    except (TypeError, ValueError):
                        event_data = None
                if not source_title and isinstance(event_data, dict):
                    source_title = event_data.get("sourceTitle")
                if isinstance(source_title, str) and source_title.strip():
                    state["titles"].add(source_title.strip().casefold())

                event_date = event.get("date")
                if isinstance(event_date, str):
                    try:
                        parsed_date = datetime.fromisoformat(event_date.replace("Z", "+00:00"))
                        if parsed_date.tzinfo is None:
                            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                        parsed_date = parsed_date.astimezone(timezone.utc)
                        if state["oldest_date"] is None or parsed_date < state["oldest_date"]:
                            state["oldest_date"] = parsed_date
                    except ValueError:
                        pass

            total_records = response.get("totalRecords")
            state["total_records"] = total_records
            if not records or len(records) < page_size:
                state["exhausted"] = True
            elif isinstance(total_records, int) and page * page_size >= total_records:
                state["exhausted"] = True
            else:
                state["page"] += 1

            if normalized_title in state["titles"] or normalized_id in state["download_ids"]:
                return True
            if state["oldest_date"] and state["oldest_date"] < oldest_relevant_date:
                return False

        return False

    def _stale_torrent_has_arr_import(self, torrent, added_date):
        """Check the import history for the Arr identified by the torrent label."""
        labels = {
            str(label).strip().casefold()
            for label in (getattr(torrent, "labels", None) or [])
            if str(label).strip()
        }
        category = _get_torrent_category(torrent)
        category_name = str(category).strip().casefold() if category else None
        torrent_title = str(getattr(torrent, "name", "") or "").strip().casefold()
        for app in ("sonarr", "radarr", "lidarr", "readarr"):
            # Readarr commonly identifies downloads by directory category rather
            # than a Transmission label. Sonarr, Radarr, and Lidarr match strictly
            # on Transmission labels.
            has_app_label = app in labels or (app == "readarr" and category_name == "readarr")
            if not has_app_label:
                continue
            imported = self._arr_import_history_contains(
                app, torrent_title, added_date, getattr(torrent, "hash_string", None)
            )
            if imported is True:
                return True
        return False

    def remove_stale_torrents(self):
        """Remove stale torrents from Transmission."""
        if CONFIG.get("disable_torrent_cleanup", False) or not self.transmission:
            logger.info("Torrent cleanup is disabled via CLEANARR_DISABLE_TORRENT_CLEANUP")
            return
        if not CONFIG.get("remove_stale_torrents", True):
            logger.info("Stale torrent cleanup is disabled")
            return
        logger.info("Checking Transmission for stale torrents")
        try:
            torrents = self.transmission_io.get_torrents()
            current_time = datetime.now(timezone.utc)  # Use UTC for consistency
            stale_threshold = timedelta(hours=CONFIG.get("stale_torrent_hours", 8))

            for torrent in torrents:
                try:
                    if not self._torrent_cleanup_allowed(torrent, "stale-torrent cleanup"):
                        continue
                    # Convert added_date to UTC-aware datetime
                    if isinstance(torrent.added_date, int):
                        # Unix timestamp to UTC datetime
                        added_date = datetime.fromtimestamp(torrent.added_date, timezone.utc)
                    elif isinstance(torrent.added_date, str):
                        # Parse string and ensure UTC
                        added_date = datetime.fromisoformat(torrent.added_date)
                        if added_date.tzinfo is None:
                            added_date = added_date.replace(tzinfo=timezone.utc)
                        else:
                            added_date = added_date.astimezone(timezone.utc)
                    elif isinstance(torrent.added_date, datetime):
                        # Ensure datetime is UTC-aware
                        added_date = torrent.added_date
                        if added_date.tzinfo is None:
                            added_date = added_date.replace(tzinfo=timezone.utc)
                        else:
                            added_date = added_date.astimezone(timezone.utc)
                    else:
                        logger.warning(f"Skipping torrent {torrent.name}: Unknown added_date type {type(torrent.added_date)}")
                        continue

                    # Calculate age
                    age = current_time - added_date

                    # Check if torrent is complete
                    is_complete = torrent.percent_done >= 1.0
                    
                    # Check if torrent is actively downloading (rate > 0 kB/s)
                    is_downloading = torrent.rate_download > 0
                    
                    # Check if torrent is queued to start downloading
                    # Status values: 0=stopped, 1=check pending, 2=checking, 3=download pending, 4=downloading, 5=seed pending, 6=seeding
                    is_queued = torrent.status in [1, 2, 3]  # check pending, checking, download pending
                    is_active_status = torrent.status in [4, 5, 6]  # downloading, seed pending, seeding

                    # Log torrent details for debugging
                    logger.debug(f"Torrent {torrent.name}: Age = {age}, Percent Done = {torrent.percent_done * 100:.1f}%, Download Rate = {torrent.rate_download} B/s, Status = {torrent.status}")

                    if CONFIG.get("stale_torrent_require_import", False):
                        # In import-verified mode, do not infer that old or idle
                        # means disposable. Require a complete, stopped torrent
                        # and an exact release-title match in the owning Arr's
                        # import history before removing its Transmission record.
                        if not is_complete or torrent.status not in (0, "stopped") or age <= stale_threshold:
                            logger.debug(
                                f"Keeping torrent {torrent.name}: import-verified cleanup requires "
                                "a completed, stopped torrent older than the stale threshold"
                            )
                            continue
                        if not self._stale_torrent_has_arr_import(torrent, added_date):
                            logger.info(
                                f"Keeping stale torrent {torrent.name}: no matching Arr import history"
                            )
                            continue
                        if CONFIG["dry_run"]:
                            logger.info(
                                f"[DRY RUN] Would remove imported stale torrent record (keep data): "
                                f"{torrent.name} (ID: {torrent.id})"
                            )
                            continue
                        logger.info(
                            f"Removing imported stale torrent record: {torrent.name} "
                            f"(ID: {torrent.id}, Age: {age})"
                        )
                        self.transmission_io.remove_torrent(torrent.id, delete_data=False)
                        continue

                    # Skip torrents that are actively downloading or queued
                    if is_downloading:
                        logger.debug(f"Torrent {torrent.name} is actively downloading ({torrent.rate_download} B/s), skipping removal")
                        continue
                    
                    if is_queued:
                        logger.debug(f"Torrent {torrent.name} is queued to start (status: {torrent.status}), skipping removal")
                        continue

                    # Remove only if incomplete and older than 8 hours
                    if not is_complete and age > stale_threshold:
                        reason = "incomplete and older than 8 hours"
                        if CONFIG["dry_run"]:
                            logger.info(f"[DRY RUN] Would remove stale torrent: {torrent.name} (ID: {torrent.id})")
                            continue
                        logger.info(f"Removing stale torrent: {torrent.name} (ID: {torrent.id}, Reason: {reason}, Age: {age})")
                        self.transmission_io.remove_torrent(torrent.id, delete_data=True)
                    elif torrent.peers_connected == 0 or (is_complete and age > stale_threshold):
                        # Additional check: don't remove if torrent has active status (downloading/seeding)
                        if is_active_status:
                            logger.debug(f"Torrent {torrent.name} has active status ({torrent.status}), skipping removal despite no peers")
                            continue
                            
                        reason = "no peers connected or completed and older than 8 hours"
                        if CONFIG["dry_run"]:
                            logger.info(f"[DRY RUN] Would remove stale torrent: {torrent.name} (ID: {torrent.id})")
                            continue
                        logger.info(f"Removing stale torrent: {torrent.name} (ID: {torrent.id}, Reason: {reason}, Age: {age})")
                        self.transmission_io.remove_torrent(torrent.id, delete_data=True)
                    else:
                        logger.debug(f"Torrent {torrent.name} not stale (Age: {age}, Complete: {is_complete} | {torrent.percent_done}, Peers: {torrent.peers_connected}, Status: {torrent.status})")
                except Exception as e:
                    logger.error(f"Error processing torrent {torrent.name}: {str(e)}")
                    self._record_summary(
                        "errors",
                        f"stale torrent processing failed: {getattr(torrent, 'name', 'unknown')}: {e}",
                    )
                    continue
        except Exception as e:
            logger.error(f"Error in stale torrent check: {str(e)}")
            self._record_summary("errors", f"stale torrent check error: {e}")

    def remove_from_plex_watchlist(self, rating_key):
        """Remove an item from the Plex Watchlist for the owner and managed users."""
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would remove item {rating_key} from Plex Watchlist")
            return
        self.plex_source.remove_from_watchlist(rating_key)

    def process_watched_episodes(self):
        """Process all watched episodes."""
        logger.info("Processing watched episodes")
        watched_episodes = self.get_watched_episodes()
        sonarr_tags = self.get_sonarr_tags()
        if sonarr_tags is None:
            logger.error("Failed to fetch Sonarr tags; aborting watched episode processing to prevent accidental deletions.")
            self._record_summary(
                "errors",
                "Failed to fetch Sonarr tags; aborted watched episode processing",
            )
            return

        # --- Watched ahead logic ---
        # Build a mapping: show -> user -> set of watched (season, episode)
        from collections import defaultdict
        show_user_watched = defaultdict(lambda: defaultdict(set))
        for ep in watched_episodes:
            for user, watched in ep["watched_by"].items():
                evidence = ep.get("watch_evidence", {}).get(user)
                if watched and evidence == "history":
                    show_user_watched[ep["show_title"]][user].add((ep["season"], ep["episode"]))

        # Build a mapping: show -> season -> episode -> episode dict
        show_season_ep_map = defaultdict(lambda: defaultdict(dict))
        for ep in watched_episodes:
            show_season_ep_map[ep["show_title"]][ep["season"]][ep["episode"]] = ep

        # For each show/season, get all episodes (including unwatched)
        # We'll use Sonarr API to get all episodes for each series
        sonarr_series_list = self.get_sonarr_series()
        protected_tag_ids = protected_ids_from_tags(sonarr_tags)

        for series in sonarr_series_list:
            show_title = series["title"]
            episodes = self.get_sonarr_episodes_for_series(series["id"]) or []
            # Build a mapping: (season, episode) -> episode dict
            all_eps = {(ep["seasonNumber"], ep["episodeNumber"]): ep for ep in episodes}
            series_tag_ids = set(series.get("tags") or [])
            series_user_tags = self.get_user_tags(sonarr_tags, series_tag_ids)

            # Aggregate watched-ahead candidates per episode so one household
            # member finishing a season cannot delete while another is mid-season.
            # key -> {users_ahead, later_by_user, ep_obj}
            ahead_candidates = collect_watch_ahead_candidates(
                show_user_watched[show_title],
                all_eps.keys(),
            )
            for key, candidate in ahead_candidates.items():
                candidate["ep_obj"] = all_eps[key]

            # Household for this show: configured users, series tags, else all
            # accounts with any real history on the show (plus any Plex accounts
            # observed on watched episodes of the show).
            show_watchers = {}
            for ep in watched_episodes:
                if ep.get("show_title") != show_title:
                    continue
                for user_name, watched in (ep.get("watched_by") or {}).items():
                    show_watchers[str(user_name).lower()] = True
            for user_name in show_user_watched[show_title]:
                show_watchers[str(user_name).lower()] = True
            household = _resolve_household_members(series_user_tags, show_watchers)

            for (season, epnum), candidate in sorted(ahead_candidates.items()):
                ep_obj = candidate["ep_obj"]
                users_ahead = sorted(candidate["users_ahead"])
                later_by_user = candidate["later_by_user"]
                episode_label = f"{show_title} S{season}E{epnum}"
                file_id = ep_obj.get("episodeFileId")
                if not file_id:
                    self._record_decision(
                        reason_code="unmatched",
                        media_type="episode",
                        media_title=episode_label,
                        reason="watch_ahead_no_sonarr_file",
                        details={
                            "series": show_title,
                            "season": season,
                            "episode": epnum,
                            "users_ahead": users_ahead,
                        },
                    )
                    continue

                plex_ep = show_season_ep_map[show_title][season].get(epnum)
                file_path = plex_ep["file"] if plex_ep else None
                episode_tag_ids = set(ep_obj.get("tags") or [])
                if protected_tag_ids & (series_tag_ids | episode_tag_ids):
                    protected_labels = self._protected_labels_for_ids(
                        sonarr_tags,
                        series_tag_ids | episode_tag_ids,
                    )
                    logger.info(
                        f"[Watched Ahead] Skipping deletion for {episode_label} "
                        f"due to 'safe' or 'kids' tag (users ahead={users_ahead})"
                    )
                    self._record_summary(
                        "protected_skips",
                        f"{episode_label} [watched-ahead by {users_ahead}]",
                    )
                    self._record_decision(
                        reason_code="protected",
                        media_type="episode",
                        media_title=episode_label,
                        reason="protected_series_or_episode_tags",
                        details={
                            "series": show_title,
                            "users_ahead": users_ahead,
                            "later_by_user": later_by_user,
                            "protected_labels": protected_labels,
                        },
                    )
                    continue

                # Per-episode tags can further restrict the household.
                episode_user_tags = self.get_user_tags(
                    sonarr_tags, series_tag_ids | episode_tag_ids
                )
                episode_household = _resolve_household_members(
                    episode_user_tags or series_user_tags, show_watchers
                ) or household

                policy = CONFIG.get("multi_user_delete_policy", "require_all_users")
                allowed, detail = household_policy_allows_delete(
                    episode_household,
                    users_ahead,
                    policy=policy,
                    primary_user=CONFIG.get("primary_user"),
                )
                if not allowed:
                    logger.info(
                        f"[Watched Ahead] Household policy blocks delete for {episode_label}: "
                        f"policy={policy} {detail} users_ahead={users_ahead}"
                    )
                    self._record_decision(
                        reason_code="skip",
                        media_type="episode",
                        media_title=episode_label,
                        reason="household_policy_blocks_watch_ahead",
                        details={
                            "policy": policy,
                            "detail": detail,
                            "household": episode_household,
                            "users_ahead": users_ahead,
                            "later_by_user": later_by_user,
                            "skip_category": "policy",
                        },
                    )
                    continue

                logger.info(
                    f"[Watched Ahead] Household policy allows delete for {episode_label}: "
                    f"policy={policy} {detail} users_ahead={users_ahead} later={later_by_user}. "
                    f"File: {file_path}"
                )
                eligible_item = plex_ep or {"guid": ep_obj.get("guid")}
                grace_key = self.leaving_soon_key(eligible_item)
                if not self.leaving_soon_allows_delete(
                    grace_key, episode_label, eligible_item.get("rating_key")
                ):
                    continue
                self._delete_episode_and_cleanup(
                    episode_label,
                    f"watched-ahead users={users_ahead} later={later_by_user} policy={policy}",
                    file_id,
                    ep_obj["id"],
                    file_path=file_path,
                    rating_key=plex_ep.get("rating_key") if plex_ep else None,
                    related_users=users_ahead,
                )


        # --- Standard logic for watched episodes ---
        for episode in watched_episodes:
            if not episode["file"]:
                logger.warning(f"No file path for episode: {episode['show_title']} S{episode['season']}E{episode['episode']}")
                self._record_decision(
                    reason_code="skip",
                    media_type="episode",
                    media_title=self._describe_episode(episode),
                    reason="missing_file_path",
                    details={"rating_key": episode.get("rating_key")},
                )
                continue
            sonarr_match = self.match_episode_to_sonarr(episode)
            if not sonarr_match:
                logger.warning(f"Skipping deletion for {episode['show_title']} S{episode['season']}E{episode['episode']} because no Sonarr match was found.")
                self._record_decision(
                    reason_code="unmatched",
                    media_type="episode",
                    media_title=self._describe_episode(episode),
                    reason="no_sonarr_match",
                    details={"rating_key": episode.get("rating_key")},
                )
                continue
            episode_label = self._describe_episode(episode)
            series_tags = sonarr_match["series"].get("tags", [])
            episode_tags = sonarr_match["episode"].get("tags") or []
            series_tag_ids = set(series_tags or [])
            episode_tag_ids = set(episode_tags)
            if protected_tag_ids & (series_tag_ids | episode_tag_ids):
                protected_labels = self._protected_labels_for_ids(
                    sonarr_tags,
                    series_tag_ids | episode_tag_ids,
                )
                logger.info(
                    f"Skipping deletion for {episode_label} "
                    "due to 'safe' or 'kids' tag"
                )
                self._record_summary("protected_skips", f"{episode_label} [protected tag]")
                self._record_decision(
                    reason_code="protected",
                    media_type="episode",
                    media_title=episode_label,
                    reason="protected_series_or_episode_tags",
                    details={
                        "series_tags": list(series_tag_ids),
                        "episode_tags": list(episode_tag_ids),
                        "protected_labels": protected_labels,
                        "watched_by": episode.get("watched_by"),
                    },
                )
                continue
            # Check if episode has been watched by any user, or if it's marked as watched at the episode level
            # (handles cases where server marks episode as watched but individual user histories are missing)
            has_been_watched = any(episode["watched_by"].values()) or episode.get("is_watched_override", False)
            
            if not has_been_watched:
                logger.info(f"Skipping deletion for {episode_label} because no users have watched it.")
                self._record_decision(
                    reason_code="skip",
                    media_type="episode",
                    media_title=episode_label,
                    reason="not_watched",
                    details={
                        "watched_by": episode["watched_by"],
                        "skip_category": "policy",
                    },
                )
                continue

            user_tags = self.get_user_tags(sonarr_tags, series_tag_ids | episode_tag_ids)
            watch_evidence = episode.get("watch_evidence", {})
            if any(source == "isWatched_fallback" for source in watch_evidence.values()):
                logger.info(
                    f"{episode_label} includes isWatched fallback evidence for exact-episode evaluation: {watch_evidence}"
                )
            if not self.should_delete_media(episode, user_tags, episode["watched_by"]):
                watched_users = [
                    user for user, watched in episode["watched_by"].items() if watched
                ]
                if not watched_users:
                    logger.info(
                        f"Skipping deletion for {episode_label} because no users have watched it."
                    )
                    skip_reason = "not_watched"
                else:
                    policy = CONFIG.get("multi_user_delete_policy", "require_all_users")
                    logger.info(
                        f"Skipping deletion for {episode_label} because household policy "
                        f"'{policy}' is not satisfied (user_tags={user_tags}, "
                        f"watched_by={episode['watched_by']}, evidence={watch_evidence})."
                    )
                    skip_reason = "household_policy_not_satisfied"
                self._record_decision(
                    reason_code="skip",
                    media_type="episode",
                    media_title=episode_label,
                    reason=skip_reason,
                    details={
                        "user_tags": user_tags,
                        "watched_by": episode["watched_by"],
                        "watch_evidence": watch_evidence,
                        "skip_category": "policy",
                    },
                )
                continue

            grace_key = self.leaving_soon_key(episode)
            if not self.leaving_soon_allows_delete(
                grace_key, episode_label, episode.get("rating_key")
            ):
                continue

            self._delete_episode_and_cleanup(
                episode_label,
                "standard watched",
                sonarr_match["file_id"],
                sonarr_match["episode"]["id"],
                file_path=episode["file"],
                rating_key=episode.get("rating_key"),
                watched_by=episode.get("watched_by"),
                related_users=user_tags or None,
            )

    def process_watched_movies(self):
        """Process all watched movies."""
        logger.info("Processing watched movies")
        watched_movies = self.get_watched_movies()
        radarr_tags = self.get_radarr_tags()
        if radarr_tags is None:
            logger.error("Failed to fetch Radarr tags; aborting watched movie processing to prevent accidental deletions.")
            self._record_summary(
                "errors",
                "Failed to fetch Radarr tags; aborted watched movie processing",
            )
            return
        radarr_managed_roots = self._radarr_managed_movie_roots()
        
        for movie in watched_movies:
            if not movie["file"]:
                logger.warning(f"No file path for movie: {movie['title']} ({movie['year']})")
                self._record_decision(
                    reason_code="skip",
                    media_type="movie",
                    media_title=f"{movie['title']} ({movie['year']})",
                    reason="missing_file_path",
                    details={"rating_key": movie.get("rating_key")},
                )
                continue
            radarr_match = self.match_movie_to_radarr(movie, log_unmatched=False)
            if not radarr_match:
                if radarr_managed_roots and not self._is_movie_file_in_radarr_path(movie["file"]):
                    logger.info(
                        f"Skipping watched movie outside Radarr-managed paths: {movie['title']} ({movie['year']}) "
                        f"path={movie['file']}"
                    )
                    logger.debug(
                        "Movie was not matched in Radarr and was outside cached managed paths: "
                        f"{movie['file']} not in {radarr_managed_roots}"
                    )
                    self._record_decision(
                        reason_code="skip",
                        media_type="movie",
                        media_title=f"{movie['title']} ({movie['year']})",
                        reason="outside_managed_paths",
                        details={"path": movie["file"]},
                    )
                else:
                    logger.warning(f"Movie not found in Radarr: {movie['title']} ({movie['year']})")
                    self._record_decision(
                        reason_code="unmatched",
                        media_type="movie",
                        media_title=f"{movie['title']} ({movie['year']})",
                        reason="no_radarr_match",
                        details={"watching_guid": movie.get("guid")},
                    )
                continue
            movie_tags = radarr_match["movie"].get("tags", [])
            if ids_include_any_label(radarr_tags, movie_tags, ("safe", "kids")):
                protected_labels = self._protected_labels_for_ids(radarr_tags, movie_tags)
                logger.info(
                    f"Skipping deletion for {movie['title']} ({movie['year']}) due to 'safe' or 'kids' tag"
                )
                self._record_decision(
                    reason_code="protected",
                    media_type="movie",
                    media_title=f"{movie['title']} ({movie['year']})",
                    reason="protected_series_or_movie_tags",
                    details={
                        "movie_tags": movie_tags,
                        "protected_labels": protected_labels,
                        "watched_by": movie.get("watched_by"),
                    },
                )
                continue
            user_tags = self.get_user_tags(radarr_tags, movie_tags)
            if self.should_delete_media(movie, user_tags, movie["watched_by"]):
                if not radarr_match["file_id"]:
                    logger.info(
                        f"Skipping deletion for {movie['title']} ({movie['year']}): "
                        f"matched Radarr entry '{radarr_match['movie'].get('title')}' "
                        "has no movie file id"
                    )
                    self._record_decision(
                        reason_code="skip",
                        media_type="movie",
                        media_title=f"{movie['title']} ({movie['year']})",
                        reason="no_movie_file_id",
                        details={
                            "radarr_id": radarr_match["movie"].get("id"),
                            "skip_category": "policy",
                            "watched_by": movie.get("watched_by"),
                        },
                    )
                    continue
                grace_key = self.leaving_soon_key(movie)
                if not self.leaving_soon_allows_delete(
                    grace_key, f"{movie['title']} ({movie['year']})", movie.get("rating_key")
                ):
                    continue
                if self.delete_radarr_movie_file(radarr_match["file_id"]):
                    if int(CONFIG.get("grace_days", 0) or 0) > 0:
                        self.run_summary.setdefault("deleted_after_grace", []).append(
                            f"{movie['title']} ({movie['year']})"
                        )
                    delete_reason = "dry-run" if CONFIG["dry_run"] else "delete"
                    self._record_decision(
                        reason_code=delete_reason,
                        media_type="movie",
                        media_title=f"{movie['title']} ({movie['year']})",
                        reason="standard watched",
                        details={
                            "radarr_id": radarr_match["movie"]["id"],
                            "file_id": radarr_match["file_id"],
                            "watched_by": movie.get("watched_by"),
                            "user_tags": user_tags,
                        },
                    )
                    self._record_summary("movie_deletions", f"{movie['title']} ({movie['year']}) [standard watched]")
                    self.unmonitor_radarr_movie(radarr_match["movie"]["id"])
                    self.remove_torrent_by_file_path(movie["file"])
                    self.remove_from_plex_watchlist(movie.get("rating_key"))
                else:
                    self._record_decision(
                        reason_code="error",
                        media_type="movie",
                        media_title=f"{movie['title']} ({movie['year']})",
                        reason="delete_failed",
                        details={"radarr_id": radarr_match["movie"]["id"]},
                    )
                    self._record_summary("errors", f"{movie['title']} ({movie['year']}) delete failed [standard watched]")
            else:
                self._record_decision(
                    reason_code="skip",
                    media_type="movie",
                    media_title=f"{movie['title']} ({movie['year']})",
                    reason="household_policy_not_satisfied",
                    details={
                        "user_tags": user_tags,
                        "policy": CONFIG.get("multi_user_delete_policy", "require_all_users"),
                        "watched_by": movie.get("watched_by"),
                        "skip_category": "policy",
                    },
                )

    def _lidarr_enabled(self):
        """Return True when optional Lidarr music cleanup is enabled."""
        return bool(CONFIG.get("lidarr", {}).get("enabled"))

    def _lidarr_request(self, endpoint, method="GET", data=None):
        """Make a request to the Lidarr API."""
        if not self._lidarr_enabled():
            logger.debug("Lidarr cleanup disabled; skipping Lidarr API call")
            return None
        result = self.lidarr_client.request(endpoint, method=method, data=data)
        if getattr(self.lidarr_client, "_session", None) is not None:
            self.lidarr_session = self.lidarr_client._session
        return result

    def get_lidarr_tags(self):
        """Get all tags from Lidarr. Returns None on error."""
        logger.debug("Getting Lidarr tags")
        if self._arr_cache["lidarr_tags"] is not None:
            return self._arr_cache["lidarr_tags"]
        tags = self._lidarr_request("tag")
        if tags is not None:
            self._arr_cache["lidarr_tags"] = tags
        return tags

    def get_lidarr_artists(self):
        """Get all artists from Lidarr."""
        logger.debug("Getting Lidarr artists")
        if self._arr_cache["lidarr_artists"] is not None:
            return self._arr_cache["lidarr_artists"]
        artists = self._lidarr_request("artist")
        if artists is not None:
            self._arr_cache["lidarr_artists"] = artists or []
            return self._arr_cache["lidarr_artists"]
        return []

    def get_lidarr_albums_for_artist(self, artist_id):
        """Get Lidarr albums for an artist, cached per artist ID."""
        cache = self._arr_cache["lidarr_albums_by_artist_id"]
        if artist_id in cache:
            return cache[artist_id]
        albums = self._lidarr_request(f"album?artistId={artist_id}") or []
        cache[artist_id] = albums
        return albums

    def get_lidarr_tracks_for_album(self, album_id):
        """Get Lidarr tracks for an album, cached per album ID."""
        cache = self._arr_cache["lidarr_tracks_by_album_id"]
        if album_id in cache:
            return cache[album_id]
        tracks = self._lidarr_request(f"track?albumId={album_id}") or []
        cache[album_id] = tracks
        return tracks

    def get_lidarr_tracks_for_artist(self, artist_id):
        """Get Lidarr tracks for an artist, cached per artist ID."""
        cache = self._arr_cache["lidarr_tracks_by_artist_id"]
        if artist_id in cache:
            return cache[artist_id]
        tracks = self._lidarr_request(f"track?artistId={artist_id}") or []
        cache[artist_id] = tracks
        return tracks

    @staticmethod
    def _normalize_music_title(title):
        """Normalize music titles for artist/album/track matching."""
        t = (title or "").lower()
        t = re.sub(r"\(.*?\)", "", t)
        t = re.sub(r"\[.*?\]", "", t)
        t = re.sub(r"^the\s+", "", t)
        t = re.sub(r"'s\b", "", t)
        t = re.sub(r"[^a-z0-9]", "", t)
        return t.strip()

    def _extract_music_mbids(self, media_item):
        """Extract MusicBrainz IDs from Plex track guid fields."""
        ids = {"track": set(), "album": set(), "artist": set()}
        if not isinstance(media_item, dict):
            return ids

        def consider(raw_value):
            text = str(raw_value or "")
            for match in re.finditer(
                r"(mbid|musicbrainz(?:track|album|artist)?)://([^/?]+)",
                text,
                re.IGNORECASE,
            ):
                scheme = match.group(1).lower()
                value = match.group(2).strip().lower()
                if not value:
                    continue
                if "track" in scheme:
                    ids["track"].add(value)
                elif "album" in scheme:
                    ids["album"].add(value)
                elif "artist" in scheme:
                    ids["artist"].add(value)
                else:
                    # Bare mbid://uuid — role unknown; keep on all for intersection checks.
                    ids["track"].add(value)
                    ids["album"].add(value)
                    ids["artist"].add(value)
            # plexapi sometimes uses com.plexapp.agents.musicbrainz://uuid?lang=en
            for match in re.finditer(
                r"musicbrainz://([^/?]+)",
                text,
                re.IGNORECASE,
            ):
                value = match.group(1).strip().lower()
                if value:
                    ids["track"].add(value)
                    ids["album"].add(value)
                    ids["artist"].add(value)

        consider(media_item.get("guid"))
        for guid in media_item.get("guids") or []:
            consider(guid)
        return ids

    def _lidarr_foreign_id(self, record, *keys):
        """Return a normalized foreign/MusicBrainz id from a Lidarr record."""
        if not isinstance(record, dict):
            return None
        for key in keys:
            value = record.get(key)
            if value:
                return str(value).strip().lower()
        foreign = record.get("foreignArtistId") or record.get("foreignAlbumId") or record.get(
            "foreignTrackId"
        )
        if foreign:
            return str(foreign).strip().lower()
        return None

    def get_played_tracks(self):
        """Get played (listened) music tracks from Plex artist libraries.

        Source of truth for play/listened state is Plex history / isWatched /
        viewCount — the same family of signals used for TV and movies. Lidarr
        remains the source of truth for whether a track file is managed and
        may be deleted (lifecycle, tags, monitoring).
        """
        logger.info("Checking for played music tracks in Plex")
        played_tracks = []
        try:
            all_sections = self.plex.library.sections()
            music_sections = [
                section for section in all_sections if section.type == "artist"
            ]
            logger.info(
                f"Found {len(music_sections)} music sections: "
                f"{[s.title for s in music_sections]}"
            )
            for section in music_sections:
                try:
                    tracks = section.search(libtype="track")
                except Exception as search_err:
                    logger.warning(
                        f"Failed to search tracks in music section '{section.title}': {search_err}"
                    )
                    continue
                for track in tracks:
                    is_played = bool(getattr(track, "isWatched", False)) or int(
                        getattr(track, "viewCount", 0) or 0
                    ) > 0
                    if not is_played:
                        continue
                    watch_status = self._get_watch_status(track)
                    guid_values = []
                    for guid in getattr(track, "guids", None) or []:
                        value = getattr(guid, "id", guid)
                        if value:
                            guid_values.append(str(value))
                    track_data = {
                        "title": track.title,
                        "artist": getattr(track, "grandparentTitle", None)
                        or getattr(track, "originalTitle", None),
                        "album": getattr(track, "parentTitle", None),
                        "track_number": getattr(track, "index", None)
                        or getattr(track, "trackNumber", None),
                        "year": getattr(track, "year", None)
                        or getattr(track, "parentYear", None),
                        "file": track.locations[0] if getattr(track, "locations", None) else None,
                        "watched_by": watch_status,
                        "watch_evidence": self.watch_evidence_by_rating_key.get(
                            track.ratingKey, {}
                        ).copy(),
                        "guid": getattr(track, "guid", None),
                        "guids": guid_values,
                        "rating_key": track.ratingKey,
                    }
                    if not any(watch_status.values()) and is_played:
                        track_data["is_watched_override"] = True
                    played_tracks.append(track_data)
            logger.info(f"Found {len(played_tracks)} played music tracks")
            return played_tracks
        except Exception as e:
            logger.error(f"Error getting played music tracks: {str(e)}")
            return []

    def match_track_to_lidarr(self, track, *, log_unmatched=True):
        """Match a Plex music track to a Lidarr-managed track.

        Matching order (first hit wins):
        1. MusicBrainz track id (foreignTrackId)
        2. Artist name + album title + track title (normalized)
        3. Artist name + track title when the track title is unique for the artist
        """
        logger.info(
            f"Matching track to Lidarr: {track.get('artist')} - "
            f"{track.get('album')} - {track.get('title')}"
        )
        artists = self.get_lidarr_artists()
        if not artists:
            if log_unmatched:
                logger.warning("No Lidarr artists available for music matching")
            return None

        mbids = self._extract_music_mbids(track)
        track_mbids = mbids["track"]
        album_mbids = mbids["album"]
        artist_mbids = mbids["artist"]

        # --- MusicBrainz track id match across managed artists ---
        if track_mbids:
            for artist in artists:
                for lidarr_track in self.get_lidarr_tracks_for_artist(artist["id"]):
                    foreign = self._lidarr_foreign_id(lidarr_track, "foreignTrackId")
                    if foreign and foreign in track_mbids:
                        logger.info(
                            f"Matched Lidarr track by MusicBrainz id: "
                            f"{lidarr_track.get('title')} (artist={artist.get('artistName')})"
                        )
                        return {
                            "artist": artist,
                            "album": lidarr_track.get("album") or {},
                            "track": lidarr_track,
                            "file_id": lidarr_track.get("trackFileId")
                            or (lidarr_track.get("trackFile") or {}).get("id"),
                        }

        target_artist = self._normalize_music_title(track.get("artist"))
        target_album = self._normalize_music_title(track.get("album"))
        target_title = self._normalize_music_title(track.get("title"))
        if not target_artist or not target_title:
            if log_unmatched:
                logger.warning(
                    f"Insufficient music metadata to match Lidarr track: "
                    f"artist={track.get('artist')!r} title={track.get('title')!r}"
                )
            return None

        # Prefer MBID artist, then normalized artist name.
        artist_candidates = []
        for artist in artists:
            foreign = self._lidarr_foreign_id(artist, "foreignArtistId")
            if foreign and foreign in artist_mbids:
                artist_candidates.append(artist)
                continue
            name = self._normalize_music_title(
                artist.get("artistName") or artist.get("sortName") or artist.get("name")
            )
            if name and name == target_artist:
                artist_candidates.append(artist)

        if not artist_candidates:
            if log_unmatched:
                logger.warning(f"Artist not found in Lidarr: {track.get('artist')}")
            return None

        for artist in artist_candidates:
            albums = self.get_lidarr_albums_for_artist(artist["id"])
            album_candidates = []
            for album in albums:
                foreign = self._lidarr_foreign_id(album, "foreignAlbumId")
                if foreign and foreign in album_mbids:
                    album_candidates.append(album)
                    continue
                if target_album and self._normalize_music_title(album.get("title")) == target_album:
                    album_candidates.append(album)

            search_albums = album_candidates or albums
            title_matches = []
            for album in search_albums:
                for lidarr_track in self.get_lidarr_tracks_for_album(album["id"]):
                    if self._normalize_music_title(lidarr_track.get("title")) == target_title:
                        title_matches.append((artist, album, lidarr_track))

            if len(title_matches) == 1:
                artist_match, album_match, track_match = title_matches[0]
                logger.info(
                    f"Matched Lidarr track by artist/album/title: "
                    f"{artist_match.get('artistName')} / {album_match.get('title')} / "
                    f"{track_match.get('title')}"
                )
                return {
                    "artist": artist_match,
                    "album": album_match,
                    "track": track_match,
                    "file_id": track_match.get("trackFileId")
                    or (track_match.get("trackFile") or {}).get("id"),
                }

            if album_candidates and title_matches:
                # Prefer matches restricted to the matched album set.
                album_ids = {a["id"] for a in album_candidates}
                album_restricted = [m for m in title_matches if m[1].get("id") in album_ids]
                if len(album_restricted) == 1:
                    artist_match, album_match, track_match = album_restricted[0]
                    logger.info(
                        f"Matched Lidarr track by album-restricted title: "
                        f"{track_match.get('title')}"
                    )
                    return {
                        "artist": artist_match,
                        "album": album_match,
                        "track": track_match,
                        "file_id": track_match.get("trackFileId")
                        or (track_match.get("trackFile") or {}).get("id"),
                    }

            # Track-number disambiguation when titles collide.
            track_number = track.get("track_number")
            if track_number is not None and title_matches:
                numbered = []
                for artist_match, album_match, track_match in title_matches:
                    lidarr_num = track_match.get("trackNumber") or track_match.get(
                        "absoluteTrackNumber"
                    )
                    try:
                        if int(lidarr_num) == int(track_number):
                            numbered.append((artist_match, album_match, track_match))
                    except (TypeError, ValueError):
                        if str(lidarr_num) == str(track_number):
                            numbered.append((artist_match, album_match, track_match))
                if len(numbered) == 1:
                    artist_match, album_match, track_match = numbered[0]
                    logger.info(
                        f"Matched Lidarr track by title+track number: "
                        f"{track_match.get('title')} #{track_number}"
                    )
                    return {
                        "artist": artist_match,
                        "album": album_match,
                        "track": track_match,
                        "file_id": track_match.get("trackFileId")
                        or (track_match.get("trackFile") or {}).get("id"),
                    }

        if log_unmatched:
            logger.warning(
                f"Track not found in Lidarr: {track.get('artist')} - "
                f"{track.get('album')} - {track.get('title')}"
            )
        return None

    def delete_lidarr_track_file(self, file_id):
        """Delete a track file from Lidarr."""
        if not file_id:
            logger.warning("No file ID provided for Lidarr track deletion")
            return False
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would delete track file from Lidarr: {file_id}")
            return True
        logger.info(f"Deleting track file from Lidarr: {file_id}")
        result = self._lidarr_request(f"trackfile/{file_id}", method="DELETE")
        if result is None:
            return False
        logger.info(f"Successfully deleted track file: {file_id}")
        return True

    def unmonitor_lidarr_track(self, track_id):
        """Unmonitor a track in Lidarr."""
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would unmonitor track {track_id} in Lidarr")
            return True
        logger.info(f"Attempting to unmonitor track {track_id} in Lidarr")
        track = self._lidarr_request(f"track/{track_id}")
        if track:
            track["monitored"] = False
            result = self._lidarr_request(f"track/{track_id}", method="PUT", data=track)
            if result is not None:
                logger.info(f"Unmonitored track {track_id}")
                return True
            logger.error(f"Failed to unmonitor track {track_id}")
            return False
        logger.error(f"Failed to retrieve track {track_id} for unmonitoring")
        return False

    def _describe_track(self, track):
        artist = track.get("artist") or "Unknown Artist"
        album = track.get("album") or "Unknown Album"
        title = track.get("title") or "Unknown Track"
        return f"{artist} - {album} - {title}"

    def _delete_track_and_cleanup(self, track_label, reason, file_id, track_id, file_path=None):
        logger.info(f"Deleting {track_label} via {reason}")
        if self.delete_lidarr_track_file(file_id):
            delete_reason = "dry-run" if CONFIG["dry_run"] else "delete"
            self._record_decision(
                reason_code=delete_reason,
                media_type="track",
                media_title=track_label,
                reason=reason,
                details={"track_id": track_id, "file_id": file_id},
            )
            self._record_summary("music_deletions", f"{track_label} [{reason}]")
            self.unmonitor_lidarr_track(track_id)
            if file_path:
                self.remove_torrent_by_file_path(file_path)
            return True
        self._record_decision(
            reason_code="error",
            media_type="track",
            media_title=track_label,
            reason="delete_failed",
            details={"track_id": track_id, "file_id": file_id, "delete_reason": reason},
        )
        self._record_summary("errors", f"{track_label} delete failed [{reason}]")
        return False

    def process_played_tracks(self):
        """Process played music tracks against Lidarr (optional; default OFF).

        Source of truth:
        - Plex play/listened signals decide *whether* a track is eligible.
        - Lidarr records, tags, and owned track files decide *what* can be deleted.
        """
        if not self._lidarr_enabled():
            logger.info(
                "Lidarr music cleanup disabled "
                "(set CLEANARR_LIDARR_ENABLE=true to opt in)"
            )
            return

        logger.info("Processing played music tracks via Lidarr")
        if not CONFIG["lidarr"].get("apikey"):
            logger.error(
                "CLEANARR_LIDARR_ENABLE is true but CLEANARR_LIDARR_APIKEY is missing; "
                "aborting music cleanup"
            )
            self._record_summary("errors", "lidarr enabled without api key")
            return

        lidarr_tags = self.get_lidarr_tags()
        if lidarr_tags is None:
            logger.error(
                "Failed to fetch Lidarr tags; aborting music cleanup to prevent "
                "accidental deletions."
            )
            return

        protected_tag_ids = protected_ids_from_tags(lidarr_tags)

        played_tracks = self.get_played_tracks()
        for track in played_tracks:
            track_label = self._describe_track(track)
            if not track.get("file"):
                logger.warning(f"No file path for track: {track_label}")
                self._record_decision(
                    reason_code="skip",
                    media_type="track",
                    media_title=track_label,
                    reason="missing_file_path",
                    details={"rating_key": track.get("rating_key")},
                )
                continue

            lidarr_match = self.match_track_to_lidarr(track)
            if not lidarr_match:
                self._record_decision(
                    reason_code="unmatched",
                    media_type="track",
                    media_title=track_label,
                    reason="no_lidarr_match",
                    details={"rating_key": track.get("rating_key")},
                )
                continue

            artist_tag_ids = set((lidarr_match["artist"] or {}).get("tags") or [])
            album_tag_ids = set((lidarr_match["album"] or {}).get("tags") or [])
            track_tag_ids = set((lidarr_match["track"] or {}).get("tags") or [])
            combined_tag_ids = artist_tag_ids | album_tag_ids | track_tag_ids

            if protected_tag_ids & combined_tag_ids:
                logger.info(
                    f"Skipping deletion for {track_label} due to 'safe' or 'kids' tag"
                )
                self._record_summary("protected_skips", f"{track_label} [protected tag]")
                self._record_decision(
                    reason_code="protected",
                    media_type="track",
                    media_title=track_label,
                    reason="protected_artist_album_or_track_tags",
                    details={
                        "artist_tags": list(artist_tag_ids),
                        "album_tags": list(album_tag_ids),
                        "track_tags": list(track_tag_ids),
                    },
                )
                continue

            has_been_played = any(track["watched_by"].values()) or track.get(
                "is_watched_override", False
            )
            if not has_been_played:
                self._record_decision(
                    reason_code="skip",
                    media_type="track",
                    media_title=track_label,
                    reason="not_played",
                    details={"watched_by": track["watched_by"]},
                )
                continue

            user_tags = self.get_user_tags(lidarr_tags, combined_tag_ids)
            if not self.should_delete_media(track, user_tags, track["watched_by"]):
                watched_users = [
                    user for user, watched in track["watched_by"].items() if watched
                ]
                skip_reason = (
                    "not_played" if not watched_users else "tagged_users_not_all_watched"
                )
                self._record_decision(
                    reason_code="skip",
                    media_type="track",
                    media_title=track_label,
                    reason=skip_reason,
                    details={
                        "user_tags": user_tags,
                        "watched_by": track["watched_by"],
                        "watch_evidence": track.get("watch_evidence"),
                    },
                )
                continue

            file_id = lidarr_match.get("file_id")
            if not file_id:
                logger.info(
                    f"Skipping deletion for {track_label}: matched Lidarr track "
                    f"'{lidarr_match['track'].get('title')}' has no track file id "
                    "(not owned / not imported)"
                )
                self._record_decision(
                    reason_code="skip",
                    media_type="track",
                    media_title=track_label,
                    reason="no_track_file_id",
                    details={"lidarr_track_id": lidarr_match["track"].get("id")},
                )
                continue

            self._delete_track_and_cleanup(
                track_label,
                "standard played",
                file_id,
                lidarr_match["track"]["id"],
                file_path=track.get("file"),
            )


    def run(self) -> CleanupResult:
        """Run the main cleanup process.

        Returns a CleanupResult with outcome taxonomy:
        - success: no errors recorded
        - partial_failure: some deletes/operations failed, run finished
        - aborted: unhandled exception stopped the run early
        """
        start_time = time.time()
        logger.info("Starting Plex media cleanup process")
        try:
            self.process_watched_episodes()
            self.process_watched_movies()
            self._sync_leaving_soon()
            self.process_played_tracks()
            self.clean_repeated_io_error_torrents()
            self.clean_sonarr_executable_downloads()
            self.clean_failed_downloads()
            self.remove_stale_torrents()
        except Exception as e:
            logger.error(f"Error during cleanup process: {str(e)}")
            self._mark_aborted(str(e))
        finally:
            try:
                self._flush_run_summary()
            except Exception as flush_exc:
                logger.error(f"Failed to flush cleanup summary: {flush_exc}")
                self._mark_partial_failure()
                self.run_summary.setdefault("errors", []).append(
                    f"summary flush failed: {flush_exc}"
                )

        elapsed_time = time.time() - start_time
        result = self._build_result()
        if result.failed:
            logger.error(
                f"Cleanup process finished with outcome={result.outcome} "
                f"in {elapsed_time:.2f} seconds ({len(result.errors)} error(s))"
            )
        else:
            logger.info(f"Cleanup process completed in {elapsed_time:.2f} seconds")
        return result


if __name__ == "__main__":
    cleaner = MediaCleanup()
    result = cleaner.run()
    sys.exit(result.exit_code())
