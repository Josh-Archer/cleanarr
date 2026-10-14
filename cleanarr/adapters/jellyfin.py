"""Jellyfin and Emby watched-state adapter.

Jellyfin and Emby share the same REST shape used here (``X-Emby-Token`` and
``/Users/{id}/Items?IsPlayed=true``). Job mode selects this source with
``CLEANARR_MEDIA_SOURCE=emby``. Webhook payload mapping stays in the webhook
app; this adapter only loads library watch state.
"""

from __future__ import annotations

from cleanarr import emby_client


class JellyfinMediaSource:
    """Read watched movies and episodes from a Jellyfin or Emby server."""

    def __init__(self, session, config):
        self.session = session
        # Same dict as CONFIG["emby"] so later env/test patches stay visible.
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
