"""Plex library adapter.

Reads watch state and removes watchlist entries. Household delete policy is
applied by the caller in ``cleanarr.policy``.
"""

from __future__ import annotations

from loguru import logger


class PlexMediaSource:
    """PlexServer wrapper for watched movies, episodes, and watchlist removal."""

    def __init__(self, server, evidence_by_rating_key):
        self.server = server
        # Shared with MediaCleanup so webhook and job reads see the same map.
        self.evidence = evidence_by_rating_key

    def list_watched_movies(self):
        """Return watched movies from every Plex movie library section."""
        logger.info("Checking for watched movies in Plex")
        watched_movies = []
        try:
            movie_sections = [
                section for section in self.server.library.sections() if section.type == "movie"
            ]
            for section in movie_sections:
                for movie in section.search(unwatched=False):
                    watched_by = self.watch_status(movie)
                    guid_values = []
                    for guid in getattr(movie, "guids", None) or []:
                        value = getattr(guid, "id", guid)
                        if value:
                            guid_values.append(str(value))
                    watched_movies.append({
                        "title": movie.title,
                        "year": movie.year,
                        "file": movie.locations[0] if movie.locations else None,
                        "watched_by": watched_by,
                        "watch_evidence": self.evidence.get(movie.ratingKey, {}).copy(),
                        "guid": movie.guid,
                        "guids": guid_values,
                        "rating_key": movie.ratingKey,
                    })
            logger.info(f"Found {len(watched_movies)} watched movies")
            return watched_movies
        except Exception as exc:
            logger.error(f"Error getting watched movies: {str(exc)}")
            return []

    def list_watched_episodes(self):
        """Return watched episodes from every Plex show library section."""
        logger.info("Checking for watched episodes in Plex")
        watched_episodes = []
        try:
            all_sections = self.server.library.sections()
            logger.debug(
                f"Found {len(all_sections)} library sections: {[s.title for s in all_sections]}"
            )
            show_sections = [section for section in all_sections if section.type == "show"]
            logger.info(f"Found {len(show_sections)} TV show sections: {[s.title for s in show_sections]}")

            for section in show_sections:
                shows = section.all()
                logger.debug(f"Section '{section.title}' has {len(shows)} shows")
                for show in shows:
                    episodes = show.episodes()

                    if "metal" in show.title.lower() or "alchemist" in show.title.lower():
                        logger.info(f"DEBUG: Found show '{show.title}' with {len(episodes)} episodes")
                        for ep in episodes[:10]:
                            logger.info(
                                f"DEBUG: {show.title} S{ep.seasonNumber}E{ep.index} '{ep.title}' "
                                f"- isWatched={ep.isWatched}, viewCount={getattr(ep, 'viewCount', 'N/A')}"
                            )

                    watched_count = sum(1 for ep in episodes if ep.isWatched)
                    if watched_count > 0:
                        logger.debug(
                            f"Show '{show.title}' has {watched_count}/{len(episodes)} watched episodes"
                        )
                    for episode in episodes:
                        if episode.isWatched:
                            watch_status = self.watch_status(episode)
                            episode_data = {
                                "show_title": show.title,
                                "season": episode.seasonNumber,
                                "episode": episode.index,
                                "title": episode.title,
                                "file": episode.locations[0] if episode.locations else None,
                                "watched_by": watch_status,
                                "watch_evidence": self.evidence.get(episode.ratingKey, {}).copy(),
                                "guid": episode.guid,
                                "rating_key": episode.ratingKey,
                            }
                            if not any(watch_status.values()):
                                episode_data["is_watched_override"] = True
                                logger.debug(
                                    f"Episode {show.title} S{episode.seasonNumber}E{episode.index} "
                                    "marked as watched but no user history found"
                                )
                            watched_episodes.append(episode_data)
            logger.info(f"Found {len(watched_episodes)} watched episodes")
            return watched_episodes
        except Exception as exc:
            logger.error(f"Error getting watched episodes: {str(exc)}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    def watch_status(self, media_item):
        """Per-account watch flags for one Plex item. History wins; owner may fall back to isWatched."""
        watch_status = {}
        try:
            rating_key = getattr(media_item, "ratingKey", None)
            watch_evidence = {}
            account = self.server.myPlexAccount()
            users = account.users()

            owner_username = account.username or account.title

            history_kwargs = {"ratingKey": media_item.ratingKey, "accountID": 1}
            history = self.server.history(**history_kwargs)

            is_watched = bool(history)
            if not is_watched and hasattr(media_item, "isWatched"):
                is_watched = media_item.isWatched
                if is_watched:
                    watch_evidence[owner_username] = "isWatched_fallback"
                    logger.debug(
                        f"Owner '{owner_username}' marked as watched via isWatched fallback "
                        "(history was empty)"
                    )
            if owner_username not in watch_evidence:
                watch_evidence[owner_username] = "history" if history else "not_watched"

            watch_status[owner_username] = is_watched
            logger.debug(
                f"Watch status for owner '{owner_username}' (accountID=1): {is_watched}, "
                f"history items: {len(history)}, evidence={watch_evidence[owner_username]}"
            )

            for user in users:
                username = user.username or user.title
                user_id = getattr(user, "id", None)
                logger.debug(f"Checking user '{username}' with id={user_id}")
                history_kwargs = {"ratingKey": media_item.ratingKey}
                if user_id is not None:
                    history_kwargs["accountID"] = user_id
                history = self.server.history(**history_kwargs)
                watch_status[username] = bool(history)
                watch_evidence[username] = "history" if history else "not_watched"
                logger.debug(
                    f"Watch status for user '{username}' (accountID={user_id}): {bool(history)}, "
                    f"history items: {len(history)}, evidence={watch_evidence[username]}"
                )

            logger.info(f"Watch status for {media_item.title}: {watch_status}")
            if rating_key is not None:
                self.evidence[rating_key] = watch_evidence
            return watch_status
        except Exception as exc:
            logger.error(
                f"Error getting watch status for ratingKey {media_item.ratingKey}: {str(exc)}"
            )
            return {}

    def remove_from_watchlist(self, rating_key):
        """Remove an item from the Plex Watchlist for the owner token."""
        if not rating_key:
            return

        try:
            account = self.server.myPlexAccount()
            users = account.users()
            all_accounts = [account] + users

            for acc in all_accounts:
                try:
                    # For managed users, we might need to authenticate as them or use their ID
                    # PlexAPI handling of watchlists for managed users can be tricky.
                    # Standard 'removeFromWatchlist' usually works on the authenticated user's watchlist.
                    # For the owner (self.plex), it should work directly.
                    #
                    # We'll rely on the server connection to remove it from the watchlist
                    # if the item is found in the watchlist.
                    #
                    # Currently, the most reliable way via plexapi is to fetch the item and call removeFromWatchlist
                    # But we might have already deleted the file, so fetching might fail if it relies on file presence?
                    # Actually, Plex keeps metadata even if file is missing (Trash).
                    #
                    # Let's try to fetch the item using the library section
                    _ = acc
                    pass
                except Exception:
                    pass

            try:
                item = self.server.fetchItem(rating_key)
                if item:
                    logger.info(f"Attempting to remove {item.title} from Plex Watchlist (Owner)")
                    if hasattr(item, "removeFromWatchlist"):
                        item.removeFromWatchlist()
                        logger.info(f"Successfully removed {item.title} from Watchlist")
                    else:
                        account.removeFromWatchlist(item)
                        logger.info(f"Successfully removed {item.title} from Watchlist via Account")
            except Exception as exc:
                logger.warning(f"Failed to remove item {rating_key} from Watchlist: {exc}")

        except Exception as exc:
            logger.error(f"Error in remove_from_plex_watchlist: {exc}")

    def sync_leaving_soon_collection(self, name, rating_keys, removed_rating_keys=(), dry_run=False):
        """Synchronize a named manual collection and promote it to Plex hubs.

        Plex collections belong to one library section and cannot mix media
        types, so a shared name may produce one collection in each section.
        """
        desired = {str(key) for key in rating_keys if key}
        for key in removed_rating_keys:
            desired.discard(str(key))
        items_by_section = {}
        for rating_key in desired:
            try:
                item = self.server.fetchItem(int(rating_key))
                section = item.section()
                items_by_section.setdefault(section, []).append(item)
            except Exception as exc:
                logger.warning(f"Could not resolve Leaving Soon Plex item {rating_key}: {exc}")

        sections = self.server.library.sections()
        for section in sections:
            wanted_items = items_by_section.get(section, [])
            wanted_keys = {str(item.ratingKey) for item in wanted_items}
            existing = next((c for c in section.collections() if c.title == name), None)
            if existing is None and not wanted_items:
                continue
            if existing is None:
                existing = section.createCollection(name, items=wanted_items)
            else:
                present = existing.items()
                present_by_key = {str(item.ratingKey): item for item in present}
                remove_items = [item for key, item in present_by_key.items() if key not in wanted_keys]
                add_items = [item for item in wanted_items if str(item.ratingKey) not in present_by_key]
                if remove_items:
                    existing.removeItems(remove_items)
                if add_items:
                    existing.addItems(add_items)
            # Plex calls the corresponding advanced setting "showItems" and
            # exposes Home/Recommended promotion through ManagedHub.
            existing.modeUpdate("showItems")
            hub = existing.visibility()
            hub.promoteHome()
            hub.promoteRecommended()
