"""Disk free-space facts for the ``disk`` rule condition.

Free space is read from the Sonarr/Radarr ``GET /api/v3/diskspace`` endpoint,
so the job needs no filesystem mount. Fetch once per run with
``fetch_disk_facts`` and attach the result to each media fact with
``attach_disk_facts``. ``cleanarr.rules`` evaluates the facts and never
fetches them itself.

A source that cannot be read is recorded as ``{"error": ...}``. The rule
evaluator turns that into an ``error`` decision, never a delete.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from loguru import logger


DISKSPACE_ENDPOINT = "diskspace"
DISK_SOURCES = ("radarr", "sonarr")


def normalize_disk_path(path: Any) -> str:
    """Normalize a mount or folder path for prefix comparison.

    Backslashes become slashes and trailing slashes are dropped. Windows drive
    paths (``D:\\Media``) are lowercased because they compare case-insensitively.
    The filesystem root stays ``/``.
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        text = text.lower()
    stripped = text.rstrip("/")
    return stripped or "/"


def path_is_under(path: Any, mount: Any) -> bool:
    """True when ``path`` equals ``mount`` or sits below it, by whole segments."""
    target = normalize_disk_path(path)
    base = normalize_disk_path(mount)
    if not target or not base:
        return False
    if base == "/":
        return target.startswith("/")
    return target == base or target.startswith(base + "/")


def select_disk_entry(entries: Any, path: Any) -> dict | None:
    """Pick the entry whose path is the longest prefix of ``path``.

    When two entries share that path, the one with more free space wins, so a
    tie can only make cleanup less aggressive.
    """
    best = None
    best_key = None
    for entry in entries or ():
        if not isinstance(entry, Mapping):
            continue
        mount = normalize_disk_path(entry.get("path"))
        if not path_is_under(path, mount):
            continue
        free = entry.get("free_bytes")
        free_rank = free if isinstance(free, (int, float)) and not isinstance(free, bool) else -1
        key = (len(mount), free_rank)
        if best_key is None or key > best_key:
            best = dict(entry)
            best_key = key
    return best


def normalize_diskspace(payload: Any) -> list[dict]:
    """Convert an Arr ``/diskspace`` response to ``{path, free_bytes, total_bytes}`` entries.

    Raises ``ValueError`` when the payload is not a list. Entries without a
    path or a usable free-space number are dropped.
    """
    if not isinstance(payload, list):
        raise ValueError("diskspace response was not a list")
    entries = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        path = normalize_disk_path(raw.get("path"))
        free = _as_bytes(raw.get("freeSpace"))
        if not path or free is None:
            continue
        entry = {"path": path, "free_bytes": free}
        total = _as_bytes(raw.get("totalSpace"))
        if total is not None:
            entry["total_bytes"] = total
        entries.append(entry)
    return entries


def fetch_disk_facts(sources: Mapping[str, Any]) -> dict:
    """Fetch disk facts once for each named Arr source.

    ``sources`` maps ``radarr`` / ``sonarr`` to a ``ServarrClient`` (anything
    with ``request(endpoint)``) or to a callable taking the endpoint. A source
    mapped to ``None`` is left out, which the ``disk`` condition treats as
    "not gathered". A failed or malformed fetch is recorded as an error.
    """
    facts: dict[str, dict] = {}
    for name, client in (sources or {}).items():
        key = str(name or "").strip().lower()
        if key not in DISK_SOURCES:
            raise ValueError(f"unknown disk source {name!r}; expected one of {list(DISK_SOURCES)}")
        if client is None:
            continue
        request: Callable[[str], Any] = getattr(client, "request", None) or client
        try:
            payload = request(DISKSPACE_ENDPOINT)
            if payload is None:
                unreachable = getattr(client, "last_failure_unreachable", False)
                raise ValueError("unreachable" if unreachable else "request failed")
            facts[key] = {"entries": normalize_diskspace(payload)}
        except Exception as exc:
            logger.warning(f"{key} disk space fetch failed: {exc}")
            facts[key] = {"error": str(exc) or exc.__class__.__name__}
    return facts


def attach_disk_facts(fact: Mapping[str, Any], disk_facts: Mapping[str, Any] | None) -> dict:
    """Return a copy of ``fact`` carrying the run's disk facts.

    ``root_folder`` is filled from ``arr_item`` (``rootFolderPath``, then
    ``path``) unless the fact already has one.
    """
    enriched = dict(fact)
    enriched["disk"] = dict(disk_facts or {})
    if not enriched.get("root_folder"):
        root = item_root_folder(enriched.get("arr_item"))
        if root:
            enriched["root_folder"] = root
    return enriched


def item_root_folder(arr_item: Any) -> str:
    """Root folder of a Radarr movie or Sonarr series dict, or ``""``."""
    if not isinstance(arr_item, Mapping):
        return ""
    for key in ("rootFolderPath", "path"):
        value = arr_item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _as_bytes(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:  # NaN or negative
        return None
    return int(value)
