#!/usr/bin/env python3
"""Core Cleanarr cleanup logic shared by the job and webhook harnesses."""

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
from datetime import datetime, timedelta, timezone
import requests
from urllib.parse import urljoin
from plexapi.server import PlexServer
from transmission_rpc import Client as TransmissionClient
from loguru import logger

def _get_env(*keys, default=None):
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return default


def _normalize_tag_label(label: str) -> str:
    label = (label or "").strip().lower()
    label = re.sub(r"^\s*\d+\s*-\s*", "", label)
    return label


def _is_protected_tag_label(label: str) -> bool:
    return _normalize_tag_label(label) in ("safe", "kids")


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
    "dry_run": _env_flag("CLEANARR_DRY_RUN", default="false"),
    "disable_torrent_cleanup": _env_flag("CLEANARR_DISABLE_TORRENT_CLEANUP", default="false"),
    "remove_failed_downloads": _env_flag("CLEANARR_REMOVE_FAILED_DOWNLOADS", default="false"),
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
}

IO_ERROR_PATTERNS = (
    re.compile(r"input/output error", re.IGNORECASE),
    re.compile(r"stale file handle", re.IGNORECASE),
)


def _normalize_incomplete_name(name: str) -> str:
    name = (name or "").strip()
    if name.endswith(".part"):
        name = name[:-5]
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _iter_expected_incomplete_names(torrent):
    names = set()

    torrent_name = getattr(torrent, "name", None)
    if torrent_name:
        names.add(torrent_name)

    for attr in ("download_dir", "downloadDir"):
        download_dir = getattr(torrent, attr, None)
        if download_dir:
            names.add(Path(download_dir).name)

    try:
        torrent_files = torrent.files()
    except Exception as e:
        logger.warning(f"Failed to get files for torrent {torrent_name}: {e}. Falling back to torrent metadata.")
        return names

    for torrent_file in torrent_files:
        path = torrent_file.name if hasattr(torrent_file, "name") else torrent_file.get("name")
        if not path:
            continue
        path_obj = Path(path)
        if path_obj.parts:
            names.add(path_obj.parts[0])
        names.add(path_obj.name)

    return names


def _get_torrent_download_dir(torrent):
    for attr in ("download_dir", "downloadDir"):
        download_dir = getattr(torrent, attr, None)
        if download_dir:
            return download_dir
    return None


def _get_torrent_category(torrent):
    download_dir = _get_torrent_download_dir(torrent)
    if not download_dir:
        return None
    category = Path(download_dir).name.strip().lower()
    return category or None

# Setup logger
logger.remove()
logger.add(sys.stderr, level="DEBUG" if CONFIG["debug"] else "INFO")
logger.add(CONFIG["log_file"], rotation="10 MB", retention="1 week", level="INFO")


class MediaCleanup:
    """Main class for handling Plex media cleanup operations."""

    def __init__(self):
        """Initialize API connections."""
        logger.info("Initializing Plex Media Cleanup script")
        self.watch_evidence_by_rating_key = {}
        self.run_summary = {
            "tv_deletions": [],
            "movie_deletions": [],
            "protected_skips": [],
            "errors": [],
        }
        if not CONFIG["plex"]["token"]:
            logger.error("Missing Plex token. Set CLEANARR_PLEX_TOKEN or PLEX_TOKEN.")
            sys.exit(1)

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

        try:
            # Create session and inject headers *before* PlexServer connects
            session = requests.Session()
            if self.cf_headers:
                session.headers.update(self.cf_headers)
                logger.debug("Injected Cloudflare Access headers into Plex session")

            self.plex = PlexServer(CONFIG["plex"]["baseurl"], CONFIG["plex"]["token"], session=session)
            logger.info(f"Connected to Plex server: {self.plex.friendlyName}")
        except Exception as e:
            logger.error(f"Failed to connect to Plex: {str(e)}")

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

    def _record_summary(self, category, message):
        self.run_summary.setdefault(category, []).append(message)

    def _summarize_entries(self, category, label):
        entries = self.run_summary.get(category, [])
        if not entries:
            return f"{label}: none"
        return f"{label} ({len(entries)}): " + "; ".join(entries[:10])

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
            self._record_summary("errors", f"ntfy failed: {exc}")

    def _flush_run_summary(self):
        lines = [
            self._summarize_entries("tv_deletions", "TV deletions"),
            self._summarize_entries("movie_deletions", "Movie deletions"),
            self._summarize_entries("protected_skips", "Protected skips"),
            self._summarize_entries("errors", "Errors"),
        ]
        logger.info("Cleanup summary | " + " | ".join(lines))

        deletion_count = len(self.run_summary.get("tv_deletions", [])) + len(self.run_summary.get("movie_deletions", []))
        if deletion_count == 0 and not CONFIG.get("dry_run"):
            return

        title_prefix = "[DRY RUN] " if CONFIG.get("dry_run") else ""
        self._send_ntfy_summary(
            f"{title_prefix}Cleanarr summary",
            lines,
            priority="high" if deletion_count else "default",
        )

    def _torrent_cleanup_allowed(self, torrent, reason):
        torrent_name = getattr(torrent, "name", "unknown torrent")

        # --- Label filter (Transmission labels / tags) ---
        # When CLEANARR_TORRENT_CLEANUP_REQUIRED_LABELS is configured, only torrents
        # that carry at least one of the listed labels will be cleaned up.  This is
        # the safest way to restrict cleanup to *arr-managed torrents while leaving
        # untagged content (adult material, ebooks, etc.) untouched.
        required_labels = CONFIG.get("torrent_cleanup_required_labels") or set()
        if required_labels:
            torrent_labels = {
                lbl.strip().lower()
                for lbl in (getattr(torrent, "labels", None) or [])
                if lbl and lbl.strip()
            }
            # Readarr currently lacks the ability to send labels to Transmission.
            # As a fallback, if the download directory name matches a required label,
            # we consider it a match.
            category = _get_torrent_category(torrent)
            if category and category in required_labels:
                torrent_labels.add(category)

            if not torrent_labels.intersection(required_labels):
                logger.info(
                    f"Skipping torrent cleanup for {torrent_name} "
                    f"during {reason}: labels {torrent_labels or set()} do not "
                    f"match required {sorted(required_labels)}"
                )
                return False

        # --- Category (download-directory) filter ---
        allowed_categories = CONFIG.get("torrent_cleanup_allowed_categories") or set()
        if not allowed_categories:
            return True

        category = _get_torrent_category(torrent)
        if category in allowed_categories:
            return True

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
            torrents = self.transmission.get_torrents()
        except Exception as exc:
            logger.error(f"Failed to list Transmission torrents for I/O error cleanup: {exc}")
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

            if active:
                logger.warning(
                    f"Torrent {torrent.name} is reporting storage I/O errors but is still active; "
                    f"leaving it alone for now ({count}/{threshold})"
                )
                next_state[torrent_key] = entry
                continue

            if count < threshold:
                logger.warning(
                    f"Torrent {torrent.name} hit storage I/O error {count}/{threshold}: {error_string}"
                )
                next_state[torrent_key] = entry
                continue

            if CONFIG["dry_run"]:
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
                self.transmission.remove_torrent(torrent.id, delete_data=False)
            except Exception as exc:
                logger.error(f"Failed to remove torrent {torrent.name} after repeated I/O errors: {exc}")
                next_state[torrent_key] = entry

        try:
            self._save_io_error_state(next_state)
        except OSError as exc:
            logger.error(f"Failed to persist Transmission I/O error cleanup state: {exc}")

    def _sonarr_request(self, endpoint, method="GET", data=None):
        """Make a request to the Sonarr API."""
        if not CONFIG["sonarr"]["apikey"]:
            logger.error("Missing Sonarr API key. Set CLEANARR_SONARR_APIKEY.")
            return None
        url = urljoin(CONFIG["sonarr"]["baseurl"], endpoint)
        # Use session but ensure API key is present (headers.update merges)
        self.sonarr_session.headers.update({"X-Api-Key": CONFIG["sonarr"]["apikey"]})

        return self._arr_request("Sonarr", self.sonarr_session, url, endpoint, method, data)

    def _radarr_request(self, endpoint, method="GET", data=None):
        """Make a request to the Radarr API."""
        if not CONFIG["radarr"]["apikey"]:
            logger.error("Missing Radarr API key. Set CLEANARR_RADARR_APIKEY.")
            return None
        url = urljoin(CONFIG["radarr"]["baseurl"], endpoint)
        self.radarr_session.headers.update({"X-Api-Key": CONFIG["radarr"]["apikey"]})

        return self._arr_request("Radarr", self.radarr_session, url, endpoint, method, data)

    def _arr_request(self, service_name, session, url, endpoint, method="GET", data=None):
        """Make an ARR API request with light retrying for edge and upstream flakiness."""
        method = (method or "GET").upper()
        transient_statuses = {429, 500, 502, 503, 504}
        max_attempts = 3
        backoff_seconds = 1.5

        for attempt in range(1, max_attempts + 1):
            try:
                request_kwargs = {"timeout": 30}
                if method == "GET":
                    response = session.get(url, **request_kwargs)
                elif method == "DELETE":
                    response = session.delete(url, **request_kwargs)
                elif method in {"POST", "PUT"}:
                    session.headers.update({"Content-Type": "application/json"})
                    request_kwargs["data"] = json.dumps(data)
                    response = session.request(method, url, **request_kwargs)
                else:
                    response = session.request(method, url, **request_kwargs)
                if response.status_code in transient_statuses and attempt < max_attempts:
                    logger.warning(
                        f"{service_name} API transient response ({method} {endpoint}): "
                        f"{response.status_code}; retrying ({attempt}/{max_attempts})"
                    )
                    time.sleep(backoff_seconds * attempt)
                    continue

                response.raise_for_status()
                if not response.text or not response.text.strip():
                    # Treat empty 2xx responses as success. Sonarr/Radarr DELETEs commonly
                    # return an empty body, and callers should not interpret that as failure.
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return response.text
            except requests.exceptions.HTTPError as exc:
                response = exc.response
                status_code = response.status_code if response is not None else "unknown"
                response_text = response.text if response is not None else ""
                if status_code in transient_statuses and attempt < max_attempts:
                    logger.warning(
                        f"{service_name} API transient HTTP error ({method} {endpoint}): "
                        f"{status_code}; retrying ({attempt}/{max_attempts})"
                    )
                    time.sleep(backoff_seconds * attempt)
                    continue
                logger.error(
                    f"{service_name} API HTTP error ({method} {endpoint}): "
                    f"{status_code} - {response_text}"
                )
                return None
            except requests.exceptions.RequestException as exc:
                if attempt < max_attempts:
                    logger.warning(
                        f"{service_name} API request error ({method} {endpoint}): {exc}; "
                        f"retrying ({attempt}/{max_attempts})"
                    )
                    time.sleep(backoff_seconds * attempt)
                    continue
                logger.error(f"{service_name} API error ({method} {endpoint}): {exc}")
                return None
            except Exception as exc:
                logger.error(f"{service_name} API error ({method} {endpoint}): {exc}")
                return None

        return None

    def get_watched_movies(self):
        """Get all watched movies from Plex."""
        logger.info("Checking for watched movies in Plex")
        watched_movies = []
        try:
            movie_sections = [section for section in self.plex.library.sections() if section.type == "movie"]
            for section in movie_sections:
                for movie in section.search(unwatched=False):
                    watched_by = self._get_watch_status(movie)
                    watched_movies.append({
                        "title": movie.title,
                        "year": movie.year,
                        "file": movie.locations[0] if movie.locations else None,
                        "watched_by": watched_by,
                        "watch_evidence": self.watch_evidence_by_rating_key.get(movie.ratingKey, {}).copy(),
                        "guid": movie.guid,
                        "rating_key": movie.ratingKey,
                    })
            logger.info(f"Found {len(watched_movies)} watched movies")
            return watched_movies
        except Exception as e:
            logger.error(f"Error getting watched movies: {str(e)}")
            return []

    def get_watched_episodes(self):
        """Get all watched episodes from Plex."""
        logger.info("Checking for watched episodes in Plex")
        watched_episodes = []
        try:
            all_sections = self.plex.library.sections()
            logger.debug(f"Found {len(all_sections)} library sections: {[s.title for s in all_sections]}")
            show_sections = [section for section in all_sections if section.type == "show"]
            logger.info(f"Found {len(show_sections)} TV show sections: {[s.title for s in show_sections]}")
            
            for section in show_sections:
                shows = section.all()
                logger.debug(f"Section '{section.title}' has {len(shows)} shows")
                for show in shows:
                    episodes = show.episodes()
                    
                    # Special debug logging for Full Metal Alchemist
                    if "metal" in show.title.lower() or "alchemist" in show.title.lower():
                        logger.info(f"DEBUG: Found show '{show.title}' with {len(episodes)} episodes")
                        for i, ep in enumerate(episodes[:10]):  # First 10 episodes
                            logger.info(f"DEBUG: {show.title} S{ep.seasonNumber}E{ep.index} '{ep.title}' - isWatched={ep.isWatched}, viewCount={getattr(ep, 'viewCount', 'N/A')}")
                    
                    watched_count = sum(1 for ep in episodes if ep.isWatched)
                    if watched_count > 0:
                        logger.debug(f"Show '{show.title}' has {watched_count}/{len(episodes)} watched episodes")
                    for episode in episodes:
                        if episode.isWatched:
                            watch_status = self._get_watch_status(episode)
                            episode_data = {
                                "show_title": show.title,
                                "season": episode.seasonNumber,
                                "episode": episode.index,
                                "title": episode.title,
                                "file": episode.locations[0] if episode.locations else None,
                                "watched_by": watch_status,
                                "watch_evidence": self.watch_evidence_by_rating_key.get(episode.ratingKey, {}).copy(),
                                "guid": episode.guid,
                                "rating_key": episode.ratingKey,
                            }
                            # If episode is watched but no users have watch history, mark for override
                            if not any(watch_status.values()):
                                episode_data["is_watched_override"] = True
                                logger.debug(f"Episode {show.title} S{episode.seasonNumber}E{episode.index} marked as watched but no user history found")
                            watched_episodes.append(episode_data)
            logger.info(f"Found {len(watched_episodes)} watched episodes")
            return watched_episodes
        except Exception as e:
            logger.error(f"Error getting watched episodes: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    def _get_watch_status(self, media_item):
        watch_status = {}
        try:
            rating_key = getattr(media_item, "ratingKey", None)
            watch_evidence = {}
            # Get all users, including the server owner
            account = self.plex.myPlexAccount()
            users = account.users()

            # Check server owner's watch status separately
            # Local Plex records owner watches under accountID=1, not the cloud account ID
            owner_username = account.username or account.title
            
            # Check history first
            history_kwargs = {'ratingKey': media_item.ratingKey, 'accountID': 1}
            history = self.plex.history(**history_kwargs)
            
            # Fallback to isWatched status for owner if history is empty
            # This handles cases where items are manually marked as watched
            is_watched = bool(history)
            if not is_watched and hasattr(media_item, 'isWatched'):
                is_watched = media_item.isWatched
                if is_watched:
                    watch_evidence[owner_username] = "isWatched_fallback"
                    logger.debug(f"Owner '{owner_username}' marked as watched via isWatched fallback (history was empty)")
            if owner_username not in watch_evidence:
                watch_evidence[owner_username] = "history" if history else "not_watched"

            watch_status[owner_username] = is_watched
            logger.debug(
                f"Watch status for owner '{owner_username}' (accountID=1): {is_watched}, "
                f"history items: {len(history)}, evidence={watch_evidence[owner_username]}"
            )

            # Check managed users' watch status (they use their cloud account IDs)
            for user in users:
                username = user.username or user.title
                user_id = getattr(user, 'id', None)
                logger.debug(f"Checking user '{username}' with id={user_id}")
                history_kwargs = {'ratingKey': media_item.ratingKey}
                if user_id is not None:
                    history_kwargs['accountID'] = user_id
                history = self.plex.history(**history_kwargs)
                watch_status[username] = bool(history)
                watch_evidence[username] = "history" if history else "not_watched"
                logger.debug(
                    f"Watch status for user '{username}' (accountID={user_id}): {bool(history)}, "
                    f"history items: {len(history)}, evidence={watch_evidence[username]}"
                )

            logger.info(f"Watch status for {media_item.title}: {watch_status}")
            if rating_key is not None:
                self.watch_evidence_by_rating_key[rating_key] = watch_evidence
            return watch_status
        except Exception as e:
            logger.error(f"Error getting watch status for ratingKey {media_item.ratingKey}: {str(e)}")
            return {}

    def get_sonarr_tags(self):
        """Get all tags from Sonarr. Returns None on error."""
        logger.debug("Getting Sonarr tags")
        return self._sonarr_request("tag")

    def get_radarr_tags(self):
        """Get all tags from Radarr. Returns None on error."""
        logger.debug("Getting Radarr tags")
        return self._radarr_request("tag")

    def get_sonarr_series(self):
        """Get all series from Sonarr."""
        logger.debug("Getting Sonarr series")
        return self._sonarr_request("series") or []

    def get_radarr_movies(self):
        """Get all movies from Radarr."""
        logger.debug("Getting Radarr movies")
        return self._radarr_request("movie") or []

    def get_sonarr_episode(self, series_id, season_number, episode_number):
        """Get episode from Sonarr by series ID, season, and episode numbers."""
        logger.debug(f"Getting Sonarr episode: series={series_id}, S{season_number}E{episode_number}")
        episodes = self._sonarr_request(f"episode?seriesId={series_id}")
        if episodes:
            for episode in episodes:
                if episode["seasonNumber"] == season_number and episode["episodeNumber"] == episode_number:
                    return episode
        return None

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

    def match_movie_to_radarr(self, movie):
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
        for m in movie_list:
            if m["title"].lower() == movie["title"].lower() and m["year"] == movie["year"]:
                return {
                    "movie": m,
                    "file_id": m.get("movieFile", {}).get("id")
                }

        # Fallback: try normalized matching (with year)
        target_norm = normalize(movie["title"])
        for m in movie_list:
            if m["year"] == movie["year"]:
                # Exact normalized match
                if normalize(m["title"]) == target_norm:
                    logger.info(f"Matched by normalized title: Radarr='{m['title']}' for Plex='{movie['title']}'")
                    return {
                        "movie": m,
                        "file_id": m.get("movieFile", {}).get("id")
                    }

        # Final fallback: containment matching (with year)
        target_tokens = tokenize(movie["title"])
        for m in movie_list:
            if m["year"] == movie["year"]:
                candidate_tokens = tokenize(m["title"])
                if (
                    len(target_tokens) >= 2
                    and len(candidate_tokens) >= 2
                    and (
                        target_tokens.issubset(candidate_tokens)
                        or candidate_tokens.issubset(target_tokens)
                    )
                ):
                    logger.info(
                        f"Matched by token subset: Radarr='{m['title']}' for Plex='{movie['title']}'"
                    )
                    return {
                        "movie": m,
                        "file_id": m.get("movieFile", {}).get("id")
                    }

        logger.warning(f"Movie not found in Radarr: {movie['title']} ({movie['year']})")
        return None

    def get_user_tags(self, tags, tag_ids):
        """Get usernames from tag IDs, excluding 'safe'."""
        user_tags = []
        for tag in tags:
            if tag["id"] in tag_ids and not _is_protected_tag_label(tag.get("label")):
                # Use the helper to ensure consistent normalization (lower case + strip prefix)
                cleaned_tag = _normalize_tag_label(tag.get("label"))
                if cleaned_tag:
                    user_tags.append(cleaned_tag)
        return user_tags

    def should_delete_media(self, media_item, user_tags, watched_by):
        """Determine if media should be deleted based on watch status and user tags."""
        logger.info("should_delete_media called")
        # Determine the media name for logging
        media_name = media_item.get('title', 'Unknown Media')
        if 'season' in media_item and 'episode' in media_item:
            media_name = f"{media_item['show_title']} S{media_item['season']}E{media_item['episode']}"
        
        if not user_tags:
            logger.info(f"No user tags found for {media_name}, proceeding with deletion")
            return True
        
        # Log users who have watched the media
        logger.info(f"Watched by items: {watched_by.items()}")
        watched_users = [user.lower() for user, watched in watched_by.items() if watched]
        if watched_users:
            logger.info(f"Users who have watched {media_name}: {', '.join(watched_users)}")
        else:
            logger.info(f"No users have watched {media_name} yet")
        
        # Check and log users who have not watched the media
        for user_tag in user_tags:
            found = False
            for watched_user in watched_users:
                # Use regex for flexible matching (e.g., 'user' matches 'user123')
                if re.search(user_tag, watched_user) or re.search(watched_user, user_tag):
                    found = True
                    break
            if not found:
                logger.info(f"User {user_tag} has not watched {media_name} yet")
                return False
        
        logger.info(f"All tagged users have watched {media_name}: {user_tags}")
        return True

    def _describe_episode(self, episode):
        return f"{episode['show_title']} S{episode['season']}E{episode['episode']}"

    def _delete_episode_and_cleanup(self, episode_label, reason, file_id, episode_id, file_path=None, rating_key=None):
        logger.info(f"Deleting {episode_label} via {reason}")
        if self.delete_sonarr_episode_file(file_id):
            self._record_summary("tv_deletions", f"{episode_label} [{reason}]")
            self.unmonitor_sonarr_episode(episode_id)
            if file_path:
                self.remove_torrent_by_file_path(file_path)
            if rating_key:
                self.remove_from_plex_watchlist(rating_key)
            return True
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
            return
        logger.info(f"Attempting to unmonitor episode {episode_id} in Sonarr")
        episode = self._sonarr_request(f"episode/{episode_id}")
        if episode:
            episode["monitored"] = False
            result = self._sonarr_request(f"episode/{episode_id}", method="PUT", data=episode)
            if result is not None:
                logger.info(f"Unmonitored episode {episode_id}")
            else:
                logger.error(f"Failed to unmonitor episode {episode_id}")
        else:
            logger.error(f"Failed to retrieve episode {episode_id} for unmonitoring")

    def unmonitor_radarr_movie(self, movie_id):
        """Unmonitor a movie in Radarr."""
        if CONFIG["dry_run"]:
            logger.info(f"[DRY RUN] Would unmonitor movie {movie_id} in Radarr")
            return
        logger.info(f"Attempting to unmonitor movie {movie_id} in Radarr")
        movie = self._radarr_request(f"movie/{movie_id}")
        if movie:
            movie["monitored"] = False
            result = self._radarr_request("movie", method="PUT", data=movie)
            if result is not None:
                logger.info(f"Unmonitored movie {movie_id}")
            else:
                logger.error(f"Failed to unmonitor movie {movie_id}")
        else:
            logger.error(f"Failed to retrieve movie {movie_id} for unmonitoring")

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
            torrents = self.transmission.get_torrents()
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
                        
                        self.transmission.remove_torrent(torrent.id, delete_data=True)
                        logger.info(f"Successfully removed torrent: {torrent.name}")
                        return True
            logger.warning(f"No matching torrent found for file: {file_path}")
            return False
        except Exception as e:
            logger.error(f"Error removing torrent by file path {file_path}: {str(e)}")
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
            torrents = self.transmission.get_torrents()
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
                                self.transmission.remove_torrent(torrent.id, delete_data=True)
                                removed_torrent_ids.add(torrent.id)
                            except Exception as e:
                                logger.error(f"Failed to remove errored torrent {torrent.name}: {e}")
            else:
                logger.info("Failed download cleanup is disabled")

            # 2. Clean orphaned files in incomplete directory
            if not remove_orphan_incomplete_downloads:
                logger.info("Orphan incomplete cleanup is disabled")
                return

            session = self.transmission.get_session()
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
                else:
                    logger.debug(f"Keeping file {filename} (belongs to active torrent)")

        except Exception as e:
            logger.error(f"Error cleaning failed downloads: {e}")

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
            torrents = self.transmission.get_torrents()
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
                        self.transmission.remove_torrent(torrent.id, delete_data=True)
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
                        self.transmission.remove_torrent(torrent.id, delete_data=True)
                    else:
                        logger.debug(f"Torrent {torrent.name} not stale (Age: {age}, Complete: {is_complete} | {torrent.percent_done}, Peers: {torrent.peers_connected}, Status: {torrent.status})")
                except Exception as e:
                    logger.error(f"Error processing torrent {torrent.name}: {str(e)}")
                    continue
        except Exception as e:
            logger.error(f"Error in stale torrent check: {str(e)}")

    def remove_from_plex_watchlist(self, rating_key):
        """Remove an item from the Plex Watchlist for the owner and managed users."""
        if not rating_key:
            return

        try:
            # Get owner account
            account = self.plex.myPlexAccount()
            users = account.users()
            all_accounts = [account] + users

            for acc in all_accounts:
                try:
                    # For managed users, we might need to authenticate as them or use their ID
                    # PlexAPI handling of watchlists for managed users can be tricky.
                    # Standard 'removeFromWatchlist' usually works on the authenticated user's watchlist.
                    # For the owner (self.plex), it should work directly.
                    
                    # We'll rely on the server connection to remove it from the watchlist
                    # if the item is found in the watchlist.
                    
                    # Currently, the most reliable way via plexapi is to fetch the item and call removeFromWatchlist
                    # But we might have already deleted the file, so fetching might fail if it relies on file presence?
                    # Actually, Plex keeps metadata even if file is missing (Trash).
                    
                    # Let's try to fetch the item using the library section
                    pass
                except Exception:
                    pass
            
            # Simpler approach: Use the server library to fetch the item and call remove
            # This action is usually per-user. The script runs as the owner (via token).
            # So this will primarily likely remove from Owner's watchlist.
            try:
                item = self.plex.fetchItem(rating_key)
                if item:
                    logger.info(f"Attempting to remove {item.title} from Plex Watchlist (Owner)")
                    # The removeFromWatchlist method exists on the object in newer plexapi versions
                    # Or we can use account.removeFromWatchlist(item)
                    
                    if hasattr(item, 'removeFromWatchlist'):
                        item.removeFromWatchlist()
                        logger.info(f"Successfully removed {item.title} from Watchlist")
                    else:
                        # Fallback to account method
                        account.removeFromWatchlist(item)
                        logger.info(f"Successfully removed {item.title} from Watchlist via Account")
            except Exception as e:
                logger.warning(f"Failed to remove item {rating_key} from Watchlist: {e}")

        except Exception as e:
            logger.error(f"Error in remove_from_plex_watchlist: {e}")

    def process_watched_episodes(self):
        """Process all watched episodes."""
        logger.info("Processing watched episodes")
        watched_episodes = self.get_watched_episodes()
        sonarr_tags = self.get_sonarr_tags()
        if sonarr_tags is None:
            logger.error("Failed to fetch Sonarr tags; aborting watched episode processing to prevent accidental deletions.")
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
        protected_tag_ids = {
            tag["id"]
            for tag in sonarr_tags
            if _is_protected_tag_label(tag.get("label"))
        }

        for series in sonarr_series_list:
            show_title = series["title"]
            episodes = self._sonarr_request(f"episode?seriesId={series['id']}") or []
            # Build a mapping: (season, episode) -> episode dict
            all_eps = {(ep["seasonNumber"], ep["episodeNumber"]): ep for ep in episodes}
            # For each user, check for watched ahead
            for user in show_user_watched[show_title]:
                watched_set = show_user_watched[show_title][user]
                # For each episode, if user has watched N+2 or greater, but not N, mark N for deletion
                ep_numbers_by_season = defaultdict(list)
                for (season, epnum) in all_eps:
                    ep_numbers_by_season[season].append(epnum)
                for season, epnums in ep_numbers_by_season.items():
                    epnums_sorted = sorted(epnums)
                    for epnum in epnums_sorted:
                        ahead_eps = sorted(
                            watched_epnum
                            for watched_season, watched_epnum in watched_set
                            if watched_season == season and watched_epnum >= epnum + 2
                        )
                        if ahead_eps and (season, epnum) not in watched_set:
                            # Only delete if file exists and not already deleted
                            ep_obj = all_eps[(season, epnum)]
                            file_id = ep_obj.get("episodeFileId")
                            if not file_id:
                                continue
                            # Find matching Plex episode for file path
                            plex_ep = show_season_ep_map[show_title][season].get(epnum)
                            file_path = plex_ep["file"] if plex_ep else None
                            
                            # Check if series has kids or safe tags before deleting
                            series_tag_ids = set(series.get("tags") or [])
                            episode_tag_ids = set(ep_obj.get("tags") or [])
                            if protected_tag_ids & (series_tag_ids | episode_tag_ids):
                                logger.info(
                                    f"[Watched Ahead] Skipping deletion for {show_title} S{season}E{epnum} "
                                    f"due to 'safe' or 'kids' tag (user '{user}' watched later episodes {ahead_eps})"
                                )
                                self._record_summary(
                                    "protected_skips",
                                    f"{show_title} S{season}E{epnum} [watched-ahead by {user}: {ahead_eps}]",
                                )
                                continue

                            episode_label = f"{show_title} S{season}E{epnum}"
                            logger.info(
                                f"[Watched Ahead] User '{user}' has real later watched episodes {ahead_eps} "
                                f"for {episode_label}. Deleting episode file. File: {file_path}"
                            )
                            self._delete_episode_and_cleanup(
                                episode_label,
                                f"watched-ahead user={user} later={ahead_eps}",
                                file_id,
                                ep_obj["id"],
                                file_path=file_path,
                                rating_key=plex_ep.get("rating_key") if plex_ep else None,
                            )

        # --- Standard logic for watched episodes ---
        for episode in watched_episodes:
            if not episode["file"]:
                logger.warning(f"No file path for episode: {episode['show_title']} S{episode['season']}E{episode['episode']}")
                continue
            sonarr_match = self.match_episode_to_sonarr(episode)
            if not sonarr_match:
                logger.warning(f"Skipping deletion for {episode['show_title']} S{episode['season']}E{episode['episode']} because no Sonarr match was found.")
                continue
            episode_label = self._describe_episode(episode)
            series_tags = sonarr_match["series"].get("tags", [])
            episode_tags = sonarr_match["episode"].get("tags") or []
            series_tag_ids = set(series_tags or [])
            episode_tag_ids = set(episode_tags)
            if protected_tag_ids & (series_tag_ids | episode_tag_ids):
                logger.info(
                    f"Skipping deletion for {episode_label} "
                    "due to 'safe' or 'kids' tag"
                )
                self._record_summary("protected_skips", f"{episode_label} [protected tag]")
                continue
            # Check if episode has been watched by any user, or if it's marked as watched at the episode level
            # (handles cases where server marks episode as watched but individual user histories are missing)
            has_been_watched = any(episode["watched_by"].values()) or episode.get("is_watched_override", False)
            
            if not has_been_watched:
                logger.info(f"Skipping deletion for {episode_label} because no users have watched it.")
                continue

            user_tags = self.get_user_tags(sonarr_tags, series_tag_ids | episode_tag_ids)
            watch_evidence = episode.get("watch_evidence", {})
            if any(source == "isWatched_fallback" for source in watch_evidence.values()):
                logger.info(
                    f"{episode_label} includes isWatched fallback evidence for exact-episode evaluation: {watch_evidence}"
                )
            if not self.should_delete_media(episode, user_tags, episode["watched_by"]):
                logger.info(f"Skipping deletion for {episode_label} because tagged users have not all watched it.")
                continue

            self._delete_episode_and_cleanup(
                episode_label,
                "standard watched",
                sonarr_match["file_id"],
                sonarr_match["episode"]["id"],
                file_path=episode["file"],
                rating_key=episode.get("rating_key"),
            )

    def process_watched_movies(self):
        """Process all watched movies."""
        logger.info("Processing watched movies")
        watched_movies = self.get_watched_movies()
        radarr_tags = self.get_radarr_tags()
        if radarr_tags is None:
            logger.error("Failed to fetch Radarr tags; aborting watched movie processing to prevent accidental deletions.")
            return
        
        for movie in watched_movies:
            if not movie["file"]:
                logger.warning(f"No file path for movie: {movie['title']} ({movie['year']})")
                continue
            radarr_match = self.match_movie_to_radarr(movie)
            if not radarr_match:
                continue
            movie_tags = radarr_match["movie"].get("tags", [])
            kids_tag = next((tag for tag in radarr_tags if _normalize_tag_label(tag.get("label")) == "kids"), None)
            safe_tag = next((tag for tag in radarr_tags if _normalize_tag_label(tag.get("label")) == "safe"), None)
            if ((safe_tag and safe_tag["id"] in movie_tags) or
                    (kids_tag and kids_tag["id"] in movie_tags)):
                logger.info(
                    f"Skipping deletion for {movie['title']} ({movie['year']}) due to 'safe' or 'kids' tag"
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
                    continue
                if self.delete_radarr_movie_file(radarr_match["file_id"]):
                    self._record_summary("movie_deletions", f"{movie['title']} ({movie['year']}) [standard watched]")
                    self.unmonitor_radarr_movie(radarr_match["movie"]["id"])
                    self.remove_torrent_by_file_path(movie["file"])
                    self.remove_from_plex_watchlist(movie.get("rating_key"))

    def run(self):
        """Run the main cleanup process."""
        start_time = time.time()
        logger.info("Starting Plex media cleanup process")
        try:
            self.process_watched_episodes()
            self.process_watched_movies()
            self.clean_repeated_io_error_torrents()
            self.clean_failed_downloads()
            self.remove_stale_torrents()
            self._flush_run_summary()
            elapsed_time = time.time() - start_time
            logger.info(f"Cleanup process completed in {elapsed_time:.2f} seconds")
        except Exception as e:
            logger.error(f"Error during cleanup process: {str(e)}")


if __name__ == "__main__":
    cleaner = MediaCleanup()
    cleaner.run()

