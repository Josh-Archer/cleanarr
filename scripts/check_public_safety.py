#!/usr/bin/env python3
"""Fail CI if obvious private strings or artifacts are present in the public repo."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

BANNED_PATH_PARTS = (
    ".env.local",
    "plex_library.db",
    "sealed",
    "externalsecret",
    "k3s.yml",
)

BANNED_STRINGS = (
    ".".join(("archer", "casa")),
    ".".join(("archerfamily", "io")),
    ".".join(("ntfy", "monitoring", "svc", "cluster", "local")),
    ".".join(("plex-service", "media", "svc", "cluster", "local")),
    ".".join(("sonarr-service", "media", "svc", "cluster", "local")),
    ".".join(("radarr-service", "media", "svc", "cluster", "local")),
    ".".join(("transmission-service", "media", "svc", "cluster", "local")),
    "/".join(("ghcr.io", "example-org", "home")),
)


def iter_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts:
            yield path


def main():
    failures = []

    for path in iter_files():
        if path == Path(__file__).resolve():
            continue
        rel = path.relative_to(ROOT).as_posix().lower()
        if any(part in rel for part in BANNED_PATH_PARTS):
            failures.append(f"banned path: {rel}")
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            failures.append(f"read failed: {rel}: {exc}")
            continue

        for needle in BANNED_STRINGS:
            if needle in content:
                failures.append(f"banned string '{needle}' in {rel}")

    if failures:
        print("Public safety check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Public safety check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
