"""Overseerr and Jellyseerr HTTP adapter.

Handles fetching media requests, pagination, and error handling with retries.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import requests
from loguru import logger


def join_url(baseurl: str, endpoint: str) -> str:
    """Join baseurl and endpoint ensuring the /api/v1 prefix is preserved."""
    endpoint = str(endpoint or "").strip()
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    base = str(baseurl or "").rstrip("/")
    endpoint_clean = endpoint.lstrip("/")
    if not base.endswith("/api/v1") and not endpoint_clean.startswith("api/v1"):
        base = f"{base}/api/v1"
    return f"{base}/{endpoint_clean}"


class SeerrError(RuntimeError):
    """Raised when Seerr API request fails or is unreachable."""

    pass


class SeerrClient:
    """Overseerr / Jellyseerr HTTP client."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        baseurl: Optional[str] = None,
        apikey: Optional[str] = None,
        session: Optional[requests.Session] = None,
        session_factory=None,
        max_retries: int = 3,
        timeout: int = 10,
    ):
        self.config = dict(config or {})
        if baseurl is not None:
            self.config["baseurl"] = baseurl
        if apikey is not None:
            self.config["apikey"] = apikey
        self._session = session
        self._session_factory = session_factory
        self.max_retries = max_retries
        self.timeout = timeout

    @property
    def baseurl(self) -> str:
        raw = self.config.get("baseurl") or ""
        return join_url(raw, "").rstrip("/")

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            factory = self._session_factory or requests.Session
            self._session = factory()
            apikey = self.config.get("apikey")
            if apikey:
                self._session.headers.update({"X-Api-Key": apikey})
        return self._session

    def request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
    ) -> Any:
        """Execute a request against the Seerr API with transient retries."""
        apikey = self.config.get("apikey")
        if not apikey:
            logger.error("Missing Seerr API key. Set CLEANARR_SEERR_APIKEY.")
            raise SeerrError("Missing Seerr API key")

        baseurl = self.config.get("baseurl") or "http://overseerr:5055/api/v1"
        url = join_url(baseurl, endpoint)
        session = self.session
        session.headers.update({"X-Api-Key": apikey})

        method = (method or "GET").upper()
        transient_statuses = {429, 500, 502, 503, 504}
        max_attempts = self.max_retries
        backoff_seconds = 0.05

        for attempt in range(1, max_attempts + 1):
            try:
                request_kwargs: Dict[str, Any] = {"timeout": self.timeout, "params": params}
                if method in {"POST", "PUT"}:
                    session.headers.update({"Content-Type": "application/json"})
                    response = session.request(
                        method, url, data=json.dumps(data), **request_kwargs
                    )
                else:
                    response = session.request(method, url, **request_kwargs)

                if response.status_code in transient_statuses and attempt < max_attempts:
                    logger.warning(
                        f"Seerr API transient response ({method} {endpoint}): "
                        f"{response.status_code}; retrying ({attempt}/{max_attempts})"
                    )
                    time.sleep(backoff_seconds * attempt)
                    continue

                response.raise_for_status()
                if not response.text or not response.text.strip():
                    return {}
                return response.json()
            except Exception as exc:
                if attempt < max_attempts:
                    logger.warning(
                        f"Seerr API request error ({method} {endpoint}): {exc}; "
                        f"retrying ({attempt}/{max_attempts})"
                    )
                    time.sleep(backoff_seconds * attempt)
                    continue
                logger.error(f"Seerr API error ({method} {endpoint}): {exc}")
                raise SeerrError(f"Seerr API error: {exc}") from exc

        raise SeerrError(f"Seerr API failed after {max_attempts} attempts")

    def get_requests(
        self,
        take: int = 100,
        skip: int = 0,
        sort: str = "added",
        filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch a page of media requests from Seerr."""
        params: Dict[str, Any] = {"take": take, "skip": skip, "sort": sort}
        if filter:
            params["filter"] = filter
        return self.request("request", params=params)

    def get_all_requests(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """Fetch all requests from Overseerr/Jellyseerr with pagination."""
        all_results: List[Dict[str, Any]] = []
        skip = 0
        take = page_size

        while True:
            data = self.get_requests(take=take, skip=skip)
            if isinstance(data, list):
                all_results.extend(data)
                break
            if not isinstance(data, dict):
                break

            results = data.get("results") or []
            all_results.extend(results)

            page_info = data.get("pageInfo") or {}
            total = page_info.get("results")
            if total is not None and len(all_results) >= int(total):
                break
            if len(results) < take:
                break
            skip += take

        return all_results
