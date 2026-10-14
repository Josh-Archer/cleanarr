"""Service I/O adapters used by cleanup jobs and webhook deletions.

Policy decisions live in ``cleanarr.policy``. These adapters talk to Plex,
Jellyfin/Emby, the Servarr apps, and Transmission.
"""

from cleanarr.adapters.jellyfin import JellyfinMediaSource
from cleanarr.adapters.plex import PlexMediaSource
from cleanarr.adapters.servarr import ServarrClient
from cleanarr.adapters.transmission import TransmissionClientAdapter

__all__ = [
    "JellyfinMediaSource",
    "PlexMediaSource",
    "ServarrClient",
    "TransmissionClientAdapter",
]
