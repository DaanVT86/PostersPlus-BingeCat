"""Minimal directional HMAC verifier for the private BingeCat contract."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Mapping
from uuid import UUID, uuid4


AUTH_VERSION = "v1"
CLOCK_SKEW_SECONDS = 60
MAX_BODY_BYTES = 256 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuthError(ValueError):
    """Raised when a signed request fails closed."""


def sign_request(
    secret: str,
    *,
    caller: str,
    audience: str,
    method: str,
    path: str,
    body: bytes,
    request_id: UUID | str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build the directional HMAC headers for one contract request."""
    if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES or not secret:
        raise AuthError("authentication failed")
    if not caller or not audience or not method or not path:
        raise AuthError("authentication failed")
    try:
        request_uuid = UUID(str(request_id)) if request_id is not None else uuid4()
    except (TypeError, ValueError) as exc:
        raise AuthError("authentication failed") from exc
    request_timestamp = int(time.time() if timestamp is None else timestamp)
    digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        (
            AUTH_VERSION,
            caller,
            audience,
            method.upper(),
            path,
            str(request_uuid),
            str(request_timestamp),
            digest,
        )
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {
        "X-PostersPlus-Caller": caller,
        "X-PostersPlus-Audience": audience,
        "X-PostersPlus-Request-ID": str(request_uuid),
        "X-PostersPlus-Timestamp": str(request_timestamp),
        "X-PostersPlus-Content-SHA256": digest,
        "X-PostersPlus-Signature": signature,
    }


def _header(headers: Mapping[str, str], name: str) -> str:
    values = [value for key, value in headers.items() if key.lower() == name.lower()]
    if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > 256:
        raise AuthError("authentication failed")
    return values[0]


def verify_request(
    headers: Mapping[str, str],
    body: bytes,
    *,
    secret: str,
    method: str,
    path: str,
    now: int | None = None,
    nonces: dict[tuple[str, str], float] | None = None,
) -> None:
    """Verify the shared v2 headers for caller ``bingecat``.

    The nonce map is intentionally process-local here.  Core's HTTP workers
    already run behind a bounded private service; deployments that need
    cross-process replay storage can replace this function's store without
    changing the wire contract.
    """

    if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
        raise AuthError("authentication failed")
    if not secret:
        raise AuthError("authentication failed")
    caller = _header(headers, "X-PostersPlus-Caller")
    audience = _header(headers, "X-PostersPlus-Audience")
    request_id = _header(headers, "X-PostersPlus-Request-ID")
    timestamp_raw = _header(headers, "X-PostersPlus-Timestamp")
    supplied_digest = _header(headers, "X-PostersPlus-Content-SHA256")
    supplied_signature = _header(headers, "X-PostersPlus-Signature")
    if caller != "bingecat" or audience != "postersplus":
        raise AuthError("authentication failed")
    try:
        UUID(request_id)
        timestamp = int(timestamp_raw)
    except (TypeError, ValueError) as exc:
        raise AuthError("authentication failed") from exc
    if not _SHA256.fullmatch(supplied_digest) or not _SHA256.fullmatch(supplied_signature):
        raise AuthError("authentication failed")
    digest = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(digest, supplied_digest):
        raise AuthError("authentication failed")
    current = int(time.time() if now is None else now)
    if abs(current - timestamp) > CLOCK_SKEW_SECONDS:
        raise AuthError("authentication failed")
    canonical = "\n".join(
        (
            AUTH_VERSION,
            caller,
            audience,
            method.upper(),
            path,
            request_id,
            str(timestamp),
            digest,
        )
    ).encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_signature):
        raise AuthError("authentication failed")
    if nonces is not None:
        key = (caller, request_id)
        for expired, deadline in tuple(nonces.items()):
            if deadline <= current:
                nonces.pop(expired, None)
        if key in nonces:
            raise AuthError("authentication failed")
        nonces[key] = current + 120
