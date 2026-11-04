"""Jellyfin and Emby watched-state adapter.

Jellyfin and Emby share the same REST shape used here (``X-Emby-Token`` and
``/Users/{id}/Items?IsPlayed=true``). Job mode selects this source with
``CLEANARR_MEDIA_SOURCE=jellyfin`` or ``CLEANARR_MEDIA_SOURCE=emby``. Webhook
payload mapping stays in the webhook app; this adapter only loads library watch
state.
"""

from __future__ import annotations

from loguru import logger

from cleanarr import emby_client


class JellyfinMediaSource:
    """Read watched movies and episodes from a Jellyfin or Emby server."""

    def __init__(self, session, config):
        self.session = session
        # Same dict as CONFIG["jellyfin"] or CONFIG["emby"] so later env/test patches stay visible.
        self.config = config

    def load_watched_state(self):
        """Return ``(movies, episodes)`` in the cleanup record shape."""
        config = self.config or {}
        allowed = config.get("users") or set()
        return emby_client.load_watched_state(
            self.session,
            config.get("baseurl") or "",
            config.get("apikey") or "",
            allowed_usernames=allowed or None,
        )

    def sync_leaving_soon_collection(self, name, item_ids, dry_run=False):
        """Synchronize a named collection (BoxSet) with pending deletions.

        Mirrors ``PlexMediaSource.sync_leaving_soon_collection``: the
        collection is created when missing, items in the grace window are
        added, and items that are no longer pending are removed. Unlike
        Plex, a Jellyfin/Emby collection can mix media types, so a single
        collection holds every pending item. Dry run only logs the
        intended sync and sends no mutating requests.
        """
        config = self.config or {}
        baseurl = config.get("baseurl") or ""
        apikey = config.get("apikey") or ""
        desired = {str(item_id) for item_id in item_ids if item_id}
        existing = next(
            (
                collection
                for collection in emby_client.list_collections(self.session, baseurl, apikey)
                if collection.get("Name") == name
            ),
            None,
        )
        if existing is None and not desired:
            return
        if dry_run:
            logger.info(
                f"[DRY RUN] Would sync Leaving Soon collection '{name}' on Jellyfin/Emby "
                f"with {len(desired)} item(s)"
            )
            return
        if existing is None:
            emby_client.emby_post(
                self.session,
                baseurl,
                apikey,
                "Collections",
                params={"Name": name, "Ids": ",".join(sorted(desired))},
            )
            logger.info(
                f"Created Leaving Soon collection '{name}' on Jellyfin/Emby "
                f"with {len(desired)} item(s)"
            )
            return
        collection_id = str(existing.get("Id"))
        present = {
            str(item.get("Id"))
            for item in emby_client.get_collection_items(
                self.session, baseurl, apikey, collection_id
            )
            if item.get("Id")
        }
        remove_ids = sorted(present - desired)
        if remove_ids:
            emby_client.emby_delete(
                self.session,
                baseurl,
                apikey,
                f"Collections/{collection_id}/Items",
                params={"Ids": ",".join(remove_ids)},
            )
        add_ids = sorted(desired - present)
        if add_ids:
            emby_client.emby_post(
                self.session,
                baseurl,
                apikey,
                f"Collections/{collection_id}/Items",
                params={"Ids": ",".join(add_ids)},
            )
