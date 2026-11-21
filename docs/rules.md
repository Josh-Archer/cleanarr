# Rule-based cleanup policies

Cleanarr can evaluate a declarative ruleset and report what each rule would do. Evaluation calls the pure helpers in `cleanarr/policy.py` (household watch policy, protected tags, and Seerr eligibility). It does not delete, unmonitor, or contact Plex, Jellyfin, Sonarr, Radarr, or Seerr.

Live scheduled jobs and webhooks still delete through those same policy helpers directly. A ruleset does not change that path. Grace period and Leaving Soon still apply only after the runtime has already decided to delete.

Use this when you want Maintainerr-style composition: tags, who watched, age, ratings, request source, and disk free space, checked before any delete.

## Load a ruleset

```python
from cleanarr.rules import (
    default_ruleset,
    default_ruleset_from_config,
    evaluate_rules_dry_run,
    ruleset_from_env,
    write_rule_dry_run_artifacts,
)

rules = ruleset_from_env(settings=config)  # or default_ruleset()
report = evaluate_rules_dry_run(facts, rules)
write_rule_dry_run_artifacts(report, report_dir="/logs/dry-run-reports")
```

| Variable | Default | Role |
| --- | --- | --- |
| `CLEANARR_RULES_JSON` | unset | Inline ruleset JSON. Used when set, including when a file is also set. |
| `CLEANARR_RULES_FILE` | unset | Path to a ruleset JSON file. |

`ruleset_from_env` reads those variables. When both are unset it builds `default_ruleset()` (stock behavior) or `default_ruleset_from_config(settings)` when a CONFIG-shaped mapping is passed. The cleanup process does not read these variables on its own.

## How a ruleset is evaluated

Rules run in ascending `priority`. Equal priorities keep document order. Every enabled rule whose media type matches is evaluated, so the report can show rules that would have matched. The first match decides the item. A later match is `shadowed` and does not change the outcome.

| Item decision | Meaning |
| --- | --- |
| `would_keep` | The first matching rule has action `keep`. |
| `would_delete` | The first matching rule has action `delete`. Nothing is deleted. |
| `no_match` | No rule matched. This does not delete. |
| `would_leave_soon` | No rule decided the item, and a `delete` rule is held back only by a `disk` threshold that free space is approaching. Nothing is deleted. See [Condition `disk`](#condition-disk). |
| `error` | A rule raised while evaluating, a disk source could not be read, or the fact was not an object. This does not delete. |

Missing age, rating, watch, request, or disk evidence does not match. A condition that cannot prove its claim fails closed.

`match` combines conditions inside one rule:

- `all` (default): every condition must match. The report reason lists the failing conditions, or all of them when the rule matches.
- `any`: one condition must match. The report reason lists the matching conditions, or all of them when none match.

Limits: 128 rules, 32 conditions per rule. Unknown keys, unknown types, and unknown policy names are schema errors. They are not silently ignored. Policy aliases such as `all` and `primary-only` are accepted and stored in canonical form (`require_all_users`, `primary_user_only`).

## Schema version 1

A ruleset is a JSON object. A bare list of rules is accepted and treated as version 1 with the name `custom`.

| Field | Required | Meaning |
| --- | --- | --- |
| `version` | no | Integer `1`. Omitted means 1. Any other version is rejected. |
| `name` | no | Short label. Default `custom`. The built-in document is named `cleanarr-defaults`. |
| `description` | no | Optional text, at most 400 characters. |
| `rules` | yes | List of rule objects. May be empty, which never deletes. |

### Rule

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable id: `[a-z][a-z0-9_-]{0,63}`. Unique in the ruleset. |
| `name` | no | Human label. Defaults to `id`. |
| `description` | no | Optional text, at most 400 characters. |
| `enabled` | no | Boolean. Default `true`. Disabled rules are listed in the report and do not match. |
| `priority` | no | Integer from -10000 to 10000. Default `100`. Lower runs first. |
| `action` | yes | `keep` or `delete`. |
| `media_types` | no | List of `any`, `movie`, `episode`, `series`, `season`, `track`. Default `["any"]`. `any` cannot be combined with other types. |
| `match` | no | `all` or `any`. Default `all`. |
| `conditions` | yes | Non-empty list. See below. |

### Condition `tag`

| Field | Meaning |
| --- | --- |
| `op` | `has_any`, `has_all`, or `has_none`. |
| `labels` | Tag labels. Compared with the same normalization as Sonarr/Radarr tags (`10 - safe` is `safe`). |

### Condition `watched`

Uses `decide_exact_item_delete`, `decide_tagged_user_delete`, or `household_policy_allows_delete`.

| Field | Meaning |
| --- | --- |
| `scope` | `exact`, `watched_ahead`, or `tag_rules`. |
| `policy` | `require_all_users` (default), `majority`, or `primary_user_only`, plus the aliases accepted by `cleanarr.policy`. |
| `primary_user` | Optional. Used for `primary_user_only`. When omitted, the fact's `primary_user` is used. An empty primary user follows the policy helper and falls back to `require_all_users`. |

`exact` is an exact watch of the item (history or `isWatched` evidence already folded into `watched_by` by the caller). No watchers does not match.

`tag_rules` is the Jellyfin finished-event rule: the household is the user tags, untagged items do not match, and accounts that are not tagged neither block nor approve.

`watched_ahead` applies the household policy to `users_ahead`. The two-episode gap is not recomputed here. Call `collect_watch_ahead_candidates` and pass the resulting usernames. An empty `users_ahead` does not match.

### Condition `age`

| Field | Meaning |
| --- | --- |
| `field` | `added`, `aired`, `watched`, or `requested`. |
| `op` | `older_than` or `newer_than`. |
| `days` | Number greater than or equal to 0. |

`older_than` matches when the age in days is greater than or equal to `days`. `newer_than` matches when the age is strictly less. Together they cover every known age exactly once. A missing timestamp or `age_days` entry does not match.

### Condition `rating`

| Field | Meaning |
| --- | --- |
| `source` | `imdb`, `tmdb`, `rt`, `audience`, or `any`. |
| `op` | `lt`, `lte`, `gt`, `gte`, or `eq`. |
| `value` | Number. `eq` allows a difference of at most `1e-6`. |

`any` matches when any present source satisfies the comparison. Sources are checked in alphabetical order. A missing rating does not match.

### Condition `request`

| Field | Meaning |
| --- | --- |
| `source` | `seerr`. |
| `op` | `protected`, `unprotected`, or `requested_by`. |
| `window_days` | Integer greater than or equal to 0. Default `30`. `0` means no age limit, matching `is_seerr_request_eligible`. |
| `users` | Protected requesters for `protected` and `unprotected`. Required and non-empty for `requested_by`. |

`protected` matches an eligible pending or approved request for this item, inside the window or from one of `users`. `unprotected` matches only when the item was checked and no such request exists. `requested_by` matches when an eligible request was made by one of `users` (username, display name, Plex username, or email).

The fact must include `arr_item` (and `episode_item` when a season filter matters) unless `requests_matched` is true. Without an identity, the condition does not match. If `requests_unavailable` is true, `protected` matches (`seerr_unreachable`) and the other two ops do not. That is the same fail-closed behavior as Seerr protection in the cleanup runtime, and it only applies when this rule is enabled.

### Condition `watched_by`

| Field | Meaning |
| --- | --- |
| `op` | `any`, `all`, or `none`. |
| `users` | Usernames. Matching is exact and case-insensitive, same as household user matching. |

This tests specific accounts in `watched_by`. It does not apply the household policy. Use `watched` for that.

### Condition `disk`

Matches when the disk holding the item is low on free space. Use it to gate age rules so cleanup gets more aggressive as the disk fills and does nothing while there is room.

| Field | Meaning |
| --- | --- |
| `source` | `radarr`, `sonarr`, or `any`. Default `any`. Which Arr's disk report to read. |
| `op` | `free_percent_below` or `free_gb_below`. Both are strict: free space equal to `value` does not match. |
| `value` | Number greater than or equal to 0. A percentage (at most 100) or a size in GB (1024³ bytes, the unit the Arr UI shows). |
| `path` | Optional. Measure the disk holding this path instead of the item's root folder. |
| `leaving_soon_offset_percent` | Optional number from 0 to 100. Only allowed on `delete` rules. See below. |

Free space comes from the fact's `disk` entry, which the caller reads from `GET /api/v3/diskspace` on Radarr or Sonarr. The evaluator does not fetch it, and the job needs no filesystem mount.

The disk entry used is the one whose path is the longest prefix of the item's root folder, compared by whole path segments. `/media/movies` is covered by `/media` and not by `/med`. The root folder is `path` on the condition, then the fact's `root_folder`, then `arr_item.rootFolderPath`, then `arr_item.path`. With `any`, entries from both Arrs are candidates. If two entries share the winning path, the one with more free space is used.

The condition does not match when the fact has no `disk` entry for the source, when no root folder is known, or when no disk entry covers the path.

A disk source that could not be read never deletes. If the fact records an `error` for a source the condition reads, the rule's status is `error` and the item decision is `error`. `any` reads both sources, so one failed source is enough. That matches how the cleanup runtime skips work when Radarr or Sonarr is unreachable.

#### Leaving Soon offset

`leaving_soon_offset_percent` widens a threshold so the report can show what is about to be deleted before the threshold is crossed. With `free_percent_below` 10 and an offset of 2, free space from 10% up to (not including) 12% is inside the band. For `free_gb_below` the offset is that percentage of the disk's total size, added to `value`.

A `delete` rule whose other conditions all match and whose disk conditions are inside the band gets the status `leaving_soon`. It is not a match. With `match: any`, a rule that matches nothing is `leaving_soon` when at least one disk condition is inside the band.

The first `leaving_soon` rule decides the item as `would_leave_soon` unless something decides it first:

- A rule that matched earlier wins as usual.
- A later `delete` rule that fully matches wins, as long as no `keep` rule matched in between. The item is already deletable under that rule.
- A later `keep` rule does not win. Once the threshold is crossed the earlier `delete` rule will outrank it, so the item is leaving soon.

`would_leave_soon` never deletes.

## Facts

A fact is a plain object the caller already gathered. The evaluator does not fetch it.

| Field | Meaning |
| --- | --- |
| `media_type` | `movie`, `episode`, `series`, `season`, `track`, or another string. Unknown types only match rules that use `any`. |
| `title` or `media_title` | Label in the report. |
| `media_id` | Optional string or integer copied into the report. |
| `tags` | Tag labels or `{label}` objects. Normalized the same way as Arr tags. |
| `user_tags` | Optional usernames already stripped of `safe`, `kids`, and exclusion tags. When omitted, they are derived from `tags`. When present, even as an empty list, they are used as given. |
| `exclusion_tags` | Used only when deriving `user_tags`, and as extra protected labels for `tag_rules`. The keep-tag rule uses the labels stored on the rule. |
| `watched_by` | Map of username to watched boolean. |
| `household_users` | Optional roster. Same resolution order as `resolve_household_members`: this roster, then user tags, then watched accounts. |
| `primary_user` | Used when a `watched` condition omits `primary_user`. |
| `users_ahead` | Usernames already known to be watched ahead of an episode. |
| `age_days` | Optional map of `added`, `aired`, `watched`, `requested` to a number of days. |
| `added_at`, `aired_at`, `last_watched_at`, `requested_at` | ISO-8601 timestamps, used when `age_days` has no entry. Compared with the `now` argument of `evaluate_rules_dry_run`. |
| `ratings` | Map of `imdb`, `tmdb`, `rt`, `audience` to numbers. |
| `requests` | Seerr request objects already fetched by the caller. |
| `arr_item` | Radarr movie or Sonarr series dict used to match requests. |
| `episode_item` | Optional Sonarr episode dict for season filters. |
| `requests_matched` | When true, `requests` are already for this item and are not matched again. |
| `requests_unavailable` | Seerr was enabled but the request list could not be loaded. |
| `disk` | Map of `radarr` and/or `sonarr` to `{"entries": [{"path", "free_bytes", "total_bytes"}]}`, or to `{"error": "..."}` when the fetch failed. Leave a source out when it was not fetched. |
| `root_folder` | Optional root folder path of the item. When omitted, `arr_item.rootFolderPath` and then `arr_item.path` are used. |

### Gathering disk facts

`cleanarr.disk` reads free space for the `disk` condition:

```python
from cleanarr.disk import attach_disk_facts, fetch_disk_facts

disk_facts = fetch_disk_facts({"radarr": radarr_client, "sonarr": sonarr_client})
facts = [attach_disk_facts(fact, disk_facts) for fact in facts]
```

`fetch_disk_facts` calls `diskspace` once per source and records a failed or malformed response as `{"error": ...}`. Call it once per run and reuse the result for every item. `attach_disk_facts` copies the result onto a fact and fills `root_folder` from `arr_item`. Inside the cleanup runtime, `MediaCleanup.get_disk_facts()` does the fetch and caches it for the run.

Example:

```json
{
  "media_type": "movie",
  "title": "Dune",
  "tags": ["alice"],
  "user_tags": ["alice"],
  "watched_by": {"alice": true},
  "age_days": {"added": 40},
  "ratings": {"imdb": 5.5},
  "arr_item": {"title": "Dune", "tmdbId": 438631},
  "requests": []
}
```

## Built-in default rules

`default_ruleset()` encodes the behavior operators already have:

1. `protect-tags` (keep, priority 10). Keep `safe`, `kids`, and the exclusion tags (default `keep`).
2. `protect-seerr` (keep, priority 20). Keep an eligible Seerr request. Present but disabled unless Seerr protection is enabled. Unreachable Seerr keeps the item when the rule is enabled.
3. `delete-household-watched` (delete, priority 100). Delete a movie, episode, or track when the household watch policy passes for an exact watch.
4. `delete-watched-ahead` (delete, priority 110). Delete an episode when `users_ahead` satisfies that same policy.

`default_ruleset_from_config(config)` copies `multi_user_delete_policy`, `primary_user`, `exclusion_tags`, and `seerr.enabled` / `window_days` / `protected_users` onto those rules.

`default_ruleset(watch_scope="tag_rules")` points the delete rule at Jellyfin tag rules and disables watched-ahead. Untagged items do not delete.

With no protecting tag and no Seerr hit, `would_delete` matches `decide_exact_item_delete` for the same tags, watch map, policy, primary user, and household. Watched-ahead matches `household_policy_allows_delete` on `users_ahead`. Tag scope matches `decide_tagged_user_delete`.

Torrent label and category checks stay on `torrent_cleanup_decision`. They are not rules in this schema.

<!-- default-ruleset:start -->
```json
{
  "version": 1,
  "name": "cleanarr-defaults",
  "description": "Stock Cleanarr policy: protected tags, optional Seerr requests, household watch status, and watched-ahead episodes.",
  "rules": [
    {
      "id": "protect-tags",
      "name": "Keep safe, kids, and exclusion tags",
      "description": "Keep media tagged safe, kids, or an exclusion tag.",
      "enabled": true,
      "priority": 10,
      "action": "keep",
      "media_types": ["any"],
      "match": "all",
      "conditions": [
        {"type": "tag", "op": "has_any", "labels": ["keep", "kids", "safe"]}
      ]
    },
    {
      "id": "protect-seerr",
      "name": "Keep media covered by an eligible Seerr request",
      "description": "Keep a matching pending or approved Seerr request inside the window, or from a protected requester. Disabled unless Seerr protection is enabled. Unreachable Seerr matches this keep rule.",
      "enabled": false,
      "priority": 20,
      "action": "keep",
      "media_types": ["movie", "episode", "series"],
      "match": "all",
      "conditions": [
        {
          "type": "request",
          "source": "seerr",
          "op": "protected",
          "window_days": 30,
          "users": []
        }
      ]
    },
    {
      "id": "delete-household-watched",
      "name": "Delete when the household watch policy is satisfied",
      "description": "Delete when every required watcher has an exact watch.",
      "enabled": true,
      "priority": 100,
      "action": "delete",
      "media_types": ["movie", "episode", "track"],
      "match": "all",
      "conditions": [
        {"type": "watched", "scope": "exact", "policy": "require_all_users"}
      ]
    },
    {
      "id": "delete-watched-ahead",
      "name": "Delete earlier episodes when the household is watched ahead",
      "description": "Delete an episode when watched-ahead viewers satisfy the same household policy. History gap checks stay with the caller, which passes users_ahead.",
      "enabled": true,
      "priority": 110,
      "action": "delete",
      "media_types": ["episode"],
      "match": "all",
      "conditions": [
        {"type": "watched", "scope": "watched_ahead", "policy": "require_all_users"}
      ]
    }
  ]
}
```
<!-- default-ruleset:end -->

## Example: old, low-rated movies

Keep a movie Alice requested through Seerr. Otherwise delete it when it has been in the library at least 30 days, its IMDb rating is below 6, and the household watch policy passes.

<!-- example-ruleset:start -->
```json
{
  "version": 1,
  "name": "low-rated-old-movies",
  "rules": [
    {
      "id": "keep-requested",
      "name": "Keep Seerr requests from alice",
      "priority": 10,
      "action": "keep",
      "media_types": ["movie"],
      "match": "all",
      "conditions": [
        {
          "type": "request",
          "source": "seerr",
          "op": "requested_by",
          "users": ["alice"],
          "window_days": 30
        }
      ]
    },
    {
      "id": "delete-old-low-rated",
      "name": "Delete old, low-rated movies the household has watched",
      "priority": 100,
      "action": "delete",
      "media_types": ["movie"],
      "match": "all",
      "conditions": [
        {"type": "age", "field": "added", "op": "older_than", "days": 30},
        {"type": "rating", "source": "imdb", "op": "lt", "value": 6},
        {"type": "watched", "scope": "exact", "policy": "require_all_users"}
      ]
    }
  ]
}
```
<!-- example-ruleset:end -->

## Example: tiered cleanup by disk pressure

Delete nothing while the movie disk has room. Below 10% free, delete watched movies added at least 30 days ago. Below 5% free, drop that to 15 days. Movies enter Leaving Soon when free space is within 2 points of a tier.

<!-- disk-tiers-ruleset:start -->
```json
{
  "version": 1,
  "name": "disk-pressure-tiers",
  "rules": [
    {
      "id": "protect-tags",
      "priority": 10,
      "action": "keep",
      "conditions": [
        {"type": "tag", "op": "has_any", "labels": ["keep", "kids", "safe"]}
      ]
    },
    {
      "id": "delete-critical-disk",
      "name": "Below 5% free: delete watched movies older than 15 days",
      "priority": 100,
      "action": "delete",
      "media_types": ["movie"],
      "conditions": [
        {
          "type": "disk",
          "source": "radarr",
          "op": "free_percent_below",
          "value": 5,
          "leaving_soon_offset_percent": 2
        },
        {"type": "age", "field": "added", "op": "older_than", "days": 15},
        {"type": "watched", "scope": "exact", "policy": "require_all_users"}
      ]
    },
    {
      "id": "delete-low-disk",
      "name": "Below 10% free: delete watched movies older than 30 days",
      "priority": 110,
      "action": "delete",
      "media_types": ["movie"],
      "conditions": [
        {
          "type": "disk",
          "source": "radarr",
          "op": "free_percent_below",
          "value": 10,
          "leaving_soon_offset_percent": 2
        },
        {"type": "age", "field": "added", "op": "older_than", "days": 30},
        {"type": "watched", "scope": "exact", "policy": "require_all_users"}
      ]
    }
  ]
}
```
<!-- disk-tiers-ruleset:end -->

For a watched movie on the Radarr disk:

| Free space | Added 20 days ago | Added 40 days ago |
| --- | --- | --- |
| 20% | `no_match` | `no_match` |
| 11% | `no_match` | `would_leave_soon` (`delete-low-disk`) |
| 8% | `no_match` | `would_delete` (`delete-low-disk`) |
| 6% | `would_leave_soon` (`delete-critical-disk`) | `would_delete` (`delete-low-disk`) |
| 4% | `would_delete` (`delete-critical-disk`) | `would_delete` (`delete-critical-disk`) |
| Radarr unreachable | `error` | `error` |

## Dry-run report

`evaluate_rules_dry_run(facts, ruleset)` returns schema version 1. `mode` is always `dry-run`. `write_rule_dry_run_artifacts` writes:

| File | Purpose |
| --- | --- |
| `cleanarr-rule-dry-run-report.json` | Machine-readable report. |
| `cleanarr-rule-dry-run-report.md` | The same report, grouped under a heading per rule. |

`summary` counts items that `would_delete`, `would_keep`, `would_leave_soon`, `no_match`, or `error`. `summary.per_rule` and `rules[]` repeat that picture for each rule: how many items matched, how many were decisive, how many were shadowed, and how many would be kept, deleted, or moved to Leaving Soon by that rule. Each rule lists every evaluated item with `status` (`matched`, `shadowed`, `not_matched`, `leaving_soon`, `disabled`, `media_type_mismatch`, or `error`) and the condition reason.

`items[]` is the same information grouped by media item, including `decisive_rule_id`. Sensitive values are redacted with the same rules as the existing decision reports.

Pass `now` to freeze age and Seerr window checks. Pass `generated_at` when a test or caller needs a stable timestamp. A failed write logs a warning and returns no paths, same as the other dry-run artifacts.
