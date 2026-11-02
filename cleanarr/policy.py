"""Pure cleanup policy decisions.

No network, filesystem, or service-client access. Scheduled jobs and webhook
processing both call these helpers so household protections stay reviewable
in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from loguru import logger


PROTECTED_TAG_LABELS = frozenset({"safe", "kids"})

MULTI_USER_DELETE_POLICIES = frozenset({
    "require_all_users",
    "majority",
    "primary_user_only",
})

_MULTI_USER_DELETE_POLICY_ALIASES = {
    "require_all": "require_all_users",
    "require-all-users": "require_all_users",
    "all": "require_all_users",
    "all_users": "require_all_users",
    "majority_users": "majority",
    "primary": "primary_user_only",
    "primary-user-only": "primary_user_only",
    "primary_only": "primary_user_only",
    "primary-only": "primary_user_only",
}


def normalize_tag_label(label: str) -> str:
    label = (label or "").strip().lower()
    label = re.sub(r"^\s*\d+\s*-\s*", "", label)
    return label


def is_protected_tag_label(label: str, extra_tags: Iterable[str] | None = None) -> bool:
    normalized = normalize_tag_label(label)
    if normalized in PROTECTED_TAG_LABELS:
        return True
    if extra_tags:
        normalized_extra = {normalize_tag_label(t) for t in extra_tags}
        return normalized in normalized_extra
    return False


def find_tag_by_label(tags, label):
    """Return the first tag whose normalized label equals ``label``.

    ``label`` is compared as given. Callers pass already-normalized names
    such as ``safe`` and ``kids``.
    """
    target = label
    for tag in tags or []:
        if normalize_tag_label(tag.get("label")) == target:
            return tag
    return None


def protected_ids_from_tags(tags, extra_tags: Iterable[str] | None = None) -> set:
    """Ids of safe/kids/exclusion tags. ``tags`` is an Arr tag list."""
    return {
        tag["id"]
        for tag in tags or []
        if is_protected_tag_label(tag.get("label"), extra_tags=extra_tags)
    }


def protected_labels_for_ids(tags, tag_ids, extra_tags: Iterable[str] | None = None) -> list:
    """Protected tag labels (safe/kids/exclusion) present in the given id set."""
    labels = []
    id_set = set(tag_ids or [])
    for tag in tags or []:
        if tag.get("id") in id_set and is_protected_tag_label(tag.get("label"), extra_tags=extra_tags):
            labels.append(normalize_tag_label(tag.get("label")))
    return labels


def ids_include_any_label(tags, item_tag_ids, labels) -> bool:
    """True when any named label is attached to the item.

    Label selection matches the historical per-call checks: movie removal
    paths can pass ``("safe",)`` while finished playback passes safe and kids.
    """
    ids = set(item_tag_ids or [])
    for label in labels:
        tag = find_tag_by_label(tags, label)
        if tag is not None and tag["id"] in ids:
            return True
    return False


def user_tags_from_ids(tags, tag_ids, extra_tags: Iterable[str] | None = None) -> list:
    """Usernames from tag ids, excluding safe/kids and exclusion protection tags."""
    user_tags = []
    for tag in tags or []:
        if tag.get("id") in tag_ids and not is_protected_tag_label(tag.get("label"), extra_tags=extra_tags):
            cleaned_tag = normalize_tag_label(tag.get("label"))
            if cleaned_tag:
                user_tags.append(cleaned_tag)
    return user_tags


def normalize_multi_user_delete_policy(value, *, default="require_all_users"):
    """Normalize policy name; unknown values fall back to the safe default."""
    raw = (value or default or "require_all_users").strip().lower().replace("-", "_")
    candidates = [
        (value or "").strip().lower(),
        raw,
        raw.replace("_", "-"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in MULTI_USER_DELETE_POLICIES:
            return candidate
        aliased = _MULTI_USER_DELETE_POLICY_ALIASES.get(candidate)
        if aliased:
            return aliased
        underscored = candidate.replace("-", "_")
        if underscored in MULTI_USER_DELETE_POLICIES:
            return underscored
        aliased = _MULTI_USER_DELETE_POLICY_ALIASES.get(underscored)
        if aliased:
            return aliased
    logger.warning(
        f"Unknown CLEANARR_MULTI_USER_DELETE_POLICY '{value}'; "
        f"using safe default '{default}'"
    )
    return default if default in MULTI_USER_DELETE_POLICIES else "require_all_users"


def user_names_match(left: str, right: str) -> bool:
    """Exact, case/whitespace-normalized username match (tags vs account names).

    Substring/regex matching let ``ann`` match ``joanna`` and approve deletes
    for users who never watched (cleanarr#79).
    """
    left = (left or "").strip().lower()
    right = (right or "").strip().lower()
    return bool(left) and left == right


def user_in_collection(username: str, collection) -> bool:
    username = (username or "").strip().lower()
    if not username:
        return False
    for entry in collection or []:
        if user_names_match(username, entry):
            return True
    return False


def resolve_household_members(user_tags, watched_by, household_users=None):
    """Resolve the household constituency for multi-user delete decisions.

    Priority:
    1. Explicit household roster (CLEANARR_HOUSEHOLD_USERS)
    2. Sonarr/Radarr/Lidarr user tags on the item
    3. All accounts present in watched_by
    """
    if household_users:
        return sorted({str(user).strip().lower() for user in household_users if str(user).strip()})
    if user_tags:
        return sorted({str(user).strip().lower() for user in user_tags if str(user).strip()})
    return sorted({
        str(user).strip().lower()
        for user in (watched_by or {}).keys()
        if str(user).strip()
    })


def satisfied_household_members(household_members, satisfied_users):
    """Return household members whose watch state is considered satisfied."""
    satisfied = []
    for member in household_members:
        if user_in_collection(member, satisfied_users):
            satisfied.append(member)
    return satisfied


def household_policy_allows_delete(
    household_members,
    satisfied_users,
    *,
    policy="require_all_users",
    primary_user="",
):
    """Apply multi-user delete policy for a household.

    ``satisfied_users`` is the set of usernames that already meet the deletion
    precondition (exact-item watched, or watched-ahead for earlier episodes).

    Returns (allowed: bool, detail: str). Does not read process configuration.
    """
    policy = normalize_multi_user_delete_policy(policy)
    primary_user = (primary_user or "").strip().lower()

    members = [member for member in (household_members or []) if member]
    if not members:
        if satisfied_users:
            return True, "no_household_members_any_watcher"
        return False, "no_household_members_no_watchers"

    satisfied = satisfied_household_members(members, satisfied_users)
    satisfied_count = len(satisfied)
    member_count = len(members)

    if policy == "primary_user_only":
        if not primary_user:
            logger.warning(
                "CLEANARR_MULTI_USER_DELETE_POLICY=primary_user_only but "
                "CLEANARR_PRIMARY_USER is unset; falling back to require_all_users"
            )
            policy = "require_all_users"
        else:
            if user_in_collection(primary_user, satisfied):
                return True, f"primary_user_only primary={primary_user}"
            if user_in_collection(primary_user, satisfied_users):
                return True, f"primary_user_only primary={primary_user}"
            return False, (
                f"primary_user_only primary={primary_user} not satisfied "
                f"(satisfied={satisfied}, household={members})"
            )

    if policy == "majority":
        needed = (member_count // 2) + 1
        if satisfied_count >= needed:
            return True, (
                f"majority {satisfied_count}/{member_count} (needed {needed}) "
                f"satisfied={satisfied}"
            )
        return False, (
            f"majority {satisfied_count}/{member_count} (needed {needed}) "
            f"satisfied={satisfied} household={members}"
        )

    if satisfied_count >= member_count:
        return True, f"require_all_users all {member_count} satisfied={satisfied}"
    return False, (
        f"require_all_users {satisfied_count}/{member_count} "
        f"satisfied={satisfied} household={members}"
    )


def watched_users_from_map(watched_by) -> list:
    """Lowercased usernames whose watch flag is true."""
    return [
        str(user).lower()
        for user, watched in (watched_by or {}).items()
        if watched
    ]


@dataclass(frozen=True)
class DeleteDecision:
    """Exact-item delete decision shared by jobs and webhooks."""

    allowed: bool
    reason: str
    detail: str = ""
    policy: str = "require_all_users"
    household: tuple = ()
    watched_users: tuple = ()
    user_tags: tuple = ()


def decide_exact_item_delete(
    user_tags,
    watched_by,
    *,
    policy="require_all_users",
    primary_user="",
    household_users=None,
) -> DeleteDecision:
    """Decide whether an already-watched episode, movie, or track may be deleted.

    No watchers is always a skip. Otherwise the household policy applies.
    ``isWatched`` fallback evidence is accepted by the caller before this
    decision; watched-ahead inference is a separate helper.
    """
    watched_users = watched_users_from_map(watched_by)
    normalized_policy = normalize_multi_user_delete_policy(policy)
    if not watched_users:
        return DeleteDecision(
            allowed=False,
            reason="not_watched",
            detail="no_watchers",
            policy=normalized_policy,
            household=tuple(resolve_household_members(
                user_tags, watched_by, household_users
            )),
            watched_users=(),
            user_tags=tuple(user_tags or ()),
        )

    household = resolve_household_members(user_tags, watched_by, household_users)
    allowed, detail = household_policy_allows_delete(
        household,
        watched_users,
        policy=normalized_policy,
        primary_user=primary_user,
    )
    return DeleteDecision(
        allowed=allowed,
        reason="household_policy_allows" if allowed else "household_policy_not_satisfied",
        detail=detail,
        policy=normalized_policy,
        household=tuple(household),
        watched_users=tuple(watched_users),
        user_tags=tuple(user_tags or ()),
    )


def decide_tagged_user_delete(
    user_tags,
    watched_by,
    *,
    policy="require_all_users",
    primary_user="",
    extra_protected_tags: Iterable[str] | None = None,
) -> DeleteDecision:
    """Jellyfin/Emby finished-event decision using Plex-user tag rules.

    ``watched_by`` must already be keyed by Plex user names (see the webhook
    Jellyfin-to-Plex user mapping). The household is only the Sonarr/Radarr
    user tags: untagged items never delete, and untagged or unmapped media
    server accounts (hidden users) can neither block nor approve a delete.
    ``safe`` / ``kids`` protection is applied by callers before this and is
    absolute; protected labels never count as users here either.
    """
    normalized_policy = normalize_multi_user_delete_policy(policy)
    tags = sorted({
        normalize_tag_label(str(tag))
        for tag in (user_tags or ())
        if normalize_tag_label(str(tag))
        and not is_protected_tag_label(str(tag), extra_tags=extra_protected_tags)
    })
    watched_users = watched_users_from_map(watched_by)
    if not tags:
        return DeleteDecision(
            allowed=False,
            reason="no_user_tags",
            detail="untagged item; tag rules require a Plex user tag",
            policy=normalized_policy,
            household=(),
            watched_users=tuple(watched_users),
            user_tags=(),
        )
    allowed, detail = household_policy_allows_delete(
        tags,
        watched_users,
        policy=normalized_policy,
        primary_user=primary_user,
    )
    return DeleteDecision(
        allowed=allowed,
        reason="household_policy_allows" if allowed else "household_policy_not_satisfied",
        detail=detail,
        policy=normalized_policy,
        household=tuple(tags),
        watched_users=tuple(watched_users),
        user_tags=tuple(tags),
    )


def collect_watch_ahead_candidates(user_watched, episode_keys):
    """Find episodes a user has skipped while watching at least two ahead.

    ``user_watched`` maps a username to ``(season, episode)`` pairs that have
    real play history (not isWatched fallback). ``episode_keys`` is the Sonarr
    episode key set. A user who has watched the episode itself is not "ahead"
    of it. Returns ``{(season, episode): {"users_ahead", "later_by_user"}}``.
    """
    from collections import defaultdict

    ep_numbers_by_season = defaultdict(list)
    for season, epnum in episode_keys:
        ep_numbers_by_season[season].append(epnum)

    ahead_candidates = {}
    for user, watched_set in (user_watched or {}).items():
        watched_pairs = set(watched_set or [])
        for season, epnums in ep_numbers_by_season.items():
            for epnum in sorted(epnums):
                ahead_eps = sorted(
                    watched_epnum
                    for watched_season, watched_epnum in watched_pairs
                    if watched_season == season and watched_epnum >= epnum + 2
                )
                if not ahead_eps or (season, epnum) in watched_pairs:
                    continue
                key = (season, epnum)
                candidate = ahead_candidates.setdefault(
                    key,
                    {"users_ahead": set(), "later_by_user": {}},
                )
                candidate["users_ahead"].add(str(user).lower())
                candidate["later_by_user"][str(user).lower()] = ahead_eps
    return ahead_candidates


def torrent_cleanup_decision(
    *,
    labels,
    category,
    required_labels,
    allowed_categories,
):
    """Decide whether a torrent may be cleaned up.

    Returns ``(allowed, skip_kind, effective_labels)``. ``skip_kind`` is
    ``"labels"``, ``"category"``, or ``None``. ``effective_labels`` includes
    the download-directory category when it matches a required label, matching
    the Readarr fallback that does not send Transmission labels.
    """
    effective = set(labels or ())
    required = set(required_labels or ())
    allowed = set(allowed_categories or ())
    if required:
        if category and category in required:
            effective.add(category)
        if not effective.intersection(required):
            return False, "labels", effective
    if not allowed:
        return True, None, effective
    if category in allowed:
        return True, None, effective
    return False, "category", effective


def repeated_io_error_disposition(*, count, threshold, active, dry_run) -> str:
    """What to do with a torrent that has repeated storage I/O errors.

    Returns ``leave_active``, ``below_threshold``, ``dry_run``, or ``remove``.
    Active transfers are never removed. Dry-run never removes.
    """
    if active:
        return "leave_active"
    if count < threshold:
        return "below_threshold"
    if dry_run:
        return "dry_run"
    return "remove"


def is_seerr_request_eligible(
    request: dict,
    *,
    window_days: int = 30,
    protected_users: Iterable[str] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Check if a Seerr request protects media.

    Returns (is_eligible: bool, reason_detail: str).
    """
    if not isinstance(request, dict):
        return False, "invalid_request"

    # Status: 1 = Pending Approval, 2 = Approved, 3 = Declined
    status = request.get("status")
    if status is None:
        return False, "missing_status"
    if status == 3 or str(status).strip().lower() in {"declined", "3"}:
        return False, "declined"
    if str(status).strip().lower() not in {"1", "2", "pending", "approved"}:
        return False, f"invalid_status:{status}"

    requested_by = request.get("requestedBy") or {}
    requester_candidates = [
        requested_by.get("username"),
        requested_by.get("displayName"),
        requested_by.get("plexUsername"),
        requested_by.get("email"),
    ]
    requester_display = (
        requested_by.get("username")
        or requested_by.get("displayName")
        or requested_by.get("plexUsername")
        or requested_by.get("email")
        or "unknown"
    )

    # 1. Protected user check
    protected_set = {
        str(u).strip().lower()
        for u in (protected_users or ())
        if str(u).strip()
    }
    for candidate in requester_candidates:
        if candidate and str(candidate).strip().lower() in protected_set:
            return True, f"protected_user:{candidate}"

    # 2. Window days check
    if window_days is None or int(window_days) <= 0:
        return True, f"unlimited_window_by_{requester_display}"

    created_at_raw = request.get("createdAt")
    if not created_at_raw:
        return True, f"within_window:active_by_{requester_display}"

    try:
        raw_str = str(created_at_raw).strip()
        if raw_str.endswith("Z"):
            raw_str = raw_str[:-1] + "+00:00"
        created_dt = datetime.fromisoformat(raw_str)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        current_time = now or datetime.now(timezone.utc)
        age_seconds = (current_time - created_dt).total_seconds()
        # Allow minor clock skew up to 1 hour
        if -3600 <= age_seconds <= float(window_days) * 86400.0:
            age_days = max(0, int(age_seconds / 86400.0))
            return True, f"within_window:{age_days}d_by_{requester_display}"
    except Exception:
        pass

    return False, "outside_window_or_unprotected"


def seerr_request_matches_movie(request: dict, radarr_movie: dict) -> bool:
    """Check if a Seerr request matches a Radarr movie."""
    if not isinstance(request, dict) or not isinstance(radarr_movie, dict):
        return False
    media = request.get("media") or {}
    media_type = str(media.get("mediaType") or request.get("type") or "").strip().lower()
    if media_type and media_type not in {"movie", "1"}:
        return False

    req_tmdb = media.get("tmdbId")
    movie_tmdb = radarr_movie.get("tmdbId")
    if req_tmdb and movie_tmdb and str(req_tmdb).strip() == str(movie_tmdb).strip():
        return True

    req_imdb = str(media.get("imdbId") or "").strip().lower()
    movie_imdb = str(radarr_movie.get("imdbId") or "").strip().lower()
    if req_imdb and movie_imdb:
        if req_imdb == movie_imdb or req_imdb.lstrip("t") == movie_imdb.lstrip("t"):
            return True

    req_tvdb = media.get("tvdbId")
    movie_tvdb = radarr_movie.get("tvdbId")
    if req_tvdb and movie_tvdb and str(req_tvdb).strip() == str(movie_tvdb).strip():
        return True

    req_title = media.get("title") or (request.get("movie") or {}).get("title") or request.get("title")
    movie_title = radarr_movie.get("title")
    if req_title and movie_title:
        norm_req = re.sub(r"[^a-z0-9]", "", str(req_title).strip().lower())
        norm_movie = re.sub(r"[^a-z0-9]", "", str(movie_title).strip().lower())
        if norm_req and norm_req == norm_movie:
            req_year = media.get("year") or request.get("year")
            if not req_year:
                rel_date = (
                    (request.get("movie") or {}).get("releaseDate")
                    or (request.get("media") or {}).get("releaseDate")
                )
                if rel_date and len(str(rel_date)) >= 4:
                    req_year = str(rel_date)[:4]
            movie_year = radarr_movie.get("year")
            if req_year and movie_year:
                if str(req_year).strip() == str(movie_year).strip():
                    return True
            elif not req_year or not movie_year:
                return True

    return False


def seerr_request_matches_episode(
    request: dict,
    sonarr_series: dict,
    sonarr_episode: dict | None = None,
) -> bool:
    """Check if a Seerr request matches a Sonarr series/episode."""
    if not isinstance(request, dict) or not isinstance(sonarr_series, dict):
        return False
    media = request.get("media") or {}
    media_type = str(media.get("mediaType") or "").strip().lower()
    if media_type and media_type not in {"tv", "series"}:
        return False

    matched_series = False
    req_tvdb = media.get("tvdbId")
    series_tvdb = sonarr_series.get("tvdbId")
    if req_tvdb and series_tvdb and str(req_tvdb).strip() == str(series_tvdb).strip():
        matched_series = True

    if not matched_series:
        req_tmdb = media.get("tmdbId")
        series_tmdb = sonarr_series.get("tmdbId")
        if req_tmdb and series_tmdb and str(req_tmdb).strip() == str(series_tmdb).strip():
            matched_series = True

    if not matched_series:
        req_imdb = media.get("imdbId")
        series_imdb = sonarr_series.get("imdbId")
        if req_imdb and series_imdb and str(req_imdb).strip().lower() == str(series_imdb).strip().lower():
            matched_series = True

    if not matched_series:
        req_title = media.get("title") or request.get("title")
        series_title = sonarr_series.get("title")
        if req_title and series_title:
            norm_req = re.sub(r"[^a-z0-9]", "", str(req_title).strip().lower())
            norm_series = re.sub(r"[^a-z0-9]", "", str(series_title).strip().lower())
            if norm_req and norm_req == norm_series:
                matched_series = True

    if not matched_series:
        return False

    # Season matching
    if sonarr_episode is not None:
        seasons = request.get("seasons")
        if seasons:
            requested_seasons = set()
            for s in seasons:
                if isinstance(s, dict):
                    num = s.get("seasonNumber")
                else:
                    num = s
                if num is not None:
                    try:
                        requested_seasons.add(int(num))
                    except (ValueError, TypeError):
                        pass
            ep_season = sonarr_episode.get("seasonNumber")
            if ep_season is not None and requested_seasons:
                try:
                    if int(ep_season) not in requested_seasons:
                        return False
                except (ValueError, TypeError):
                    pass

    return True
