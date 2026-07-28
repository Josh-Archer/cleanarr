"""Optional shared-secret and HMAC authenticity checks for webhook ingress.

Verification is opt-in: when no secret is configured, requests are accepted
(operator is expected to protect the endpoint at the network/ingress layer).

When any secret is configured, verification fails closed: missing or invalid
token/signature credentials are rejected.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Iterable, Mapping, Optional, Sequence


# Header names accepted for shared-secret tokens (case-insensitive lookup).
TOKEN_HEADER_NAMES: Sequence[str] = (
    "X-Cleanarr-Webhook-Token",
    "X-Webhook-Token",
)

# Header names accepted for HMAC-SHA256 body signatures.
SIGNATURE_HEADER_NAMES: Sequence[str] = (
    "X-Cleanarr-Signature",
    "X-Hub-Signature-256",
    "X-Signature-256",
)


def _normalize_secret(value: Optional[str]) -> str:
    return (value or "").strip()


def configured_secrets(*secrets: Optional[str]) -> list[str]:
    """Return non-empty configured secrets (current then previous, de-duped)."""
    out: list[str] = []
    seen: set[str] = set()
    for secret in secrets:
        normalized = _normalize_secret(secret)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def verification_enabled(*secrets: Optional[str]) -> bool:
    """True when at least one shared secret is configured."""
    return bool(configured_secrets(*secrets))


def extract_token(
    headers: Optional[Mapping[str, str]] = None,
    query_token: Optional[str] = None,
) -> str:
    """Extract a shared-secret token from headers or query string."""
    if headers:
        lowered = {str(k).lower(): v for k, v in headers.items()}
        for name in TOKEN_HEADER_NAMES:
            value = lowered.get(name.lower())
            if value is not None and str(value).strip():
                return str(value).strip()
    return (query_token or "").strip()


def extract_signature_header(
    headers: Optional[Mapping[str, str]] = None,
) -> str:
    """Extract the first present signature header value."""
    if not headers:
        return ""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for name in SIGNATURE_HEADER_NAMES:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _parse_signature_value(raw: str) -> tuple[str, str]:
    """Parse ``sha256=<hex>`` or bare hex signatures into (algo, digest)."""
    value = (raw or "").strip()
    if not value:
        return "", ""
    if "=" in value:
        algo, _, digest = value.partition("=")
        return algo.strip().lower(), digest.strip()
    return "sha256", value


def compute_hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        body or b"",
        hashlib.sha256,
    ).hexdigest()


def signature_matches(secret: str, body: bytes, signature_header: str) -> bool:
    """Return True if the signature header is a valid HMAC-SHA256 of body."""
    algo, digest = _parse_signature_value(signature_header)
    if algo not in ("", "sha256") or not digest:
        return False
    expected = compute_hmac_sha256_hex(secret, body)
    try:
        return hmac.compare_digest(expected, digest.lower())
    except (TypeError, ValueError):
        return False


def token_matches(secret: str, token: str) -> bool:
    if not secret or not token:
        return False
    try:
        return hmac.compare_digest(secret, token)
    except (TypeError, ValueError):
        return False


def verify_webhook_request(
    *,
    secrets: Iterable[Optional[str]],
    token: Optional[str] = None,
    signature_header: Optional[str] = None,
    body: Optional[bytes] = None,
) -> tuple[bool, str]:
    """Verify optional webhook authenticity.

    Returns ``(accepted, reason)`` where reason is one of:
    - ``disabled``: no secrets configured (open accept)
    - ``ok_token``: shared secret matched
    - ``ok_signature``: HMAC signature matched
    - ``missing_credentials``: verification enabled but no token/signature
    - ``invalid_credentials``: token/signature present but did not match
    """
    active = configured_secrets(*list(secrets))
    if not active:
        return True, "disabled"

    provided_token = (token or "").strip()
    provided_sig = (signature_header or "").strip()
    raw_body = body if body is not None else b""

    if not provided_token and not provided_sig:
        return False, "missing_credentials"

    for secret in active:
        if provided_token and token_matches(secret, provided_token):
            return True, "ok_token"
        if provided_sig and signature_matches(secret, raw_body, provided_sig):
            return True, "ok_signature"

    return False, "invalid_credentials"


def verify_from_http(
    *,
    secrets: Iterable[Optional[str]],
    headers: Optional[Mapping[str, str]] = None,
    query_token: Optional[str] = None,
    body: Optional[bytes] = None,
) -> tuple[bool, str]:
    """Convenience wrapper extracting token/signature from HTTP inputs."""
    return verify_webhook_request(
        secrets=secrets,
        token=extract_token(headers, query_token=query_token),
        signature_header=extract_signature_header(headers),
        body=body,
    )
