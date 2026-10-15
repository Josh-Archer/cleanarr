"""Sonarr, Radarr, and Lidarr HTTP adapter.

Request behavior (retries, empty DELETE bodies, API-key header) matches the
historical MediaCleanup helpers. Callers own caching and delete policy.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urljoin

import requests
from loguru import logger


class ServarrClient:
    """One Arr application: Sonarr, Radarr, or Lidarr."""

    def __init__(
        self,
        service_name,
        config,
        *,
        session=None,
        session_factory=None,
        missing_key_env="API key",
    ):
        self.service_name = service_name
        self.config = config
        self._session = session
        self._session_factory = session_factory
        self.missing_key_env = missing_key_env

    @property
    def session(self):
        if self._session is None:
            factory = self._session_factory
            if factory is None:
                factory = requests.Session
            self._session = factory()
        return self._session

    def request(self, endpoint, method="GET", data=None):
        """Call an Arr endpoint. Returns parsed JSON, text, ``{}``, or None."""
        apikey = (self.config or {}).get("apikey")
        if not apikey:
            logger.error(
                f"Missing {self.service_name} API key. Set {self.missing_key_env}."
            )
            return None

        baseurl = (self.config or {}).get("baseurl") or ""
        url = urljoin(baseurl, endpoint)
        session = self.session
        session.headers.update({"X-Api-Key": apikey})
        return self._request(session, url, endpoint, method=method, data=data)

    def _request(self, session, url, endpoint, method="GET", data=None):
        """Arr request with light retrying for edge and upstream flakiness."""
        method = (method or "GET").upper()
        transient_statuses = {429, 500, 502, 503, 504}
        max_attempts = 3
        backoff_seconds = 1.5
        service_name = self.service_name

        for attempt in range(1, max_attempts + 1):
            try:
                request_kwargs = {"timeout": 30}
                if method == "GET":
                    response = session.get(url, **request_kwargs)
                elif method == "DELETE":
                    response = session.delete(url, **request_kwargs)
                elif method in {"POST", "PUT"}:
                    session.headers.update({"Content-Type": "application/json"})
                    request_kwargs["data"] = json.dumps(data)
                    response = session.request(method, url, **request_kwargs)
                else:
                    response = session.request(method, url, **request_kwargs)
                if response.status_code in transient_statuses and attempt < max_attempts:
                    logger.warning(
                        f"{service_name} API transient response ({method} {endpoint}): "
                        f"{response.status_code}; retrying ({attempt}/{max_attempts})"
                    )
                    time.sleep(backoff_seconds * attempt)
                    continue

                response.raise_for_status()
                if not response.text or not response.text.strip():
                    # Empty 2xx is success. Sonarr/Radarr DELETEs often return
                    # an empty body, and callers must not treat that as failure.
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return response.text
            except requests.exceptions.HTTPError as exc:
                response = exc.response
                status_code = response.status_code if response is not None else "unknown"
                response_text = response.text if response is not None else ""
                if status_code in transient_statuses and attempt < max_attempts:
                    logger.warning(
                        f"{service_name} API transient HTTP error ({method} {endpoint}): "
                        f"{status_code}; retrying ({attempt}/{max_attempts})"
                    )
                    time.sleep(backoff_seconds * attempt)
                    continue
                logger.error(
                    f"{service_name} API HTTP error ({method} {endpoint}): "
                    f"{status_code} - {response_text}"
                )
                return None
            except requests.exceptions.RequestException as exc:
                if attempt < max_attempts:
                    logger.warning(
                        f"{service_name} API request error ({method} {endpoint}): {exc}; "
                        f"retrying ({attempt}/{max_attempts})"
                    )
                    time.sleep(backoff_seconds * attempt)
                    continue
                logger.error(f"{service_name} API error ({method} {endpoint}): {exc}")
                return None
            except Exception as exc:
                logger.error(f"{service_name} API error ({method} {endpoint}): {exc}")
                return None

        return None
