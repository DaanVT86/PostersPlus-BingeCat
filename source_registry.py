"""Standalone signed Netcup source-art owner service.

This module intentionally exposes only the source registration protocol and
health endpoint. It does not import the enrichment or renderer applications,
does not download provider URLs, and has no background maintenance task.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from PIL import Image

import config
from service_auth import AuthError, SQLiteNonceStore, verify_request
from source_art import (
    MAX_SOURCE_BYTES,
    RECIPE_VERSIONS,
    SourceArtError,
    SourceArtStore,
    SourceDigestMismatch,
    SourceResourceError,
    SourceVerificationKey,
    SourceVerificationMemo,
    _verification_result,
    normalize_verification_key,
    validate_locator_for_kind,
    validate_source_mount,
    verification_key_signature,
)


_SOURCE_SCHEMA = "postersplus.source_art"
_SOURCE_VERSION = 1
_VERIFICATION_SCHEMA = "postersplus.source_art.verification"
_VERIFICATION_VERSION = 1
_CALLER = "oracle-core"
_AUDIENCE = "postersplus-source-owner"
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceOwnerUnavailable(SourceResourceError):
    pass


class SourceOwnerConflict(SourceArtError):
    pass


class SourceOwnerRuntime:
    def __init__(self) -> None:
        self.store: SourceArtStore | None = None
        self.nonce_store: SQLiteNonceStore | None = None

    def initialize(self) -> None:
        if config.POSTERSPLUS_SOURCE_REGISTRY_MODE != "owner":
            raise SourceOwnerUnavailable("source owner mode is disabled")
        if config.POSTERSPLUS_SOURCE_STORE_MODE != "local":
            raise SourceOwnerUnavailable("source owner requires local ledger mode")
        if not config.POSTERSPLUS_SOURCE_REGISTRY_SECRET:
            raise SourceOwnerUnavailable("source owner secret is not configured")
        _ensure_mounts()
        self.store = SourceArtStore(
            config.SOURCE_ART_CACHE_DIR,
            config.SOURCE_ART_LEDGER_PATH,
            staging_root=config.POSTERSPLUS_SOURCE_INCOMING_DIR,
            require_staging_root=True,
            reclaim_staging=True,
        )
        self.nonce_store = SQLiteNonceStore(
            config.POSTERSPLUS_SOURCE_REGISTRY_NONCE_DB_PATH
        )

    def require_ready(self) -> tuple[SourceArtStore, SQLiteNonceStore]:
        if self.store is None or self.nonce_store is None:
            self.initialize()
        assert self.store is not None
        assert self.nonce_store is not None
        _ensure_mounts()
        return self.store, self.nonce_store


_runtime = SourceOwnerRuntime()


def _ensure_mounts() -> None:
    for path in (
        Path(config.SOURCE_ART_CACHE_DIR),
        Path(config.POSTERSPLUS_SOURCE_INCOMING_DIR),
    ):
        try:
            info = path.lstat()
        except OSError as exc:
            raise SourceOwnerUnavailable("source mount unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SourceOwnerUnavailable("source mount is not a regular directory")
        if config.POSTERSPLUS_SOURCE_REQUIRE_MOUNTS:
            try:
                validate_source_mount(
                    path,
                    expectation="owner-local",
                    mountinfo_path=config.POSTERSPLUS_SOURCE_MOUNTINFO_PATH,
                )
            except SourceArtError as exc:
                raise SourceOwnerUnavailable(
                    "source owner mount is not an exact local mount"
                ) from exc
        try:
            with os.scandir(path) as entries:
                next(entries, None)
        except OSError as exc:
            raise SourceOwnerUnavailable("source mount is unavailable") from exc


def _utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise SourceArtError(f"{field} is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceArtError(f"{field} is invalid") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SourceArtError(f"{field} must be timezone-aware")
    return result.astimezone(timezone.utc)


def _strict_dict(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SourceArtError("source request must be an object")
    return payload


def _envelope(payload: object, schema: str, fields: set[str]) -> dict[str, Any]:
    value = _strict_dict(payload)
    version = value.get("version")
    if (
        set(value) != fields
        or value.get("schema") != schema
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version != 1
    ):
        raise SourceArtError("source request schema is invalid")
    return value


def _sha256(value: object, field: str = "sha256") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SourceArtError(f"{field} is invalid")
    return value


def _int(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SourceArtError(f"{field} is invalid")
    return value


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise SourceArtError(f"{field} is invalid")
    return value


def _token(value: object, field: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        raise SourceArtError(f"{field} is invalid")
    return value


def _staged_path(filename: object) -> Path:
    if not isinstance(filename, str) or not _FILENAME_RE.fullmatch(filename):
        raise SourceArtError("staged_filename is invalid")
    if filename in {".", ".."} or "/" in filename or "\\" in filename or "\x00" in filename:
        raise SourceArtError("staged_filename traverses")
    root = Path(config.POSTERSPLUS_SOURCE_INCOMING_DIR)
    try:
        info = root.lstat()
    except OSError as exc:
        raise SourceOwnerUnavailable("source staging mount unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SourceOwnerUnavailable("source staging mount is unsafe")
    path = root / filename
    try:
        info = path.lstat()
    except OSError as exc:
        raise SourceResourceError("staged file is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SourceArtError("staged file is not a regular file")
    if int(info.st_nlink) != 1:
        raise SourceArtError("staged file has an unexpected hard link")
    return path


def _read_staged(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SourceResourceError("staged file is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or int(info.st_nlink) != 1:
            raise SourceArtError("staged file is not a private regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_SOURCE_BYTES:
            chunk = os.read(fd, min(1024 * 1024, MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            raise SourceResourceError("staged file is too large")
        return b"".join(chunks)
    except OSError as exc:
        raise SourceResourceError("staged file is unreadable") from exc
    finally:
        os.close(fd)


def _validate_normalized(
    *,
    payload: bytes,
    kind: str,
    recipe_version: int,
    expected_digest: str,
    expected_size: int,
    mime: str,
    width: int,
    height: int,
) -> None:
    if (
        not isinstance(kind, str)
        or kind not in RECIPE_VERSIONS
        or isinstance(recipe_version, bool)
        or not isinstance(recipe_version, int)
        or recipe_version != RECIPE_VERSIONS[kind]
    ):
        raise SourceArtError("source recipe is invalid")
    if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_digest:
        raise SourceDigestMismatch("staged source digest mismatch")
    expected_mime = "image/png" if kind == "logo" else "image/jpeg"
    if mime != expected_mime:
        raise SourceArtError("source MIME is invalid for kind")
    if kind in {"poster", "backdrop"} and (width, height) != (500, 750):
        raise SourceArtError("source dimensions are invalid for kind")
    if kind == "logo" and (width > 1000 or height > 400):
        raise SourceArtError("source dimensions are invalid for kind")
    try:
        with Image.open(__import__("io").BytesIO(payload)) as image:
            image.load()
            actual_format = "PNG" if mime == "image/png" else "JPEG"
            if image.format != actual_format or image.size != (width, height):
                raise SourceArtError("source MIME or dimensions do not match payload")
            if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
                raise SourceArtError("animated source images are not allowed")
    except SourceArtError:
        raise
    except (OSError, ValueError) as exc:
        raise SourceArtError("staged source is not a valid image") from exc


def _artifact_relpath(store: SourceArtStore, derivative: Any) -> str:
    try:
        relative = Path(derivative.path).relative_to(store.root)
    except ValueError as exc:
        raise SourceResourceError("registered artifact escaped source root") from exc
    value = relative.as_posix()
    if (
        not re.fullmatch(r"(poster|backdrop|logo)/[0-9a-f]{2}/[0-9a-f]{64}\.(jpg|png)", value)
        or value.split("/")[0] != derivative.kind
        or value.split("/")[2].split(".")[0] != derivative.sha256
    ):
        raise SourceResourceError("registered artifact path is invalid")
    return value


def _derivative_payload(store: SourceArtStore, derivative: Any) -> dict[str, Any]:
    return {
        "source_art_id": derivative.source_art_id,
        "kind": derivative.kind,
        "sha256": derivative.sha256,
        "byte_size": derivative.byte_size,
        "mime": derivative.mime,
        "recipe_version": derivative.recipe_version,
        "artifact_relpath": _artifact_relpath(store, derivative),
        "width": derivative.width,
        "height": derivative.height,
        "locator": derivative.locator.model_dump(mode="json") if derivative.locator else None,
        "created_at": derivative.created_at.astimezone(timezone.utc).isoformat(),
        "last_used_at": derivative.last_used_at.astimezone(timezone.utc).isoformat(),
        "pinned": derivative.pinned,
        "reconstructable": derivative.reconstructable,
    }


def _memo_payload(memo: SourceVerificationMemo) -> dict[str, Any]:
    return {
        "signature": memo.signature,
        "key": memo.key.to_dict(),
        "result": memo.result,
        "verified_at": memo.verified_at.astimezone(timezone.utc).isoformat(),
        "source_sha256": memo.source_sha256,
    }


def _strict_lookup(store: SourceArtStore, sha256: str, kind: str, recipe_version: int):
    with store._connect() as connection:
        row = connection.execute(
            "SELECT path FROM source_art_ledger WHERE sha256=? AND kind=? AND recipe_version=?",
            (sha256, kind, recipe_version),
        ).fetchone()
    if row is not None:
        try:
            info = Path(row[0]).lstat()
        except OSError as exc:
            raise SourceResourceError("registered source artifact is missing") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or int(info.st_nlink) != 1:
            raise SourceDigestMismatch("registered source artifact is unsafe")
    derivative = store.get(sha256, kind, recipe_version)
    if row is not None and derivative is None:
        raise SourceResourceError("registered source artifact is missing")
    return derivative


def _owner_runtime(request: Request) -> tuple[SourceArtStore, SQLiteNonceStore]:
    if config.POSTERSPLUS_SOURCE_REGISTRY_MODE != "owner":
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return _runtime.require_ready()
    except (SourceArtError, OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="source_owner_unavailable") from exc


async def _authenticate(request: Request) -> SourceOwnerRuntime:
    _store, nonce_store = _owner_runtime(request)
    try:
        await verify_request(
            request,
            config.POSTERSPLUS_SOURCE_REGISTRY_SECRET.encode("utf-8"),
            _CALLER,
            _AUDIENCE,
            nonce_store,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
    return _runtime


def _map_error(exc: Exception) -> HTTPException:
    message = str(exc).lower()
    if isinstance(exc, SourceOwnerUnavailable) or "unavailable" in message or "mount" in message:
        return HTTPException(status_code=503, detail="source_owner_unavailable")
    if "capacity" in message or "hard limit" in message:
        return HTTPException(status_code=409, detail="source_capacity_exceeded")
    if "reservation" in message and ("expired" in message or "unknown" in message):
        return HTTPException(status_code=409, detail="reservation_expired")
    if "conflict" in message:
        return HTTPException(status_code=409, detail="verification_conflict")
    if isinstance(exc, SourceDigestMismatch) or "corrupt" in message or "digest" in message:
        return HTTPException(status_code=422, detail="invalid_source_art")
    if isinstance(exc, SourceArtError):
        return HTTPException(status_code=422, detail="invalid_source_request")
    return HTTPException(status_code=503, detail="source_owner_unavailable")


def _body(request: Request) -> dict[str, Any]:
    try:
        raw = request._body  # type: ignore[attr-defined]
        if not isinstance(raw, bytes):
            raise ValueError
        value = json.loads(raw)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid_source_request") from exc
    return _strict_dict(value)


owner_router = APIRouter()


@owner_router.post("/v2/source-art/reserve")
async def reserve_endpoint(request: Request):
    await _authenticate(request)
    payload = _body(request)
    try:
        data = _envelope(
            payload,
            _SOURCE_SCHEMA,
            {"schema", "version", "required_bytes", "ttl_seconds"},
        )
        required_bytes = _int(
            data["required_bytes"],
            "required_bytes",
            minimum=1,
            maximum=MAX_SOURCE_BYTES,
        )
        ttl_seconds = _int(data["ttl_seconds"], "ttl_seconds", minimum=1, maximum=900)
        store, _ = _runtime.require_ready()
        token = await asyncio.to_thread(
            store.reserve_capacity,
            required_bytes,
            ttl_seconds=ttl_seconds,
        )
        with store._connect() as connection:
            expires = connection.execute(
                "SELECT expires_at FROM source_art_capacity_reservations WHERE token=?",
                (token,),
            ).fetchone()
        if expires is None:
            raise SourceResourceError("source reservation disappeared")
        return {
            "schema": _SOURCE_SCHEMA,
            "version": _SOURCE_VERSION,
            "reservation_token": token,
            "expires_at": datetime.fromtimestamp(float(expires[0]), timezone.utc).isoformat(),
            "reserved_bytes": required_bytes,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc


@owner_router.post("/v2/source-art/release")
async def release_endpoint(request: Request):
    await _authenticate(request)
    payload = _body(request)
    try:
        data = _envelope(payload, _SOURCE_SCHEMA, {"schema", "version", "reservation_token"})
        token = _token(data["reservation_token"], "reservation_token")
        store, _ = _runtime.require_ready()
        with store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                connection.execute(
                    "DELETE FROM source_art_capacity_reservations WHERE expires_at<=?",
                    (now,),
                )
                removed = connection.execute(
                    "DELETE FROM source_art_capacity_reservations "
                    "WHERE token=? AND expires_at>?",
                    (token, now),
                ).rowcount
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "schema": _SOURCE_SCHEMA,
            "version": _SOURCE_VERSION,
            "released": bool(removed),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc


@owner_router.post("/v2/source-art/lookup")
async def lookup_endpoint(request: Request):
    await _authenticate(request)
    payload = _body(request)
    try:
        data = _envelope(
            payload,
            _SOURCE_SCHEMA,
            {"schema", "version", "kind", "recipe_version", "sha256"},
        )
        kind = data["kind"]
        if not isinstance(kind, str) or kind not in RECIPE_VERSIONS:
            raise SourceArtError("kind is invalid")
        recipe_version = _int(
            data["recipe_version"],
            "recipe_version",
            minimum=1,
            maximum=65535,
        )
        if recipe_version != RECIPE_VERSIONS[kind]:
            raise SourceArtError("recipe_version is invalid")
        digest = _sha256(data["sha256"])
        store, _ = _runtime.require_ready()
        derivative = await asyncio.to_thread(_strict_lookup, store, digest, kind, recipe_version)
        if derivative is None:
            return {"schema": _SOURCE_SCHEMA, "version": _SOURCE_VERSION, "found": False}
        return {
            "schema": _SOURCE_SCHEMA,
            "version": _SOURCE_VERSION,
            "found": True,
            "derivative": _derivative_payload(store, derivative),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc


@owner_router.post("/v2/source-art/register")
async def register_endpoint(request: Request):
    await _authenticate(request)
    payload = _body(request)
    try:
        data = _envelope(
            payload,
            _SOURCE_SCHEMA,
            {
                "schema",
                "version",
                "reservation_token",
                "staged_filename",
                "kind",
                "recipe_version",
                "sha256",
                "byte_size",
                "mime",
                "width",
                "height",
                "locator",
                "pinned",
                "reconstructable",
            },
        )
        token = _token(data["reservation_token"], "reservation_token")
        staged = _staged_path(data["staged_filename"])
        kind = data["kind"]
        if not isinstance(kind, str) or kind not in RECIPE_VERSIONS:
            raise SourceArtError("kind is invalid")
        recipe_version = _int(data["recipe_version"], "recipe_version", minimum=1, maximum=65535)
        digest = _sha256(data["sha256"])
        byte_size = _int(data["byte_size"], "byte_size", minimum=1, maximum=MAX_SOURCE_BYTES)
        width = _int(data["width"], "width", minimum=1, maximum=8192)
        height = _int(data["height"], "height", minimum=1, maximum=8192)
        mime = data["mime"]
        if not isinstance(mime, str) or mime not in {"image/jpeg", "image/png"}:
            raise SourceArtError("mime is invalid")
        pinned = _bool(data["pinned"], "pinned")
        reconstructable = _bool(data["reconstructable"], "reconstructable")
        locator = None
        if data["locator"] is not None:
            try:
                from integration_contract import ArtworkLocator

                locator = ArtworkLocator.model_validate(data["locator"])
                validate_locator_for_kind(locator, kind)
            except Exception as exc:
                raise SourceArtError("locator is invalid") from exc
        if reconstructable and locator is None:
            raise SourceArtError("reconstructable source requires locator")
        if not reconstructable and locator is not None:
            # Keep a supplied locator only when it is explicitly useful; this
            # preserves legacy references while rejecting ambiguous metadata.
            pass
        raw = _read_staged(staged)
        _validate_normalized(
            payload=raw,
            kind=kind,
            recipe_version=recipe_version,
            expected_digest=digest,
            expected_size=byte_size,
            mime=mime,
            width=width,
            height=height,
        )
        store, _ = _runtime.require_ready()
        derivative = await asyncio.to_thread(
            store.install,
            kind=kind,
            recipe_version=recipe_version,
            payload=raw,
            mime=mime,
            width=width,
            height=height,
            locator=locator,
            now=datetime.now(timezone.utc),
            pinned=pinned,
            reconstructable=reconstructable,
            reservation_token=token,
            staged_path=staged,
        )
        try:
            staged.unlink()
        except OSError:
            pass
        return {
            "schema": _SOURCE_SCHEMA,
            "version": _SOURCE_VERSION,
            "found": True,
            "derivative": _derivative_payload(store, derivative),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc


def _verification_request(request: Request) -> tuple[dict[str, Any], SourceVerificationKey]:
    payload = _body(request)
    data = _envelope(
        payload,
        _VERIFICATION_SCHEMA,
        {"schema", "version", "key", "signature"},
    )
    key = normalize_verification_key(data["key"])
    signature = data["signature"]
    if signature != verification_key_signature(key):
        raise SourceArtError("verification signature is invalid")
    return data, key


@owner_router.post("/v2/source-art/verification-lookup")
async def verification_lookup_endpoint(request: Request):
    await _authenticate(request)
    try:
        _, key = _verification_request(request)
        store, _ = _runtime.require_ready()
        memo = await asyncio.to_thread(store.lookup_verification, key)
        if memo is None:
            return {
                "schema": _VERIFICATION_SCHEMA,
                "version": _VERIFICATION_VERSION,
                "found": False,
            }
        return {
            "schema": _VERIFICATION_SCHEMA,
            "version": _VERIFICATION_VERSION,
            "found": True,
            "memo": _memo_payload(memo),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc


@owner_router.post("/v2/source-art/verification-register")
async def verification_register_endpoint(request: Request):
    await _authenticate(request)
    try:
        payload = _body(request)
        data = _envelope(
            payload,
            _VERIFICATION_SCHEMA,
            {"schema", "version", "key", "signature", "result", "verified_at"},
        )
        key = normalize_verification_key(data["key"])
        if data["signature"] != verification_key_signature(key):
            raise SourceArtError("verification signature is invalid")
        result = _verification_result(data["result"])
        verified_at = _utc_timestamp(data["verified_at"], "verified_at")
        store, _ = _runtime.require_ready()
        memo = await asyncio.to_thread(
            store.register_verification,
            key,
            result=result,
            verified_at=verified_at,
        )
        return {
            "schema": _VERIFICATION_SCHEMA,
            "version": _VERIFICATION_VERSION,
            "found": True,
            "memo": _memo_payload(memo),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        _runtime.initialize()
    except (SourceArtError, OSError, sqlite3.Error):
        # Keep the health endpoint available so an operator can distinguish a
        # missing mount/configuration from a process that failed to start.
        pass
    yield


app = FastAPI(
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(owner_router)


@app.get("/health")
async def health_endpoint():
    try:
        _runtime.require_ready()
    except (SourceArtError, OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="source_owner_unavailable") from exc
    return {"status": "ok", "role": "source-owner"}


router = owner_router

__all__ = ["app", "owner_router", "router"]
