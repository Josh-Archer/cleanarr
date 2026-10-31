"""Pure cleanup policy decisions.

No network, filesystem, or service-client access. Scheduled jobs and webhook
processing both call these helpers so household protections stay reviewable
in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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


def is_protected_tag_label(label: str) -> bool:
    return normalize_tag_label(label) in PROTECTED_TAG_LABELS


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


def protected_ids_from_tags(tags) -> set:
    """Ids of safe/kids tags. ``tags`` is an Arr tag list."""
    return {
        tag["id"]
        for tag in tags or []
        if is_protected_tag_label(tag.get("label"))
    }


def protected_labels_for_ids(tags, tag_ids) -> list:
    """Protected tag labels (safe/kids) present in the given id set."""
    labels = []
    id_set = set(tag_ids or [])
    for tag in tags or []:
        if tag.get("id") in id_set and is_protected_tag_label(tag.get("label")):
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


def user_tags_from_ids(tags, tag_ids) -> list:
    """Usernames from tag ids, excluding safe/kids protection tags."""
    user_tags = []
    for tag in tags:
        if tag["id"] in tag_ids and not is_protected_tag_label(tag.get("label")):
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
        if normalize_tag_label(str(tag)) and not is_protected_tag_label(str(tag))
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
