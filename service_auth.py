"""Directional HMAC authentication for the private v2 service contract."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
from os import PathLike
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, runtime_checkable
from urllib.parse import unquote
from uuid import RFC_4122, UUID

from starlette.requests import Request

from integration_contract import MAX_JSON_BODY_BYTES


AUTH_VERSION = "v1"
CLOCK_SKEW_SECONDS = 60
NONCE_TTL_SECONDS = 120

HEADER_TIMESTAMP = "X-PostersPlus-Timestamp"
HEADER_CONTENT_SHA256 = "X-PostersPlus-Content-SHA256"
HEADER_REQUEST_ID = "X-PostersPlus-Request-ID"
HEADER_CALLER = "X-PostersPlus-Caller"
HEADER_AUDIENCE = "X-PostersPlus-Audience"
HEADER_SIGNATURE = "X-PostersPlus-Signature"

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_METHOD = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")
_TIMESTAMP = re.compile(r"^(?:0|[1-9][0-9]{0,11})$")


class AuthError(ValueError):
    """A bounded authentication failure safe to map to an HTTP response."""

    status_code = 403
    code = "invalid_service_auth"

    def __init__(self, message: str = "authentication failed") -> None:
        super().__init__(message)


class AuthenticationRequiredError(AuthError):
    status_code = 401
    code = "service_auth_required"


class QueryStringNotAllowedError(AuthError):
    status_code = 400
    code = "query_string_not_allowed"

    def __init__(self) -> None:
        super().__init__("query strings are not allowed")


class RequestBodyTooLargeError(AuthError):
    status_code = 413
    code = "request_body_too_large"

    def __init__(self) -> None:
        super().__init__(f"request body exceeds {MAX_JSON_BODY_BYTES} bytes")


@dataclass(frozen=True, slots=True)
class AuthContext:
    caller: str
    audience: str
    method: str
    path: str
    request_id: UUID
    timestamp: int
    body_sha256: str


@runtime_checkable
class NonceStore(Protocol):
    """Adapter contract; returning True means the nonce was stored once."""

    def record_once(
        self,
        caller: str,
        request_id: UUID,
        ttl_seconds: int,
    ) -> bool | Awaitable[bool]: ...


class MemoryNonceStore:
    """Concurrency-safe local nonce store suitable for tests/single workers."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._expires: dict[tuple[str, UUID], float] = {}
        self._lock = asyncio.Lock()

    async def record_once(self, caller: str, request_id: UUID, ttl_seconds: int) -> bool:
        if ttl_seconds < NONCE_TTL_SECONDS:
            raise ValueError(f"nonce TTL must be at least {NONCE_TTL_SECONDS} seconds")
        now = self._clock()
        key = (caller, request_id)
        async with self._lock:
            expired = [stored for stored, deadline in self._expires.items() if deadline < now]
            for stored in expired:
                self._expires.pop(stored, None)
            if key in self._expires:
                return False
            self._expires[key] = now + ttl_seconds
            return True


class SQLiteNonceStore:
    """Cross-process atomic nonce store for multi-worker service deployments."""

    def __init__(
        self,
        database_path: str | PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._database_path = str(database_path)
        self._clock = clock
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._timeout_seconds,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(self._timeout_seconds * 1000)}")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS postersplus_v2_auth_nonces (
                    caller TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (caller, request_id)
                ) WITHOUT ROWID
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_postersplus_v2_auth_nonce_expiry
                ON postersplus_v2_auth_nonces (expires_at)
                """
            )

    def record_once(self, caller: str, request_id: UUID, ttl_seconds: int) -> bool:
        if ttl_seconds < NONCE_TTL_SECONDS:
            raise ValueError(f"nonce TTL must be at least {NONCE_TTL_SECONDS} seconds")
        _validate_token(caller, "caller")
        _validate_request_id(request_id)
        now = self._clock()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM postersplus_v2_auth_nonces WHERE expires_at < ?",
                (now,),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO postersplus_v2_auth_nonces
                    (caller, request_id, expires_at)
                VALUES (?, ?, ?)
                """,
                (caller, str(request_id), now + ttl_seconds),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def _validate_token(value: str, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _validate_request_id(value: UUID) -> UUID:
    if not isinstance(value, UUID) or value.version != 4 or value.variant != RFC_4122:
        raise ValueError("request_id must be an RFC 4122 UUIDv4")
    return value


def _validate_timestamp(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 253_402_300_799:
        raise ValueError("timestamp must be a non-negative integer Unix timestamp")
    return value


def _decoded_absolute_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("path must be a decoded absolute path")
    if "?" in path or "#" in path:
        raise ValueError("query strings are not allowed")
    try:
        decoded = unquote(path, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("path must be valid UTF-8") from exc
    if not decoded.startswith("/") or decoded.startswith("//"):
        raise ValueError("path must be a decoded absolute path")
    if "?" in decoded or "#" in decoded:
        raise ValueError("decoded path contains a query or fragment delimiter")
    if any(character in decoded for character in ("\r", "\n", "\x00")):
        raise ValueError("path contains forbidden control characters")
    return decoded


def canonical_request_bytes(
    *,
    method: str,
    path: str,
    body: bytes,
    request_id: UUID,
    timestamp: int,
    caller: str,
    audience: str,
) -> bytes:
    """Build the exact UTF-8 bytes covered by a v1 signature."""

    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    canonical_method = method.upper() if isinstance(method, str) else ""
    if not _METHOD.fullmatch(canonical_method):
        raise ValueError("invalid HTTP method")
    decoded_path = _decoded_absolute_path(path)
    nonce = _validate_request_id(request_id)
    unix_timestamp = _validate_timestamp(timestamp)
    canonical_caller = _validate_token(caller, "caller")
    canonical_audience = _validate_token(audience, "audience")
    body_sha256 = hashlib.sha256(body).hexdigest()
    return "\n".join(
        (
            AUTH_VERSION,
            canonical_caller,
            canonical_audience,
            canonical_method,
            decoded_path,
            str(nonce),
            str(unix_timestamp),
            body_sha256,
        )
    ).encode("utf-8")


def sign_request(
    method: str,
    path: str,
    body: bytes,
    request_id: UUID,
    timestamp: int,
    secret: bytes,
    caller: str,
    audience: str,
) -> str:
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("service secret must be non-empty bytes")
    canonical = canonical_request_bytes(
        method=method,
        path=path,
        body=body,
        request_id=request_id,
        timestamp=timestamp,
        caller=caller,
        audience=audience,
    )
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def build_auth_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    request_id: UUID,
    timestamp: int,
    secret: bytes,
    caller: str,
    audience: str,
) -> dict[str, str]:
    """Construct the only accepted, plural ``X-PostersPlus-*`` headers."""

    signature = sign_request(
        method,
        path,
        body,
        request_id,
        timestamp,
        secret,
        caller,
        audience,
    )
    return {
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_CONTENT_SHA256: hashlib.sha256(body).hexdigest(),
        HEADER_REQUEST_ID: str(request_id),
        HEADER_CALLER: caller,
        HEADER_AUDIENCE: audience,
        HEADER_SIGNATURE: signature,
    }


def _single_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if not values:
        raise AuthenticationRequiredError()
    if len(values) != 1:
        raise AuthError()
    value = values[0]
    if len(value) > 256:
        raise AuthError()
    return value


async def _bounded_body(request: Request) -> bytes:
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise AuthError()
    if lengths:
        value = lengths[0]
        if not value.isascii() or not value.isdigit():
            raise AuthError()
        if int(value) > MAX_JSON_BODY_BYTES:
            raise RequestBodyTooLargeError()

    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_JSON_BODY_BYTES:
            chunks.clear()
            raise RequestBodyTooLargeError()
        chunks.append(chunk)
    body = b"".join(chunks)
    # Starlette normally sets this in Request.body().  Keep downstream JSON
    # parsing safe after consuming the stream ourselves for the hard byte cap.
    request._body = body  # type: ignore[attr-defined]
    return body


def _request_path(request: Request) -> str:
    raw_path = request.scope.get("raw_path")
    if isinstance(raw_path, bytes):
        try:
            path = raw_path.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuthError() from exc
    else:
        path = request.scope.get("path", "")
    try:
        return _decoded_absolute_path(path)
    except ValueError as exc:
        raise AuthError() from exc


async def verify_request(
    request: Request,
    secret: bytes,
    caller: str,
    audience: str,
    nonce_store: NonceStore,
    *,
    now: int | None = None,
) -> AuthContext:
    """Authenticate a request and atomically consume its UUIDv4 nonce."""

    if request.scope.get("query_string", b""):
        raise QueryStringNotAllowedError()
    if not isinstance(secret, bytes) or not secret:
        raise AuthenticationRequiredError()
    try:
        expected_caller = _validate_token(caller, "caller")
        expected_audience = _validate_token(audience, "audience")
    except ValueError as exc:
        raise AuthError() from exc

    timestamp_header = _single_header(request, HEADER_TIMESTAMP)
    digest_header = _single_header(request, HEADER_CONTENT_SHA256)
    request_id_header = _single_header(request, HEADER_REQUEST_ID)
    caller_header = _single_header(request, HEADER_CALLER)
    audience_header = _single_header(request, HEADER_AUDIENCE)
    signature_header = _single_header(request, HEADER_SIGNATURE)

    if caller_header != expected_caller or audience_header != expected_audience:
        raise AuthError()
    if not _TIMESTAMP.fullmatch(timestamp_header):
        raise AuthError()
    timestamp = int(timestamp_header)
    current_time = int(time.time()) if now is None else _validate_timestamp(now)
    if abs(current_time - timestamp) > CLOCK_SKEW_SECONDS:
        raise AuthError()
    try:
        request_id = UUID(request_id_header)
        _validate_request_id(request_id)
    except (ValueError, AttributeError) as exc:
        raise AuthError() from exc
    if request_id_header != str(request_id):
        raise AuthError()
    if not _HEX_SHA256.fullmatch(digest_header) or not _HEX_SHA256.fullmatch(signature_header):
        raise AuthError()

    body = await _bounded_body(request)
    if request.method.upper() == "GET" and body:
        raise AuthError()
    actual_digest = hashlib.sha256(body).hexdigest()
    path = _request_path(request)
    try:
        expected_signature = sign_request(
            request.method,
            path,
            body,
            request_id,
            timestamp,
            secret,
            expected_caller,
            expected_audience,
        )
    except (TypeError, ValueError) as exc:
        raise AuthError() from exc

    digest_matches = hmac.compare_digest(actual_digest, digest_header)
    signature_matches = hmac.compare_digest(expected_signature, signature_header)
    if not (digest_matches and signature_matches):
        raise AuthError()

    try:
        accepted = nonce_store.record_once(
            expected_caller,
            request_id,
            NONCE_TTL_SECONDS,
        )
        if inspect.isawaitable(accepted):
            accepted = await accepted
    except Exception as exc:
        if isinstance(exc, AuthError):
            raise
        raise AuthError() from exc
    if accepted is not True:
        raise AuthError()

    return AuthContext(
        caller=expected_caller,
        audience=expected_audience,
        method=request.method.upper(),
        path=path,
        request_id=request_id,
        timestamp=timestamp,
        body_sha256=actual_digest,
    )


__all__ = [
    "AUTH_VERSION",
    "AuthContext",
    "AuthError",
    "AuthenticationRequiredError",
    "CLOCK_SKEW_SECONDS",
    "HEADER_AUDIENCE",
    "HEADER_CALLER",
    "HEADER_CONTENT_SHA256",
    "HEADER_REQUEST_ID",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "MemoryNonceStore",
    "NONCE_TTL_SECONDS",
    "NonceStore",
    "QueryStringNotAllowedError",
    "RequestBodyTooLargeError",
    "SQLiteNonceStore",
    "build_auth_headers",
    "canonical_request_bytes",
    "sign_request",
    "verify_request",
]
