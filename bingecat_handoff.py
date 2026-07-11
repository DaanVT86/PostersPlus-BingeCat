"""Secure browser handoff for BingeCat power-user poster configuration.

Opaque handoff/save credentials never leave the server-to-server channel.  The
browser receives only a short-lived random session cookie and a matching CSRF
token.  No route accepts the legacy PostersPlus ``ACCESS_KEY``.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

import config
from integration_contract import CONTRACT_SCHEMA, CONTRACT_VERSION, MAX_JSON_BODY_BYTES
from render_spec import CanonicalRenderSpec, canonicalize_config
from service_auth import build_auth_headers
from v2_render import RENDERER_REVISION


router = APIRouter()
logger = logging.getLogger(__name__)
_SQLITE_INIT_LOCK = threading.Lock()

COOKIE_NAME = "pp_bc_session"
COOKIE_PATH = "/bingecat/configurator"
SESSION_TTL_SECONDS = 300
MAX_FORM_BODY_BYTES = 64 * 1024
MAX_CALLBACK_RESPONSE_BYTES = 64 * 1024
MAX_HANDOFF_TOKEN_BYTES = 512
MAX_CANONICAL_CONFIG_BYTES = 48 * 1024
CONSUME_PATH = "/api/internal/posterplus/v2/handoffs/consume"
SAVE_PATH = "/api/internal/posterplus/v2/configs"

_OPAQUE = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")
_HANDOFF_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")


class HandoffError(RuntimeError):
    """Bounded failure safe to map to a generic browser response."""

    def __init__(self, code: str, status_code: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ConsumedHandoff:
    handoff_id: str
    save_grant: str
    expires_at: float
    return_url: str
    canonical_config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConfiguratorSession:
    digest: str
    handoff_id: str
    save_grant: str
    return_url: str
    canonical_config: dict[str, Any]
    expires_at: float
    state: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(payload.encode("utf-8")) > MAX_CANONICAL_CONFIG_BYTES:
        raise HandoffError("invalid_config", 422)
    return payload


def _safe_origin(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("invalid callback origin")
    if any(character in value for character in ("\r", "\n", "\x00", "\\")):
        raise ValueError("invalid callback origin")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid callback origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port == 0
    ):
        raise ValueError("invalid callback origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _allowed_return_urls() -> tuple[str, ...]:
    candidates = tuple(
        value.strip()
        for value in config.POSTERSPLUS_CONFIGURATOR_RETURN_URLS.replace("\n", ",").split(",")
        if value.strip()
    )
    valid: list[str] = []
    for value in candidates:
        if len(value) > 2048 or any(char in value for char in ("\r", "\n", "\x00", "\\")):
            raise ValueError("invalid return URL")
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid return URL") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.startswith("/")
            or port == 0
        ):
            raise ValueError("invalid return URL")
        valid.append(value)
    if not valid or len(set(valid)) != len(valid):
        raise ValueError("missing or duplicate return URL")
    return tuple(valid)


def _exact_return_url(value: object, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        raise HandoffError("invalid_callback", 503)
    for candidate in allowed:
        if hmac.compare_digest(value, candidate):
            return candidate
    raise HandoffError("invalid_callback", 503)


def _enabled() -> bool:
    try:
        _safe_origin(config.BINGECAT_POSTERSPLUS_CALLBACK_BASE_URL)
        _allowed_return_urls()
    except ValueError:
        return False
    return bool(config.BINGECAT_POSTERSPLUS_CALLBACK_SECRET)


class SQLiteConfiguratorSessionStore:
    """Cross-worker five-minute sessions keyed by a digest of the browser cookie."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self.path = str(database_path)
        self.clock = clock
        self.token_factory = token_factory
        self._secure_initialize()

    def _secure_initialize(self) -> None:
        path = Path(self.path).absolute()
        parent = path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if parent.resolve(strict=True) != parent:
                raise ValueError("session database parent may not contain symlinks")
            os.chmod(parent, 0o700)
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ValueError("session database must be a regular file")
            else:
                descriptor = os.open(
                    path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.close(descriptor)
        except OSError as exc:
            raise ValueError("session database path is unsafe") from exc
        self.path = str(path)
        with _SQLITE_INIT_LOCK:
            previous_umask = os.umask(0o077)
            try:
                self._initialize()
            finally:
                os.umask(previous_umask)
        self._secure_modes()

    def _secure_modes(self) -> None:
        for candidate in (self.path, f"{self.path}-wal", f"{self.path}-shm"):
            try:
                info = os.lstat(candidate)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ValueError("unsafe session database sidecar")
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError("session database permissions unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            info = os.lstat(self.path)
        except OSError as exc:
            raise sqlite3.OperationalError("session database unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise sqlite3.OperationalError("session database path is unsafe")
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bingecat_configurator_sessions (
                    session_digest TEXT PRIMARY KEY,
                    csrf_digest TEXT NOT NULL,
                    handoff_id TEXT NOT NULL,
                    save_grant TEXT NOT NULL,
                    return_url TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('ready', 'saving')),
                    created_at REAL NOT NULL
                ) WITHOUT ROWID
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS ix_bingecat_configurator_expiry "
                "ON bingecat_configurator_sessions(expires_at)"
            )
        self._secure_modes()

    def create(self, consumed: ConsumedHandoff) -> tuple[str, str]:
        now = self.clock()
        expiry = min(float(consumed.expires_at), now + SESSION_TTL_SECONDS)
        if expiry <= now:
            raise HandoffError("handoff_expired", 410)
        config_json = _canonical_json(consumed.canonical_config)
        for _attempt in range(3):
            session_token = self.token_factory(48)
            csrf_token = self.token_factory(32)
            if not _OPAQUE.fullmatch(session_token) or not _OPAQUE.fullmatch(csrf_token):
                continue
            try:
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "DELETE FROM bingecat_configurator_sessions WHERE expires_at<=?",
                        (now,),
                    )
                    db.execute(
                        """
                        INSERT INTO bingecat_configurator_sessions
                            (session_digest, csrf_digest, handoff_id, save_grant,
                             return_url, config_json, expires_at, state, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?)
                        """,
                        (
                            _digest(session_token),
                            _digest(csrf_token),
                            consumed.handoff_id,
                            consumed.save_grant,
                            consumed.return_url,
                            config_json,
                            expiry,
                            now,
                        ),
                    )
                    db.commit()
                return session_token, csrf_token
            except sqlite3.IntegrityError:
                continue
            except sqlite3.Error as exc:
                raise HandoffError("session_unavailable", 503) from exc
        raise HandoffError("session_unavailable", 503)

    def _token_digest(self, token: object) -> str:
        if not isinstance(token, str) or not _OPAQUE.fullmatch(token):
            raise HandoffError("session_unavailable", 410)
        return _digest(token)

    @staticmethod
    def _row(row: tuple[Any, ...]) -> ConfiguratorSession:
        try:
            config_value = json.loads(row[4])
        except (TypeError, ValueError) as exc:
            raise HandoffError("session_unavailable", 410) from exc
        if not isinstance(config_value, dict):
            raise HandoffError("session_unavailable", 410)
        return ConfiguratorSession(
            digest=str(row[0]),
            handoff_id=str(row[1]),
            save_grant=str(row[2]),
            return_url=str(row[3]),
            canonical_config=config_value,
            expires_at=float(row[5]),
            state=str(row[6]),
        )

    def get(self, token: object) -> ConfiguratorSession:
        digest = self._token_digest(token)
        now = self.clock()
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT session_digest, handoff_id, save_grant, return_url, "
                    "config_json, expires_at, state FROM bingecat_configurator_sessions "
                    "WHERE session_digest=? AND expires_at>?",
                    (digest, now),
                ).fetchone()
        except sqlite3.Error as exc:
            raise HandoffError("session_unavailable", 503) from exc
        if row is None:
            raise HandoffError("session_unavailable", 410)
        return self._row(row)

    def rotate_csrf(self, token: object) -> tuple[ConfiguratorSession, str]:
        digest = self._token_digest(token)
        csrf_token = self.token_factory(32)
        if not _OPAQUE.fullmatch(csrf_token):
            raise HandoffError("session_unavailable", 503)
        now = self.clock()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT session_digest, handoff_id, save_grant, return_url, "
                    "config_json, expires_at, state FROM bingecat_configurator_sessions "
                    "WHERE session_digest=? AND expires_at>? AND state='ready'",
                    (digest, now),
                ).fetchone()
                if row is None:
                    db.rollback()
                    raise HandoffError("session_unavailable", 410)
                updated = db.execute(
                    "UPDATE bingecat_configurator_sessions SET csrf_digest=? "
                    "WHERE session_digest=? AND expires_at>? AND state='ready'",
                    (_digest(csrf_token), digest, now),
                ).rowcount
                if updated != 1:
                    db.rollback()
                    raise HandoffError("session_unavailable", 410)
                db.commit()
                return self._row(row), csrf_token
        except HandoffError:
            raise
        except sqlite3.Error as exc:
            raise HandoffError("session_unavailable", 503) from exc

    def claim_save(self, token: object, csrf_token: object) -> ConfiguratorSession:
        digest = self._token_digest(token)
        if not isinstance(csrf_token, str) or not _OPAQUE.fullmatch(csrf_token):
            raise HandoffError("csrf_failed", 403)
        now = self.clock()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT session_digest, handoff_id, save_grant, return_url, "
                    "config_json, expires_at, state, csrf_digest "
                    "FROM bingecat_configurator_sessions WHERE session_digest=?",
                    (digest,),
                ).fetchone()
                if row is None or float(row[5]) <= now:
                    db.rollback()
                    raise HandoffError("session_unavailable", 410)
                if not hmac.compare_digest(str(row[7]), _digest(csrf_token)):
                    db.rollback()
                    raise HandoffError("csrf_failed", 403)
                if row[6] != "ready":
                    db.rollback()
                    raise HandoffError("save_in_progress", 409)
                updated = db.execute(
                    "UPDATE bingecat_configurator_sessions SET state='saving' "
                    "WHERE session_digest=? AND state='ready'",
                    (digest,),
                ).rowcount
                if updated != 1:
                    db.rollback()
                    raise HandoffError("save_in_progress", 409)
                db.commit()
                return self._row(row[:7])
        except HandoffError:
            raise
        except sqlite3.Error as exc:
            raise HandoffError("session_unavailable", 503) from exc

    def release_save(self, digest: str) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    "UPDATE bingecat_configurator_sessions SET state='ready' "
                    "WHERE session_digest=? AND state='saving'",
                    (digest,),
                )
                db.commit()
        except sqlite3.Error as exc:
            raise HandoffError("session_unavailable", 503) from exc

    def complete(self, digest: str) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    "DELETE FROM bingecat_configurator_sessions WHERE session_digest=?",
                    (digest,),
                )
                db.commit()
        except sqlite3.Error as exc:
            raise HandoffError("session_unavailable", 503) from exc


class BingeCatCallbackClient:
    """Fixed-origin directional-HMAC client with bounded responses and no redirects."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.base_url = _safe_origin(base_url)
        if not isinstance(secret, str) or not secret or len(secret.encode("utf-8")) > 4096:
            raise ValueError("invalid callback secret")
        self.secret = secret.encode("utf-8")
        self.timeout = max(0.5, min(10.0, float(timeout_seconds)))
        self.transport = transport
        self.clock = clock

    async def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = _canonical_json(payload).encode("utf-8")
        if len(body) > MAX_JSON_BODY_BYTES:
            raise HandoffError("callback_unavailable", 503)
        request_id = uuid4()
        timestamp = int(self.clock())
        headers = build_auth_headers(
            method="POST",
            path=path,
            body=body,
            request_id=request_id,
            timestamp=timestamp,
            secret=self.secret,
            caller="postersplus",
            audience="bingecat",
        )
        headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        timeout = httpx.Timeout(self.timeout)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST", f"{self.base_url}{path}", content=body, headers=headers
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise HandoffError("callback_unavailable", 503)
                    content_types = response.headers.get_list("content-type")
                    if (
                        len(content_types) != 1
                        or content_types[0].split(";", 1)[0].strip().lower()
                        != "application/json"
                    ):
                        raise HandoffError("invalid_callback", 503)
                    lengths = response.headers.get_list("content-length")
                    if len(lengths) > 1:
                        raise HandoffError("invalid_callback", 503)
                    declared_length: int | None = None
                    if lengths:
                        declared = lengths[0]
                        if not declared.isascii() or not declared.isdigit():
                            raise HandoffError("invalid_callback", 503)
                        declared_length = int(declared)
                        if declared_length > MAX_CALLBACK_RESPONSE_BYTES:
                            raise HandoffError("callback_unavailable", 503)
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_CALLBACK_RESPONSE_BYTES:
                            raise HandoffError("callback_unavailable", 503)
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    if declared_length is not None and declared_length != len(raw):
                        raise HandoffError("invalid_callback", 503)
                    if response.status_code == 410:
                        raise HandoffError("handoff_unavailable", 410)
                    if response.status_code != 200:
                        raise HandoffError("callback_unavailable", 503)
        except HandoffError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HandoffError("callback_unavailable", 503) from exc
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HandoffError("invalid_callback", 503) from exc
        if not isinstance(value, dict):
            raise HandoffError("invalid_callback", 503)
        return value

    async def consume(self, handoff_token: str) -> dict[str, Any]:
        return await self._post(
            CONSUME_PATH,
            {
                "schema": CONTRACT_SCHEMA,
                "version": CONTRACT_VERSION,
                "handoff_token": handoff_token,
            },
        )

    async def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._post(SAVE_PATH, payload)


_SESSION_STORE: SQLiteConfiguratorSessionStore | None = None
_SESSION_STORE_PATH: str | None = None


def _session_store() -> SQLiteConfiguratorSessionStore:
    global _SESSION_STORE, _SESSION_STORE_PATH
    path = config.POSTERSPLUS_CONFIGURATOR_SESSION_DB_PATH
    if not path:
        raise HandoffError("session_unavailable", 503)
    if _SESSION_STORE is None or _SESSION_STORE_PATH != path:
        try:
            _SESSION_STORE = SQLiteConfiguratorSessionStore(path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HandoffError("session_unavailable", 503) from exc
        _SESSION_STORE_PATH = path
    return _SESSION_STORE


def _callback_client() -> BingeCatCallbackClient:
    try:
        return BingeCatCallbackClient(
            config.BINGECAT_POSTERSPLUS_CALLBACK_BASE_URL,
            config.BINGECAT_POSTERSPLUS_CALLBACK_SECRET,
            timeout_seconds=config.POSTERSPLUS_CONFIGURATOR_CALLBACK_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        raise HandoffError("callback_unavailable", 503) from exc


async def _bounded_body(request: Request, maximum: int) -> bytes:
    values = request.headers.getlist("content-length")
    if len(values) > 1:
        raise HandoffError("invalid_request", 400)
    if values:
        if not values[0].isascii() or not values[0].isdigit():
            raise HandoffError("invalid_request", 400)
        if int(values[0]) > maximum:
            raise HandoffError("request_too_large", 413)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HandoffError("request_too_large", 413)
        chunks.append(chunk)
    return b"".join(chunks)


async def _urlencoded_form(request: Request, maximum: int) -> dict[str, str]:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != (
        "application/x-www-form-urlencoded"
    ):
        raise HandoffError("invalid_request", 400)
    body = await _bounded_body(request, maximum)
    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=128,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HandoffError("invalid_request", 400) from exc
    if any(len(values) != 1 for values in parsed.values()):
        raise HandoffError("invalid_request", 400)
    return {key: values[0] for key, values in parsed.items()}


def _parse_expiry(value: object, now: float) -> float:
    if not isinstance(value, str) or len(value) > 64:
        raise HandoffError("invalid_callback", 503)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffError("invalid_callback", 503) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HandoffError("invalid_callback", 503)
    expiry = parsed.astimezone(timezone.utc).timestamp()
    if expiry <= now or expiry > now + SESSION_TTL_SECONDS + 60:
        raise HandoffError("invalid_callback", 503)
    return expiry


def _validate_consumed(value: object, now: float) -> ConsumedHandoff:
    required = {"schema", "version", "handoff_id", "save_grant", "expires_at", "return_url"}
    if not isinstance(value, dict):
        raise HandoffError("invalid_callback", 503)
    keys = set(value)
    if keys != required and keys != required | {"canonical_config"}:
        raise HandoffError("invalid_callback", 503)
    if (
        value.get("schema") != CONTRACT_SCHEMA
        or isinstance(value.get("version"), bool)
        or value.get("version") != CONTRACT_VERSION
        or not isinstance(value.get("handoff_id"), str)
        or not _HANDOFF_ID.fullmatch(value["handoff_id"])
        or not isinstance(value.get("save_grant"), str)
        or not _OPAQUE.fullmatch(value["save_grant"])
    ):
        raise HandoffError("invalid_callback", 503)
    return_url = _exact_return_url(value.get("return_url"), _allowed_return_urls())
    supplied = value.get("canonical_config")
    try:
        spec = canonicalize_config(supplied if isinstance(supplied, dict) else {})
    except (TypeError, ValueError) as exc:
        raise HandoffError("invalid_callback", 503) from exc
    canonical = json.loads(spec.canonical_json())
    if supplied is not None and supplied != canonical:
        raise HandoffError("invalid_callback", 503)
    return ConsumedHandoff(
        handoff_id=value["handoff_id"],
        save_grant=value["save_grant"],
        expires_at=_parse_expiry(value.get("expires_at"), now),
        return_url=return_url,
        canonical_config=canonical,
    )


def _validate_saved(value: object, expected_return_url: str) -> dict[str, Any]:
    required = {"schema", "version", "config_public_id", "revision", "reused", "return_url"}
    if not isinstance(value, dict) or set(value) != required:
        raise HandoffError("invalid_callback", 503)
    revision = value.get("revision")
    if (
        value.get("schema") != CONTRACT_SCHEMA
        or isinstance(value.get("version"), bool)
        or value.get("version") != CONTRACT_VERSION
        or not isinstance(value.get("config_public_id"), str)
        or not _HEX_32.fullmatch(value["config_public_id"])
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision <= 0
        or not isinstance(value.get("reused"), bool)
        or not isinstance(value.get("return_url"), str)
        or not hmac.compare_digest(value["return_url"], expected_return_url)
    ):
        raise HandoffError("invalid_callback", 503)
    _exact_return_url(value["return_url"], _allowed_return_urls())
    return value


_BOOL_FIELDS = {
    field.name
    for field in fields(CanonicalRenderSpec)
    if isinstance(field.default, bool)
}
_FIELD_NAMES = {field.name for field in fields(CanonicalRenderSpec)} - {"schema", "version"}
_CHOICES = {
    "badge_display_mode": ("0", "3"),
    "rating_display_mode": tuple(str(value) for value in range(6)),
    "score_color_mode": tuple(str(value) for value in range(4)),
    "bar_style": ("frosted", "pure_black", "silver", "gold", "rating_black", "rating_frosted"),
    "bar_accent": ("silver", "gold", "sample", "palette_0", "palette_1", "palette_2", "palette_custom"),
    "bar_append": ("rating_year", "rating", "year", "sash", "second_rating"),
    "logo_language": ("en", "pt", "nl", "de", "es"),
    "logo_priority": ("native_original", "original_native", "native_if_original_english", "native_text"),
    "fallback_bg_style": ("minimal", "photoreal"),
    "original_art_source": ("primary", "top_rated"),
    "top_gradient": ("off", "low", "medium", "high", "custom"),
    "bottom_gradient": ("off", "low", "medium", "high", "custom"),
    "sash_mode": ("hidden", "sash", "notch"),
    "sash_badge_style": ("silver", "gold", "frosted", "black"),
    "primary_client": ("manual", "stremio_tv_nuvio", "stremio_desktop_web", "plex", "jellyfin"),
}


def _format_value(name: str, value: Any) -> str:
    if name in {"movie_weights", "tv_weights"}:
        return ",".join(f"{source}:{weight:g}" for source, weight in value)
    if name in {"sash_priority", "sash_exclusions"}:
        return ",".join(value)
    if value is None:
        return ""
    return str(value)


def _configurator_html(session: ConfiguratorSession, csrf_token: str) -> str:
    values = dict(session.canonical_config)
    controls: list[str] = []
    controls.append(
        '<label>Clientprofiel<select name="primary_client">'
        + "".join(f'<option value="{html.escape(choice)}">{html.escape(choice)}</option>' for choice in _CHOICES["primary_client"])
        + "</select></label>"
    )
    for definition in fields(CanonicalRenderSpec):
        name = definition.name
        if name in {"schema", "version"}:
            continue
        value = values.get(name, definition.default)
        escaped_name = html.escape(name)
        if name in _BOOL_FIELDS:
            checked = " checked" if value else ""
            control = f'<input type="checkbox" name="{escaped_name}" value="true"{checked}>'
        elif name in _CHOICES:
            selected = str(value)
            options = "".join(
                f'<option value="{html.escape(choice)}"'
                + (" selected" if choice == selected else "")
                + f'>{html.escape(choice)}</option>'
                for choice in _CHOICES[name]
            )
            control = f'<select name="{escaped_name}">{options}</select>'
        else:
            raw = html.escape(_format_value(name, value), quote=True)
            input_type = "number" if isinstance(value, (int, float)) and not isinstance(value, bool) else "text"
            step = ' step="any"' if input_type == "number" else ""
            control = f'<input type="{input_type}" name="{escaped_name}" value="{raw}"{step}>'
        controls.append(f"<label>{escaped_name}{control}</label>")
    template = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BingeCat PostersPlus</title><style>
:root{color-scheme:dark;font:15px system-ui;background:#101218;color:#f5f6fa}body{max-width:1100px;margin:auto;padding:24px}
h1{margin:0 0 8px}.note{color:#aeb5c4;margin-bottom:22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
label{display:grid;gap:6px;background:#191d27;padding:10px;border-radius:8px}input,select,button{font:inherit;padding:8px;border-radius:6px;border:1px solid #434b60;background:#111522;color:inherit}
button{margin-top:20px;background:#7257ff;border:0;font-weight:700;cursor:pointer}.actions{display:flex;gap:12px;align-items:center}
</style></head><body><h1>PostersPlus v2</h1><p class="note">Power-userconfiguratie voor BingeCat. Quality-badges zijn beperkt tot uit of leeftijd.</p>
<form method="post" action="/bingecat/configurator/save"><input type="hidden" name="csrf_token" value="{{CSRF}}">
<div class="grid">{{CONTROLS}}</div><div class="actions"><button type="submit">Opslaan in BingeCat</button></div></form></body></html>"""
    return template.replace("{{CSRF}}", html.escape(csrf_token, quote=True)).replace(
        "{{CONTROLS}}", "".join(controls)
    )


def _config_from_form(form: Mapping[str, str]) -> dict[str, Any]:
    allowed = _FIELD_NAMES | {"csrf_token", "primary_client"}
    if set(form) - allowed:
        raise HandoffError("invalid_config", 422)
    for name, choices in _CHOICES.items():
        if name == "primary_client":
            continue
        if name in form and form[name] not in choices:
            raise HandoffError("invalid_config", 422)
    raw: dict[str, Any] = {}
    for name in _FIELD_NAMES:
        if name in _BOOL_FIELDS:
            raw[name] = form.get(name) == "true"
        elif name in form:
            raw[name] = form[name]
    client = form.get("primary_client")
    if client and client != "manual":
        if client not in _CHOICES["primary_client"]:
            raise HandoffError("invalid_config", 422)
        raw["primary_client"] = client
        # A selected profile owns the defaults; manual insets remain authoritative
        # only when "manual" is selected.
        raw.pop("bar_bottom_inset", None)
        raw.pop("sash_badge_inset", None)
    try:
        return json.loads(canonicalize_config(raw).canonical_json())
    except (TypeError, ValueError) as exc:
        raise HandoffError("invalid_config", 422) from exc


def apply_security_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; style-src 'unsafe-inline'"
    )
    return response


def _failure(exc: HandoffError) -> Response:
    labels = {
        400: "Ongeldig verzoek.",
        403: "De beveiligingscontrole is mislukt.",
        409: "Deze configuratie wordt al opgeslagen.",
        410: "Deze configuratiesessie is verlopen.",
        413: "Het verzoek is te groot.",
        422: "De configuratie bevat ongeldige waarden.",
        503: "BingeCat is tijdelijk niet bereikbaar.",
    }
    body = f"<!doctype html><meta charset=utf-8><title>PostersPlus</title><p>{labels.get(exc.status_code, labels[503])}</p>"
    return apply_security_headers(HTMLResponse(body, status_code=exc.status_code))


def _cookie(request: Request) -> str | None:
    return request.cookies.get(COOKIE_NAME)


@router.post("/bingecat/configurator/session")
async def start_configurator_session(request: Request):
    if not _enabled():
        return apply_security_headers(Response(status_code=404))
    if request.url.query:
        return _failure(HandoffError("invalid_request", 400))
    try:
        form = await _urlencoded_form(request, 1024)
        if set(form) != {"handoff_token"}:
            raise HandoffError("invalid_request", 400)
        handoff_token = form["handoff_token"]
        if (
            not _OPAQUE.fullmatch(handoff_token)
            or len(handoff_token.encode("utf-8")) > MAX_HANDOFF_TOKEN_BYTES
        ):
            raise HandoffError("invalid_request", 400)
        raw = await _callback_client().consume(handoff_token)
        consumed = _validate_consumed(raw, time.time())
        session_token, _csrf_token = _session_store().create(consumed)
        response = Response(
            status_code=303,
            headers={"Location": "/bingecat/configurator"},
        )
        response.set_cookie(
            COOKIE_NAME,
            session_token,
            max_age=max(
                1,
                int(
                    min(consumed.expires_at, time.time() + SESSION_TTL_SECONDS)
                    - time.time()
                ),
            ),
            secure=True,
            httponly=True,
            samesite="lax",
            path=COOKIE_PATH,
        )
        return apply_security_headers(response)
    except HandoffError as exc:
        return _failure(exc)


@router.get("/bingecat/configurator")
async def get_configurator(request: Request):
    if not _enabled():
        return apply_security_headers(Response(status_code=404))
    if request.url.query:
        return _failure(HandoffError("invalid_request", 400))
    try:
        session, csrf_token = _session_store().rotate_csrf(_cookie(request))
        return apply_security_headers(HTMLResponse(_configurator_html(session, csrf_token)))
    except HandoffError as exc:
        return _failure(exc)


@router.post("/bingecat/configurator/save")
async def save_configurator(request: Request):
    if not _enabled():
        return apply_security_headers(Response(status_code=404))
    if request.url.query:
        return _failure(HandoffError("invalid_request", 400))
    session: ConfiguratorSession | None = None
    store: SQLiteConfiguratorSessionStore | None = None
    try:
        form = await _urlencoded_form(request, MAX_FORM_BODY_BYTES)
        csrf_token = form.get("csrf_token")
        store = _session_store()
        session = store.claim_save(_cookie(request), csrf_token)
        canonical = _config_from_form(form)
        config_text = _canonical_json(canonical)
        config_hash = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
        result = await _callback_client().save(
            {
                "schema": CONTRACT_SCHEMA,
                "version": CONTRACT_VERSION,
                "handoff_id": session.handoff_id,
                "save_grant": session.save_grant,
                "canonical_config": canonical,
                "config_hash": config_hash,
                "renderer_revision": RENDERER_REVISION,
            }
        )
        saved = _validate_saved(result, session.return_url)
        try:
            store.complete(session.digest)
        except HandoffError:
            # The remote one-use save already committed.  Never force the user
            # into a second remote save solely because local expiry cleanup is
            # temporarily unavailable.
            logger.warning("BingeCat configurator session cleanup unavailable after save")
        response = Response(status_code=303, headers={"Location": saved["return_url"]})
        response.delete_cookie(
            COOKIE_NAME,
            path=COOKIE_PATH,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return apply_security_headers(response)
    except HandoffError as exc:
        if session is not None and store is not None:
            try:
                store.release_save(session.digest)
            except HandoffError:
                pass
        return _failure(exc)


__all__ = [
    "BingeCatCallbackClient",
    "COOKIE_NAME",
    "COOKIE_PATH",
    "ConsumedHandoff",
    "HandoffError",
    "SQLiteConfiguratorSessionStore",
    "apply_security_headers",
    "router",
]
