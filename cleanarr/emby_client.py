"""Emby REST helpers for job-mode watched state and webhook event mapping.

Emby Server API is authenticated with a static API key via ``X-Emby-Token``
(or ``api_key`` query param). Watched state is read per-user from
``/Users/{userId}/Items?IsPlayed=true``.

Native Emby notification webhooks use an ``Event`` field (e.g.
``playback.stop``, ``item.markplayed``) with nested ``Item`` / ``User``
objects. Community webhook plugins may also emit Jellyfin-style
``NotificationType`` payloads; both shapes are accepted.
"""

from __future__ import annotations

import html
from typing import Any, Iterable
from urllib.parse import urljoin

import requests


def normalize_baseurl(baseurl: str) -> str:
    raw = (baseurl or "").strip().rstrip("/")
    if not raw:
        return ""
    return raw


def auth_headers(apikey: str) -> dict[str, str]:
    token = (apikey or "").strip()
    if not token:
        return {}
    # Jellyfin 10.11+/12.x rejects the legacy X-Emby-Token header (401);
    # the MediaBrowser Authorization scheme works on Jellyfin and Emby.
    return {
        "Authorization": f'MediaBrowser Token="{token}"',
        "X-Emby-Token": token,
        "Accept": "application/json",
    }


def emby_get(
    session: requests.Session,
    baseurl: str,
    apikey: str,
    path: str,
    params: dict | None = None,
    timeout: int = 30,
) -> Any:
    """GET an Emby API path; returns parsed JSON or None on failure."""
    root = normalize_baseurl(baseurl)
    if not root or not (apikey or "").strip():
        return None
    url = urljoin(root + "/", path.lstrip("/"))
    response = session.get(
        url,
        headers=auth_headers(apikey),
        params=params or {},
        timeout=timeout,
    )
    response.raise_for_status()
    if not response.content:
        return None
    return response.json()


def list_users(
    session: requests.Session,
    baseurl: str,
    apikey: str,
    allowed_usernames: Iterable[str] | None = None,
) -> list[dict]:
    """Return Emby users, optionally filtered by username (case-insensitive)."""
    data = emby_get(session, baseurl, apikey, "Users")
    if not isinstance(data, list):
        return []
    allowed = {
        name.strip().lower()
        for name in (allowed_usernames or [])
        if name and str(name).strip()
    }
    users = []
    for user in data:
        if not isinstance(user, dict):
            continue
        name = (user.get("Name") or "").strip()
        user_id = user.get("Id")
        if not name or not user_id:
            continue
        if allowed and name.lower() not in allowed:
            continue
        # Skip disabled users when the flag is present
        if user.get("Policy", {}).get("IsDisabled"):
            continue
        users.append({"id": str(user_id), "name": name})
    return users


def list_played_items(
    session: requests.Session,
    baseurl: str,
    apikey: str,
    user_id: str,
    item_types: str,
) -> list[dict]:
    """Return played library items of the given Emby ItemType(s) for one user."""
    params = {
        "IsPlayed": "true",
        "Recursive": "true",
        "IncludeItemTypes": item_types,
        "Fields": "Path,ProviderIds,MediaSources,UserData,SeriesName,"
        "ParentIndexNumber,IndexNumber,ProductionYear,Overview",
        "EnableTotalRecordCount": "false",
    }
    data = emby_get(
        session,
        baseurl,
        apikey,
        f"Users/{user_id}/Items",
        params=params,
    )
    if not isinstance(data, dict):
        return []
    items = data.get("Items") or []
    return [item for item in items if isinstance(item, dict)]


def _provider_guids(provider_ids: dict | None) -> tuple[str | None, list[str]]:
    if not isinstance(provider_ids, dict):
        return None, []
    # Emby keys are typically Imdb / Tmdb / Tvdb (mixed case)
    normalized = {str(k).lower(): v for k, v in provider_ids.items() if v}
    guids: list[str] = []
    primary = None
    for key, scheme in (("imdb", "imdb"), ("tmdb", "tmdb"), ("tvdb", "tvdb")):
        value = normalized.get(key)
        if not value:
            continue
        guid = f"{scheme}://{value}"
        guids.append(guid)
        if primary is None:
            primary = guid
    return primary, guids


def _item_path(item: dict) -> str | None:
    path = item.get("Path")
    if path:
        return str(path)
    media_sources = item.get("MediaSources") or []
    if isinstance(media_sources, list):
        for source in media_sources:
            if isinstance(source, dict) and source.get("Path"):
                return str(source["Path"])
    return None


def _item_identity(item: dict) -> str:
    """Stable key used to merge the same library item across users."""
    item_id = item.get("Id")
    if item_id:
        return f"id:{item_id}"
    path = _item_path(item)
    if path:
        return f"path:{path}"
    primary, _ = _provider_guids(item.get("ProviderIds"))
    if primary:
        return f"guid:{primary}"
    name = item.get("Name") or item.get("SeriesName") or "unknown"
    year = item.get("ProductionYear") or ""
    return f"title:{name}:{year}"


def _with_unwatched_users(records, items_by_user) -> list[dict]:
    """Record every loaded user, not only watchers (cleanarr#80).

    The policy falls back to "all users in watched_by" as the household, so a
    map that only lists watchers lets one watcher satisfy require_all_users.
    """
    out = []
    for record in records:
        for username in items_by_user:
            record["watched_by"].setdefault(username, False)
        out.append(record)
    return out


def build_watched_movies(
    items_by_user: dict[str, list[dict]],
) -> list[dict]:
    """Merge per-user played movie lists into Cleanarr watched-movie records."""
    by_key: dict[str, dict] = {}
    for username, items in items_by_user.items():
        for item in items:
            item_type = (item.get("Type") or "Movie").lower()
            if item_type and item_type != "movie":
                continue
            key = _item_identity(item)
            primary_guid, guids = _provider_guids(item.get("ProviderIds"))
            record = by_key.setdefault(
                key,
                {
                    "title": item.get("Name") or "Unknown",
                    "year": item.get("ProductionYear"),
                    "file": _item_path(item),
                    "watched_by": {},
                    "watch_evidence": {},
                    "guid": primary_guid,
                    "guids": guids,
                    "rating_key": item.get("Id"),
                    "source": "emby",
                },
            )
            # Prefer a non-empty path if a later user has one
            if not record.get("file"):
                record["file"] = _item_path(item)
            if not record.get("guid") and primary_guid:
                record["guid"] = primary_guid
                record["guids"] = guids
            record["watched_by"][username] = True
            record["watch_evidence"][username] = "emby_isplayed"
    return _with_unwatched_users(by_key.values(), items_by_user)


def build_watched_episodes(
    items_by_user: dict[str, list[dict]],
) -> list[dict]:
    """Merge per-user played episode lists into Cleanarr watched-episode records."""
    by_key: dict[str, dict] = {}
    for username, items in items_by_user.items():
        for item in items:
            if (item.get("Type") or "").lower() != "episode":
                continue
            key = _item_identity(item)
            primary_guid, _guids = _provider_guids(item.get("ProviderIds"))
            season = item.get("ParentIndexNumber")
            episode = item.get("IndexNumber")
            record = by_key.setdefault(
                key,
                {
                    "show_title": item.get("SeriesName") or item.get("Series") or "Unknown",
                    "season": season,
                    "episode": episode,
                    "title": item.get("Name") or "Unknown",
                    "file": _item_path(item),
                    "watched_by": {},
                    "watch_evidence": {},
                    "guid": primary_guid,
                    "rating_key": item.get("Id"),
                    "source": "emby",
                },
            )
            if not record.get("file"):
                record["file"] = _item_path(item)
            record["watched_by"][username] = True
            record["watch_evidence"][username] = "emby_isplayed"
    return _with_unwatched_users(by_key.values(), items_by_user)


def load_watched_state(
    session: requests.Session,
    baseurl: str,
    apikey: str,
    allowed_usernames: Iterable[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Load watched movies and episodes from Emby for all (or filtered) users."""
    users = list_users(session, baseurl, apikey, allowed_usernames=allowed_usernames)
    movies_by_user: dict[str, list[dict]] = {}
    episodes_by_user: dict[str, list[dict]] = {}
    for user in users:
        name = user["name"]
        uid = user["id"]
        movies_by_user[name] = list_played_items(
            session, baseurl, apikey, uid, "Movie"
        )
        episodes_by_user[name] = list_played_items(
            session, baseurl, apikey, uid, "Episode"
        )
    return (
        build_watched_movies(movies_by_user),
        build_watched_episodes(episodes_by_user),
    )


def get_item_watch_state(
    session: requests.Session,
    baseurl: str,
    apikey: str,
    item_id: str,
    allowed_usernames: Iterable[str] | None = None,
) -> tuple[dict | None, dict[str, bool]]:
    """Read one library item and every user's ``UserData.Played`` for it.

    Returns ``(item, watched_by)``. ``item`` is the first user-visible copy of
    the item (path, provider IDs, series/season/episode numbers) or ``None``
    when no user can see it. ``watched_by`` maps each non-disabled username to
    their played flag; users who cannot see the item count as not watched.
    """
    if not item_id:
        return None, {}
    users = list_users(session, baseurl, apikey, allowed_usernames=allowed_usernames)
    item: dict | None = None
    watched_by: dict[str, bool] = {}
    params = {
        "Ids": str(item_id),
        "Recursive": "true",
        "Fields": "Path,ProviderIds,MediaSources,UserData,SeriesName,"
        "ParentIndexNumber,IndexNumber,ProductionYear",
        "EnableTotalRecordCount": "false",
    }
    for user in users:
        data = emby_get(session, baseurl, apikey, f"Users/{user['id']}/Items", params=params)
        rows = (data or {}).get("Items") if isinstance(data, dict) else None
        row = next(
            (r for r in (rows or []) if isinstance(r, dict) and str(r.get("Id")) == str(item_id)),
            None,
        )
        if row is None:
            watched_by[user["name"]] = False
            continue
        if item is None:
            item = row
        watched_by[user["name"]] = bool((row.get("UserData") or {}).get("Played"))
    return item, watched_by


def item_record(item: dict) -> dict:
    """Map an Emby/Jellyfin item to the fields cleanup matching uses."""
    primary, guids = _provider_guids(item.get("ProviderIds"))
    return {
        "type": str(item.get("Type") or "").lower(),
        "title": item.get("Name"),
        "show_title": item.get("SeriesName"),
        "season": item.get("ParentIndexNumber"),
        "episode": item.get("IndexNumber"),
        "year": item.get("ProductionYear"),
        "file": _item_path(item),
        "guid": primary,
        "guids": guids,
        "item_id": item.get("Id"),
    }


# --- Webhook event mapping -------------------------------------------------

_EMBY_FINISHED_EVENTS = {
    "item.markplayed",
    "item.markwatched",
    "library.markplayed",
}
_EMBY_STOPPED_EVENTS = {
    "playback.stop",
    "playback.stopped",
}
_EMBY_PAUSED_EVENTS = {
    "playback.pause",
    "playback.paused",
}
_PLUGIN_FINISHED_TYPES = {
    "itemmarkplayed",
    "userdatasaved",
}
_PLUGIN_PAUSED_TYPES = {"playbackpaused"}
_PLUGIN_STOPPED_TYPES = {"playbackstop", "playbackstopped"}


def _resolve_emby_event_name(payload: dict) -> str:
    # Native Emby notifications use Event; plugin templates often use NotificationType
    return (
        payload.get("Event")
        or payload.get("NotificationType")
        or payload.get("event")
        or ""
    )


def _extract_emby_item(payload: dict) -> dict:
    item = payload.get("Item")
    if isinstance(item, dict):
        return item
    return {}


def _extract_emby_user(payload: dict) -> tuple[str | None, str]:
    user = payload.get("User")
    if isinstance(user, dict):
        return user.get("Id"), (user.get("Name") or "").strip()
    # Plugin / Jellyfin-compatible flat fields
    name = (
        payload.get("NotificationUsername")
        or payload.get("UserName")
        or ""
    )
    return payload.get("UserId"), str(name).strip()


def compute_emby_event_flags(event_name: str, payload: dict | None = None) -> dict:
    """Map Emby (native or plugin) event names to Cleanarr finished/paused/stopped flags."""
    evt = (event_name or "").strip().lower()
    payload = payload or {}

    is_finished = evt in _EMBY_FINISHED_EVENTS or evt in _PLUGIN_FINISHED_TYPES
    is_paused = evt in _EMBY_PAUSED_EVENTS or evt in _PLUGIN_PAUSED_TYPES
    is_stopped = evt in _EMBY_STOPPED_EVENTS or evt in _PLUGIN_STOPPED_TYPES

    # playback.stop only counts as "finished" when Emby reports completion
    if (evt in _EMBY_STOPPED_EVENTS or evt in _PLUGIN_STOPPED_TYPES) and not is_finished:
        playback = payload.get("PlaybackInfo") or payload.get("Playback") or {}
        if isinstance(playback, dict) and playback.get("PlayedToCompletion") is True:
            is_finished = True
        # Some templates put the flag at the top level
        if payload.get("PlayedToCompletion") is True:
            is_finished = True

    return {
        "finished": bool(is_finished),
        "removed": False,
        "paused": bool(is_paused),
        "stopped": bool(is_stopped),
        "actionable": bool(is_finished or is_paused or is_stopped),
        "recorded": bool(is_finished),
    }


def map_emby_webhook_payload(
    payload: dict,
    *,
    remote_addr: str = "",
    method: str = "POST",
    canonical_user: str | None = None,
) -> dict:
    """Normalize an Emby webhook body into the internal Cleanarr event shape."""
    event_name = _resolve_emby_event_name(payload)
    user_id, user_name = _extract_emby_user(payload)
    item = _extract_emby_item(payload)

    # Flat plugin fields fall back when nested Item is absent
    mtype = (
        (item.get("Type") if item else None)
        or payload.get("ItemType")
        or ""
    )
    mtype = str(mtype).lower()

    provider_ids = item.get("ProviderIds") if item else payload.get("ProviderIds")
    primary_guid, _ = _provider_guids(provider_ids if isinstance(provider_ids, dict) else None)

    title = (
        (item.get("Name") if item else None)
        or payload.get("ItemName")
        or payload.get("Name")
        or ""
    )
    series_name = (
        (item.get("SeriesName") if item else None)
        or payload.get("SeriesName")
        or ""
    )
    year = (
        (item.get("ProductionYear") if item else None)
        or payload.get("Year")
        or payload.get("ProductionYear")
    )
    index = (
        (item.get("IndexNumber") if item else None)
        or payload.get("IndexNumber")
        or payload.get("EpisodeNumber")
    )
    parent_index = (
        (item.get("ParentIndexNumber") if item else None)
        or payload.get("ParentIndexNumber")
        or payload.get("SeasonNumber")
    )
    item_id = (item.get("Id") if item else None) or payload.get("ItemId")

    resolved_user = canonical_user if canonical_user is not None else user_name
    media_type = (
        "episode"
        if mtype in ("episode", "series")
        else "movie"
        if mtype == "movie"
        else mtype
    )
    flags = compute_emby_event_flags(event_name, payload)

    return {
        "remote_addr": remote_addr,
        "method": method,
        "platform": "emby",
        "event": event_name,
        "action": "",
        "payload": payload,
        "account": {
            "id": user_id,
            "title": resolved_user,
        },
        "metadata": {
            "guid": primary_guid,
            "ratingKey": item_id,
            "title": html.unescape(str(title or "")),
            "type": media_type,
            "librarySectionTitle": None,
            "sectionTitle": None,
            "parentTitle": html.unescape(str(series_name or "")),
            "index": index,
            "parentIndex": parent_index,
            "year": year,
            "grandparentTitle": html.unescape(str(series_name or "")),
        },
        **flags,
    }
