"""Transmission RPC adapter and torrent identity helpers.

Removal policy (labels, categories, repeated I/O errors) lives in
``cleanarr.policy``. This module lists, removes, and describes torrents.
"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger


def normalize_incomplete_name(name: str) -> str:
    name = (name or "").strip()
    if name.endswith(".part"):
        name = name[:-5]
    return re.sub(r"[^a-z0-9]", "", name.lower())


def get_torrent_download_dir(torrent):
    for attr in ("download_dir", "downloadDir"):
        download_dir = getattr(torrent, attr, None)
        if download_dir:
            return download_dir
    return None


def get_torrent_category(torrent):
    download_dir = get_torrent_download_dir(torrent)
    if not download_dir:
        return None
    category = Path(download_dir).name.strip().lower()
    return category or None


def transmission_labels(torrent) -> set:
    return {
        label.strip().lower()
        for label in (getattr(torrent, "labels", None) or [])
        if label and label.strip()
    }


def iter_expected_incomplete_names(torrent):
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
    except Exception as exc:
        logger.warning(
            f"Failed to get files for torrent {torrent_name}: {exc}. "
            "Falling back to torrent metadata."
        )
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


class TransmissionClientAdapter:
    """Thin wrapper around a ``transmission_rpc`` client."""

    def __init__(self, client):
        self.client = client

    def get_torrents(self):
        return self.client.get_torrents()

    def remove_torrent(self, torrent_id, delete_data=False):
        return self.client.remove_torrent(torrent_id, delete_data=delete_data)

    def get_session(self):
        return self.client.get_session()
