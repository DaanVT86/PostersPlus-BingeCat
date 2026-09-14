"""Oracle adapter for the Netcup source-art owner with no local ledger."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from integration_contract import ArtworkLocator
from service_auth import build_auth_headers
from source_art import (
    MAX_SOURCE_BYTES,
    RECIPE_VERSIONS,
    SourceArtError,
    SourceDerivative,
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


_SOURCE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ARTIFACT_RELPATH_RE = re.compile(
    r"^(poster|backdrop|logo)/([0-9a-f]{2})/([0-9a-f]{64})\.(jpg|png)$"
)
_SOURCE_ACTIONS = frozenset(
    {
        "reserve",
        "lookup",
        "register",
        "release",
        "verification-lookup",
        "verification-register",
    }
)
_SOURCE_CALLER = "oracle-core"
_SOURCE_AUDIENCE = "postersplus-source-owner"


class RemoteSourceRegistryError(SourceResourceError):
    """A bounded failure returned by or raised while contacting the owner."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _utc_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise SourceArtError(f"source owner {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceArtError(f"source owner {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceArtError(f"source owner {field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


class RemoteSourceArtStore:
    """SourceArtStore-compatible client with no local source ledger.

    The only writable path used by this adapter is the configured incoming
    staging directory. Published artifacts are read-only files on the mounted
    cache and all ownership decisions are made by the signed owner API.
    """

    def __init__(
        self,
        registry_url: str,
        secret: str,
        cache_root: str | os.PathLike[str],
        incoming_root: str | os.PathLike[str],
        *,
        timeout_seconds: float = 15.0,
        require_mounts: bool = True,
        mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
        requester: Callable[[str, str, bytes, dict[str, str]], tuple[int, object]] | None = None,
    ) -> None:
        self.registry_url = registry_url.rstrip("/")
        self.secret = secret
        self.root = Path(incoming_root)
        self.artifact_root = Path(cache_root)
        # Raw provider bytes must land directly on RW incoming NFS so the
        # owner's bounded staging accounting sees them before registration.
        self.download_root = self.root
        self.ledger_path = None
        self.timeout_seconds = max(1.0, min(30.0, float(timeout_seconds)))
        self.require_mounts = bool(require_mounts)
        self.mountinfo_path = Path(mountinfo_path)
        self._requester = requester
        self._validate_registry_url()

    @classmethod
    def from_config(cls) -> "RemoteSourceArtStore":
        import config

        if config.POSTERSPLUS_SOURCE_STORE_MODE != "remote":
            raise SourceArtError("remote source store is not enabled")
        if not config.POSTERSPLUS_SOURCE_REGISTRY_URL:
            raise SourceResourceError("source registry URL is not configured")
        if not config.POSTERSPLUS_SOURCE_REGISTRY_SECRET:
            raise SourceResourceError("source registry secret is not configured")
        return cls(
            config.POSTERSPLUS_SOURCE_REGISTRY_URL,
            config.POSTERSPLUS_SOURCE_REGISTRY_SECRET,
            config.SOURCE_ART_CACHE_DIR,
            config.POSTERSPLUS_SOURCE_INCOMING_DIR,
            timeout_seconds=config.POSTERSPLUS_SOURCE_REGISTRY_TIMEOUT_SECONDS,
            require_mounts=config.POSTERSPLUS_SOURCE_REQUIRE_MOUNTS,
            mountinfo_path=config.POSTERSPLUS_SOURCE_MOUNTINFO_PATH,
        )

    def _validate_registry_url(self) -> None:
        parsed = urlsplit(self.registry_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SourceResourceError("source registry URL is invalid")

    def _ensure_mounts(self) -> None:
        for path, writable in (
            (self.artifact_root, False),
            (self.root, True),
        ):
            try:
                info = path.lstat()
            except OSError as exc:
                raise SourceResourceError("source NFS mount unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SourceResourceError("source NFS mount is not a regular directory")
            if self.require_mounts:
                try:
                    validate_source_mount(
                        path,
                        expectation="oracle-nfs",
                        mountinfo_path=self.mountinfo_path,
                    )
                except SourceArtError as exc:
                    raise SourceResourceError(
                        "source path is not an exact NFS mount"
                    ) from exc
            if not os.access(path, os.R_OK | (os.W_OK if writable else 0)):
                raise SourceResourceError("source NFS mount has insufficient access")
            try:
                with os.scandir(path) as entries:
                    next(entries, None)
            except OSError as exc:
                raise SourceResourceError("source NFS mount is unavailable") from exc

    def _path(self, action: str) -> str:
        if action not in _SOURCE_ACTIONS:
            raise SourceArtError("invalid source registry action")
        return f"/v2/source-art/{action}"

    def _url(self, action: str) -> str:
        parsed = urlsplit(self.registry_url)
        base = parsed.path.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}{base}{self._path(action)}"

    @staticmethod
    def _json_body(payload: Mapping[str, Any]) -> bytes:
        try:
            return json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SourceArtError("source registry payload is not JSON-serializable") from exc

    def _post(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = self._json_body(payload)
        path = self._path(action)
        headers = build_auth_headers(
            method="POST",
            path=path,
            body=body,
            request_id=uuid.uuid4(),
            timestamp=int(__import__("time").time()),
            secret=self.secret.encode("utf-8"),
            caller=_SOURCE_CALLER,
            audience=_SOURCE_AUDIENCE,
        )
        headers["Content-Type"] = "application/json"
        url = self._url(action)
        try:
            if self._requester is not None:
                status_code, raw = self._requester(action, url, body, headers)
                response_payload = raw
            else:
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    response = client.post(url, content=body, headers=headers)
                status_code = response.status_code
                try:
                    response_payload = response.json()
                except ValueError as exc:
                    raise RemoteSourceRegistryError(
                        "source registry returned invalid JSON",
                        status_code=status_code,
                    ) from exc
        except RemoteSourceRegistryError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise RemoteSourceRegistryError("source registry is unavailable") from exc

        if not isinstance(response_payload, dict):
            raise RemoteSourceRegistryError(
                "source registry returned an invalid response",
                status_code=status_code,
            )
        if status_code >= 400:
            detail = response_payload.get("detail")
            code = detail if isinstance(detail, str) else "source_owner_unavailable"
            if status_code == 409 and code == "source_capacity_exceeded":
                raise RemoteSourceRegistryError(
                    code, status_code=status_code, code=code
                )
            raise RemoteSourceRegistryError(
                code, status_code=status_code, code=code
            )
        expected_schema = (
            "postersplus.source_art.verification"
            if action.startswith("verification-")
            else "postersplus.source_art"
        )
        if (
            response_payload.get("schema") != expected_schema
            or isinstance(response_payload.get("version"), bool)
            or response_payload.get("version") != 1
        ):
            raise RemoteSourceRegistryError(
                "source registry returned an invalid envelope",
                status_code=status_code,
            )
        return response_payload

    @staticmethod
    def _envelope(schema: str) -> dict[str, Any]:
        return {"schema": schema, "version": 1}

    @staticmethod
    def _validate_sha256(value: object, field: str = "sha256") -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise SourceArtError(f"{field} is invalid")
        return value

    @staticmethod
    def _validate_artifact_relpath(value: object) -> str:
        if not isinstance(value, str) or not _ARTIFACT_RELPATH_RE.fullmatch(value):
            raise SourceResourceError("source owner artifact path is invalid")
        if "\\" in value or any(part in {".", ".."} for part in value.split("/")):
            raise SourceResourceError("source owner artifact path traverses")
        return value

    def _artifact_path(self, relative: str) -> Path:
        relative = self._validate_artifact_relpath(relative)
        if self.artifact_root.is_symlink() or not self.artifact_root.is_dir():
            raise SourceResourceError("source artifact mount unavailable")
        parts = relative.split("/")
        current = self.artifact_root
        for component in parts[:-1]:
            current = current / component
            try:
                info = current.lstat()
            except OSError as exc:
                raise SourceResourceError("source artifact path is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SourceResourceError("source artifact path is unsafe")
        destination = current / parts[-1]
        try:
            info = destination.lstat()
        except OSError as exc:
            raise SourceResourceError("source artifact is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SourceResourceError("source artifact is not a regular file")
        if int(info.st_nlink) != 1:
            raise SourceResourceError("source artifact has an unexpected hard link")
        return destination

    @staticmethod
    def _hash_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(128 * 1024), b""):
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        raise SourceResourceError("source artifact is too large")
                    digest.update(chunk)
        except OSError as exc:
            raise SourceResourceError("source artifact is unreadable") from exc
        return total, digest.hexdigest()

    def _verify_artifact(self, derivative: SourceDerivative, relative: str) -> None:
        path = self._artifact_path(relative)
        size, digest = self._hash_file(path)
        if size != derivative.byte_size or digest != derivative.sha256:
            raise SourceDigestMismatch("source artifact digest mismatch")
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.load()
                expected_format = "JPEG" if derivative.mime == "image/jpeg" else "PNG"
                if image.format != expected_format or image.size != (derivative.width, derivative.height):
                    raise SourceDigestMismatch("source artifact metadata mismatch")
                if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
                    raise SourceDigestMismatch("animated source artifact is not allowed")
        except SourceArtError:
            raise
        except (OSError, ValueError) as exc:
            raise SourceDigestMismatch("source artifact is not a valid image") from exc

    @staticmethod
    def _derivative_from_wire(payload: Mapping[str, Any], artifact_root: Path) -> tuple[SourceDerivative, str]:
        required = {
            "source_art_id",
            "kind",
            "sha256",
            "byte_size",
            "mime",
            "recipe_version",
            "artifact_relpath",
            "width",
            "height",
            "locator",
            "created_at",
            "last_used_at",
            "pinned",
            "reconstructable",
        }
        if set(payload) != required:
            raise SourceArtError("source owner derivative fields are invalid")
        kind = payload["kind"]
        if not isinstance(kind, str) or kind not in RECIPE_VERSIONS:
            raise SourceArtError("source owner derivative kind is invalid")
        recipe_version = payload["recipe_version"]
        if (
            isinstance(recipe_version, bool)
            or not isinstance(recipe_version, int)
            or recipe_version != RECIPE_VERSIONS[kind]
        ):
            raise SourceArtError("source owner derivative recipe is invalid")
        sha256 = RemoteSourceArtStore._validate_sha256(payload["sha256"])
        source_art_id = payload["source_art_id"]
        if source_art_id != f"{kind}-r{recipe_version}-{sha256}":
            raise SourceArtError("source owner derivative id is invalid")
        mime = payload["mime"]
        if not isinstance(mime, str) or mime not in {"image/jpeg", "image/png"}:
            raise SourceArtError("source owner derivative MIME is invalid")
        if (kind in {"poster", "backdrop"} and mime != "image/jpeg") or (
            kind == "logo" and mime != "image/png"
        ):
            raise SourceArtError("source owner derivative MIME is invalid")
        byte_size = payload["byte_size"]
        width = payload["width"]
        height = payload["height"]
        for value, field in ((byte_size, "byte_size"), (width, "width"), (height, "height")):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SourceArtError(f"source owner {field} is invalid")
        if byte_size > MAX_SOURCE_BYTES:
            raise SourceResourceError("source owner derivative is too large")
        if kind in {"poster", "backdrop"} and (width, height) != (500, 750):
            raise SourceArtError("source owner derivative dimensions are invalid")
        if kind == "logo" and (width > 1000 or height > 400):
            raise SourceArtError("source owner derivative dimensions are invalid")
        relative = RemoteSourceArtStore._validate_artifact_relpath(payload["artifact_relpath"])
        if relative.split("/")[0] != kind or relative.split("/")[2].split(".")[0] != sha256:
            raise SourceArtError("source owner artifact path does not match digest")
        locator_payload = payload["locator"]
        locator = None
        if locator_payload is not None:
            try:
                locator = ArtworkLocator.model_validate(locator_payload)
                validate_locator_for_kind(locator, kind)
            except Exception as exc:
                raise SourceArtError("source owner locator is invalid") from exc
        reconstructable = payload["reconstructable"]
        if not isinstance(reconstructable, bool):
            raise SourceArtError("source owner reconstructable flag is invalid")
        if reconstructable and locator is None:
            raise SourceArtError("reconstructable source owner derivative lacks locator")
        if not isinstance(payload["pinned"], bool):
            raise SourceArtError("source owner pinned flag is invalid")
        derivative = SourceDerivative(
            source_art_id=source_art_id,
            kind=kind,
            sha256=sha256,
            byte_size=byte_size,
            mime=mime,
            recipe_version=recipe_version,
            path=str(artifact_root / relative),
            width=width,
            height=height,
            created_at=_utc_datetime(payload["created_at"], "created_at"),
            last_used_at=_utc_datetime(payload["last_used_at"], "last_used_at"),
            locator=locator,
            pinned=payload["pinned"],
            reconstructable=reconstructable,
        )
        return derivative, relative

    def reserve_capacity(self, required_bytes: int) -> str:
        self._ensure_mounts()
        if isinstance(required_bytes, bool) or not isinstance(required_bytes, int):
            raise SourceResourceError("invalid source cache reservation")
        if required_bytes <= 0 or required_bytes > MAX_SOURCE_BYTES:
            raise SourceResourceError("invalid source cache reservation")
        payload = {
            **self._envelope("postersplus.source_art"),
            "required_bytes": required_bytes,
            "ttl_seconds": 300,
        }
        response = self._post("reserve", payload)
        token = response.get("reservation_token")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 128
            or not re.fullmatch(r"[A-Za-z0-9_-]+", token)
        ):
            raise SourceArtError("source owner reservation token is invalid")
        reserved_bytes = response.get("reserved_bytes")
        if reserved_bytes != required_bytes:
            raise SourceArtError("source owner reservation size mismatch")
        _utc_datetime(response.get("expires_at"), "expires_at")
        return token

    def release_capacity(self, token: str) -> None:
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 128
            or not re.fullmatch(r"[A-Za-z0-9_-]+", token)
        ):
            return
        try:
            self._ensure_mounts()
            response = self._post(
                "release",
                {
                    **self._envelope("postersplus.source_art"),
                    "reservation_token": token,
                },
            )
            if not isinstance(response.get("released"), bool):
                raise SourceArtError("source owner release response is invalid")
        except SourceArtError:
            # Preserve the legacy release shape: a cleanup failure must not
            # replace the original download/normalization exception.
            return

    def get(
        self,
        sha256: str,
        kind: str,
        recipe_version: int,
        *,
        now: Any = None,
    ) -> SourceDerivative | None:
        self._ensure_mounts()
        sha256 = self._validate_sha256(sha256)
        if (
            not isinstance(kind, str)
            or kind not in RECIPE_VERSIONS
            or isinstance(recipe_version, bool)
            or not isinstance(recipe_version, int)
            or recipe_version != RECIPE_VERSIONS[kind]
        ):
            raise SourceArtError("source lookup recipe is invalid")
        response = self._post(
            "lookup",
            {
                **self._envelope("postersplus.source_art"),
                "kind": kind,
                "recipe_version": recipe_version,
                "sha256": sha256,
            },
        )
        if response.get("found") is False:
            return None
        if response.get("found") is not True or not isinstance(response.get("derivative"), dict):
            raise SourceArtError("source owner lookup response is invalid")
        derivative, relative = self._derivative_from_wire(response["derivative"], self.artifact_root)
        self._verify_artifact(derivative, relative)
        return derivative

    def _write_staged(self, payload: bytes, kind: str, mime: str) -> str:
        self._ensure_mounts()
        suffix = "jpg" if mime == "image/jpeg" else "png"
        filename = f"normalized-{kind}-{uuid.uuid4().hex}.{suffix}"
        if not _SOURCE_FILENAME_RE.fullmatch(filename):
            raise SourceArtError("generated staging filename is invalid")
        target = self.root / filename
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
        except OSError as exc:
            raise SourceResourceError("source staging file could not be created") from exc
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return filename

    def install(
        self,
        *,
        kind: str,
        recipe_version: int,
        payload: bytes,
        mime: str,
        width: int,
        height: int,
        locator: ArtworkLocator | None,
        now: Any,
        pinned: bool,
        reconstructable: bool,
        reservation_token: str | None = None,
    ) -> SourceDerivative:
        if (
            not isinstance(kind, str)
            or kind not in RECIPE_VERSIONS
            or isinstance(recipe_version, bool)
            or not isinstance(recipe_version, int)
            or recipe_version != RECIPE_VERSIONS[kind]
        ):
            raise SourceArtError("source registration recipe is invalid")
        if not isinstance(payload, bytes) or not payload or len(payload) > MAX_SOURCE_BYTES:
            raise SourceResourceError("invalid source derivative payload")
        if not isinstance(mime, str) or mime not in {"image/jpeg", "image/png"}:
            raise SourceArtError("source registration MIME is invalid")
        if (kind in {"poster", "backdrop"} and mime != "image/jpeg") or (
            kind == "logo" and mime != "image/png"
        ):
            raise SourceArtError("source registration MIME is invalid")
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            raise SourceArtError("source registration width is invalid")
        if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
            raise SourceArtError("source registration height is invalid")
        if not isinstance(pinned, bool) or not isinstance(reconstructable, bool):
            raise SourceArtError("source registration flags are invalid")
        if reconstructable and locator is None:
            raise SourceArtError("reconstructable source registration lacks locator")
        digest = hashlib.sha256(payload).hexdigest()
        owned_reservation = False
        if reservation_token is None:
            reservation_token = self.reserve_capacity(MAX_SOURCE_BYTES)
            owned_reservation = True
        elif not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reservation_token):
            raise SourceResourceError("invalid source cache reservation")
        staged_filename: str | None = None
        try:
            staged_filename = self._write_staged(payload, kind, mime)
            response = self._post(
                "register",
                {
                    **self._envelope("postersplus.source_art"),
                    "reservation_token": reservation_token,
                    "staged_filename": staged_filename,
                    "kind": kind,
                    "recipe_version": recipe_version,
                    "sha256": digest,
                    "byte_size": len(payload),
                    "mime": mime,
                    "width": width,
                    "height": height,
                    "locator": locator.model_dump(mode="json") if locator is not None else None,
                    "pinned": pinned,
                    "reconstructable": reconstructable,
                },
            )
            if response.get("found") is not True or not isinstance(response.get("derivative"), dict):
                raise SourceArtError("source owner registration response is invalid")
            derivative, relative = self._derivative_from_wire(response["derivative"], self.artifact_root)
            if derivative.sha256 != digest:
                raise SourceDigestMismatch("source owner registered an unexpected digest")
            self._verify_artifact(derivative, relative)
            return derivative
        finally:
            if staged_filename is not None:
                try:
                    (self.root / staged_filename).unlink()
                except OSError:
                    pass
            if owned_reservation:
                self.release_capacity(reservation_token)

    def lookup_verification(
        self,
        key: SourceVerificationKey | Mapping[str, Any],
    ) -> SourceVerificationMemo | None:
        self._ensure_mounts()
        normalized = normalize_verification_key(key)
        response = self._post(
            "verification-lookup",
            {
                "schema": "postersplus.source_art.verification",
                "version": 1,
                "key": normalized.to_dict(),
                "signature": verification_key_signature(normalized),
            },
        )
        if response.get("found") is False:
            return None
        memo = response.get("memo")
        if response.get("found") is not True or not isinstance(memo, dict):
            raise SourceArtError("source owner verification lookup response is invalid")
        return self._memo_from_wire(memo, normalized)

    def register_verification(
        self,
        key: SourceVerificationKey | Mapping[str, Any],
        *,
        result: str,
        verified_at: datetime | None = None,
    ) -> SourceVerificationMemo:
        self._ensure_mounts()
        normalized = normalize_verification_key(key)
        result = _verification_result(result)
        observed = verified_at or datetime.now(timezone.utc)
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise SourceArtError("verification timestamp must be timezone-aware")
        response = self._post(
            "verification-register",
            {
                "schema": "postersplus.source_art.verification",
                "version": 1,
                "key": normalized.to_dict(),
                "signature": verification_key_signature(normalized),
                "result": result,
                "verified_at": observed.astimezone(timezone.utc).isoformat(),
            },
        )
        memo = response.get("memo")
        if response.get("found") is not True or not isinstance(memo, dict):
            raise SourceArtError("source owner verification registration response is invalid")
        return self._memo_from_wire(memo, normalized)

    @staticmethod
    def _memo_from_wire(
        payload: Mapping[str, Any],
        expected_key: SourceVerificationKey,
    ) -> SourceVerificationMemo:
        required = {"signature", "key", "result", "verified_at", "source_sha256"}
        if set(payload) != required:
            raise SourceArtError("source owner verification memo fields are invalid")
        key = normalize_verification_key(payload["key"])
        signature = payload["signature"]
        if signature != verification_key_signature(key) or key != expected_key:
            raise SourceArtError("source owner verification memo signature mismatch")
        source_sha256 = payload["source_sha256"]
        if source_sha256 != key.source_sha256:
            raise SourceArtError("source owner verification source mismatch")
        return SourceVerificationMemo(
            signature=signature,
            key=key,
            result=_verification_result(payload["result"]),
            verified_at=_utc_datetime(payload["verified_at"], "verified_at"),
            source_sha256=source_sha256,
        )


__all__ = [
    "RemoteSourceArtStore",
    "RemoteSourceRegistryError",
]
