"""SSRF-safe source retrieval and deterministic content-addressed derivatives."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import stat
import ssl
import tempfile
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Literal, Mapping
from urllib.parse import unquote, urljoin, urlsplit

from PIL import Image, ImageOps

from integration_contract import ArtworkLocator


MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_AXIS = 8192
MAX_REDIRECTS = 2
CAPACITY_RESERVATION_SECONDS = 300
STAGING_RECLAIM_SCAN_LIMIT = 4096
STAGING_RESERVATION_CLOCK_SKEW_SECONDS = 60
RECIPE_VERSIONS = {"poster": 1, "backdrop": 5, "logo": 1}
SOURCE_ART_SCHEMA = "postersplus.source_art"
SOURCE_ART_VERSION = 1
SOURCE_VERIFICATION_SCHEMA = "postersplus.source_art.verification"
SOURCE_VERIFICATION_VERSION = 1
MAX_RESERVATION_TOKEN_LENGTH = 128
MAX_VERIFICATION_TOKEN_LENGTH = 80
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,79}$")
_STAGED_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RECLAIMABLE_STAGED_FILENAME_RE = re.compile(
    r"(?:raw-[A-Za-z0-9_-]{6}|normalized-(?:poster|backdrop|logo)-[0-9a-f]{32}\.(?:jpg|png))"
)
_ARTIFACT_RELPATH_RE = re.compile(
    r"^(poster|backdrop|logo)/([0-9a-f]{2})/([0-9a-f]{64})\.(jpg|png)$"
)
_source_download_semaphore: asyncio.Semaphore | None = None
_source_download_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_source_download_semaphore() -> asyncio.Semaphore:
    """Return the per-process source download guard for the active loop."""

    global _source_download_semaphore, _source_download_semaphore_loop
    loop = asyncio.get_running_loop()
    if _source_download_semaphore is None or _source_download_semaphore_loop is not loop:
        try:
            import config

            limit = int(config.POSTERSPLUS_SOURCE_DOWNLOAD_CONCURRENCY)
        except (ImportError, AttributeError, TypeError, ValueError):
            limit = 2
        _source_download_semaphore = asyncio.Semaphore(max(1, min(2, limit)))
        _source_download_semaphore_loop = loop
    return _source_download_semaphore


class SourceArtError(RuntimeError):
    pass


class SourceSecurityError(SourceArtError):
    pass


class SourceResourceError(SourceArtError):
    pass


class SourceDigestMismatch(SourceArtError):
    pass


@dataclass(frozen=True)
class SourceMountInfo:
    """Decoded mountinfo fields for one exact mountpoint."""

    mountpoint: str
    root: str
    filesystem: str
    source: str


_MOUNTINFO_ESCAPES = {
    "040": " ",
    "011": "\t",
    "012": "\n",
    "134": "\\",
}
_MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_NETWORK_FILESYSTEMS = frozenset(
    {
        "9p",
        "ceph",
        "cifs",
        "fuse.glusterfs",
        "fuse.sshfs",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smb3",
    }
)
_LOCAL_FILESYSTEMS = frozenset(
    {
        "aufs",
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "hfs",
        "hfsplus",
        "ntfs",
        "overlay",
        "tmpfs",
        "xfs",
        "zfs",
    }
)


def _decode_mountinfo_field(value: str) -> str:
    return _MOUNTINFO_ESCAPE_RE.sub(
        lambda match: _MOUNTINFO_ESCAPES.get(match.group(1), match.group(0)),
        value,
    )


def read_source_mountinfo(
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
) -> tuple[SourceMountInfo, ...]:
    """Read and decode Linux mountinfo without following the target path."""

    try:
        raw = Path(mountinfo_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SourceResourceError("source mount information is unavailable") from exc

    records: list[SourceMountInfo] = []
    for line in raw.splitlines():
        pre, separator, post = line.partition(" - ")
        if not separator:
            continue
        pre_fields = pre.split()
        post_fields = post.split()
        if len(pre_fields) < 6 or len(post_fields) < 2:
            continue
        records.append(
            SourceMountInfo(
                mountpoint=_decode_mountinfo_field(pre_fields[4]),
                root=_decode_mountinfo_field(pre_fields[3]),
                filesystem=_decode_mountinfo_field(post_fields[0]).lower(),
                source=_decode_mountinfo_field(post_fields[1]),
            )
        )
    return tuple(records)


def exact_source_mount(
    path: str | os.PathLike[str],
    *,
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
) -> SourceMountInfo | None:
    """Return the last mountinfo record whose target equals ``path``."""

    target = os.path.normpath(os.fspath(path))
    match: SourceMountInfo | None = None
    for record in read_source_mountinfo(mountinfo_path):
        if record.mountpoint == target:
            match = record
    return match


def validate_source_mount(
    path: str | os.PathLike[str],
    *,
    expectation: Literal["oracle-nfs", "owner-local"],
    mountinfo_path: str | os.PathLike[str] = "/proc/self/mountinfo",
) -> SourceMountInfo:
    """Require an exact source mount with role-specific filesystem policy."""

    record = exact_source_mount(path, mountinfo_path=mountinfo_path)
    if record is None:
        raise SourceResourceError("source mount is not an exact mountpoint")
    if expectation == "oracle-nfs":
        if record.filesystem not in {"nfs", "nfs4"}:
            raise SourceResourceError("Oracle source mount is not NFS")
    elif expectation == "owner-local":
        if (
            record.filesystem in _NETWORK_FILESYSTEMS
            or record.filesystem not in _LOCAL_FILESYSTEMS
        ):
            raise SourceResourceError("source owner mount is not a local filesystem")
    else:  # pragma: no cover - Literal callers should make this unreachable.
        raise SourceResourceError("source mount expectation is invalid")
    return record


@dataclass(frozen=True)
class PinnedHTTPResponse:
    status_code: int
    headers: Mapping[str, str]
    chunks: tuple[bytes, ...]
    peer_ip: str


@dataclass(frozen=True)
class DownloadedSource:
    path: Path
    content_type: str
    locator: ArtworkLocator


@dataclass(frozen=True)
class SourceDerivative:
    source_art_id: str
    kind: Literal["poster", "backdrop", "logo"]
    sha256: str
    byte_size: int
    mime: Literal["image/jpeg", "image/png"]
    recipe_version: int
    path: str
    width: int
    height: int
    created_at: datetime
    last_used_at: datetime
    locator: ArtworkLocator | None = None
    pinned: bool = False
    reconstructable: bool = False


@dataclass(frozen=True)
class SourceVerificationKey:
    """Stable OCR memo identity shared by local and remote source stores."""

    kind: Literal["poster", "backdrop"]
    source_sha256: str
    title_context_sha256: str
    detection_rules: str
    model: str
    runtime: str
    runtime_version: str
    architecture: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "source_sha256": self.source_sha256,
            "title_context_sha256": self.title_context_sha256,
            "detection_rules": self.detection_rules,
            "model": self.model,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "architecture": self.architecture,
        }

    @property
    def signature(self) -> str:
        return verification_key_signature(self)


@dataclass(frozen=True)
class SourceVerificationMemo:
    signature: str
    key: SourceVerificationKey
    result: Literal["textless", "text", "unknown"]
    verified_at: datetime
    source_sha256: str


def _verification_key(value: SourceVerificationKey | Mapping[str, Any]) -> SourceVerificationKey:
    if isinstance(value, SourceVerificationKey):
        candidate = value
    elif isinstance(value, Mapping):
        expected = {
            "kind",
            "source_sha256",
            "title_context_sha256",
            "detection_rules",
            "model",
            "runtime",
            "runtime_version",
            "architecture",
        }
        if set(value) != expected:
            raise SourceArtError("source verification key fields are invalid")
        try:
            candidate = SourceVerificationKey(
                kind=value["kind"],
                source_sha256=value["source_sha256"],
                title_context_sha256=value["title_context_sha256"],
                detection_rules=value["detection_rules"],
                model=value["model"],
                runtime=value["runtime"],
                runtime_version=value["runtime_version"],
                architecture=value["architecture"],
            )
        except (KeyError, TypeError) as exc:
            raise SourceArtError("source verification key is invalid") from exc
    else:
        raise SourceArtError("source verification key is invalid")

    if not isinstance(candidate.kind, str) or candidate.kind not in {"poster", "backdrop"}:
        raise SourceArtError("OCR verification kind is invalid")
    for name in ("source_sha256", "title_context_sha256"):
        raw = getattr(candidate, name)
        if not isinstance(raw, str) or not _SHA256_RE.fullmatch(raw):
            raise SourceArtError(f"OCR verification {name} is invalid")
    for name in (
        "detection_rules",
        "model",
        "runtime",
        "runtime_version",
        "architecture",
    ):
        raw = getattr(candidate, name)
        if not isinstance(raw, str) or not _TOKEN_RE.fullmatch(raw):
            raise SourceArtError(f"OCR verification {name} is invalid")
    return candidate


def verification_key_signature(
    key: SourceVerificationKey | Mapping[str, Any],
) -> str:
    normalized = _verification_key(key).to_dict()
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def normalize_verification_key(
    key: SourceVerificationKey | Mapping[str, Any],
) -> SourceVerificationKey:
    return _verification_key(key)


def _verification_result(value: object) -> Literal["textless", "text", "unknown"]:
    if not isinstance(value, str) or value not in {"textless", "text", "unknown"}:
        raise SourceArtError("OCR verification result is invalid")
    return value  # type: ignore[return-value]


def _utc(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("source-art timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def validate_locator_for_kind(
    locator: ArtworkLocator,
    kind: Literal["poster", "backdrop", "logo"],
) -> None:
    parsed = urlsplit(locator.url)
    try:
        decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceSecurityError("invalid artwork path encoding") from exc
    if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        raise SourceSecurityError("artwork path traversal rejected")

    if locator.provider == "tmdb":
        match = re.fullmatch(
            r"/t/p/(original|w300|w342|w500|w780|w1280)/[A-Za-z0-9_./-]+\.(?:jpe?g|png|webp|svg)",
            decoded,
            flags=re.IGNORECASE,
        )
        if not match:
            raise SourceSecurityError("TMDB artwork path is not allowlisted")
        size = match.group(1).lower()
        if decoded.lower().endswith(".svg") and kind != "logo":
            raise SourceSecurityError("SVG source art is restricted to logos")
        allowed_sizes = {
            "poster": {"original", "w342", "w500", "w780"},
            "backdrop": {"original", "w780", "w1280"},
            "logo": {"original", "w300", "w500", "w780"},
        }
        if size not in allowed_sizes[kind]:
            raise SourceSecurityError("TMDB artwork size is not allowed for kind")
        return

    if locator.provider == "tvdb":
        if kind not in {"poster", "backdrop", "logo"}:
            raise SourceSecurityError("TVDB artwork kind is not allowlisted")
        if not re.fullmatch(
            r"/(?:banners|artworks|series|movies)/[A-Za-z0-9_./@+-]+\.(?:jpe?g|png|webp)",
            decoded,
            flags=re.IGNORECASE,
        ):
            raise SourceSecurityError("TVDB artwork path is not allowlisted")
        return

    if locator.provider == "metahub":
        if kind != "logo" or not re.fullmatch(
            r"/logo/(?:small|medium|large)/tt[0-9]{7,10}/img",
            decoded,
        ):
            raise SourceSecurityError("Metahub artwork path is not allowlisted")
        return
    raise SourceSecurityError("artwork provider is not allowlisted")


def resolve_public_addresses(
    host: str,
    *,
    resolver: Callable[[str, int], Iterable[tuple]] = socket.getaddrinfo,
) -> tuple[str, ...]:
    try:
        answers = resolver(host, 443)
    except OSError as exc:
        raise SourceSecurityError("artwork DNS resolution failed") from exc
    addresses: set[str] = set()
    for answer in answers:
        try:
            raw = answer[4][0]
            address = ipaddress.ip_address(raw)
        except (IndexError, TypeError, ValueError) as exc:
            raise SourceSecurityError("artwork DNS returned an invalid address") from exc
        if not address.is_global:
            raise SourceSecurityError("every artwork DNS answer must be public")
        addresses.add(address.compressed)
    if not addresses:
        raise SourceSecurityError("artwork DNS returned no addresses")
    return tuple(sorted(addresses, key=lambda item: (ipaddress.ip_address(item).version, item)))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, connect_ip: str, *, timeout: float) -> None:
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._connect_ip = connect_ip

    def connect(self) -> None:
        raw = socket.create_connection((self._connect_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _request_pinned(
    url: str,
    connect_ip: str,
    host: str,
    *,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> PinnedHTTPResponse:
    parsed = urlsplit(url)
    connection = _PinnedHTTPSConnection(host, connect_ip, timeout=15.0)
    try:
        connection.request(
            "GET",
            parsed.path,
            headers={
                "Host": host,
                "Accept": "image/*,image/svg+xml",
                "Accept-Encoding": "identity",
                "User-Agent": "PostersPlus-BingeCat-v2/1",
                "Connection": "close",
            },
        )
        # Capture the verified peer while ``http.client`` still owns the
        # socket.  A response with ``Connection: close`` clears
        # ``connection.sock`` inside ``getresponse()`` before callers can
        # inspect it, even though the request used the pinned address.
        peer_ip = str(connection.sock.getpeername()[0]) if connection.sock else ""
        response = connection.getresponse()
        headers = {key.lower(): value.strip() for key, value in response.getheaders()}
        content_length = headers.get("content-length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise SourceResourceError("invalid source Content-Length") from exc
            if declared < 0 or declared > max_bytes:
                raise SourceResourceError("source download is too large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SourceResourceError("source download is too large")
            chunks.append(chunk)
        return PinnedHTTPResponse(response.status, headers, tuple(chunks), peer_ip)
    except SourceArtError:
        raise
    except Exception as exc:
        raise SourceArtError("source download failed") from exc
    finally:
        connection.close()


def download_source(
    locator: ArtworkLocator,
    kind: Literal["poster", "backdrop", "logo"],
    *,
    temp_dir: str | os.PathLike[str],
    resolver: Callable[[str, int], Iterable[tuple]] = socket.getaddrinfo,
    requester: Callable[..., PinnedHTTPResponse] = _request_pinned,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> DownloadedSource:
    current = locator
    for redirects in range(MAX_REDIRECTS + 1):
        validate_locator_for_kind(current, kind)
        host = urlsplit(current.url).hostname or ""
        addresses = resolve_public_addresses(host, resolver=resolver)
        pinned_ip = addresses[0]
        response = requester(
            current.url,
            pinned_ip,
            host,
            max_bytes=max_bytes,
        )
        try:
            peer = ipaddress.ip_address(response.peer_ip).compressed
        except ValueError as exc:
            raise SourceSecurityError("source peer address is invalid") from exc
        if peer != ipaddress.ip_address(pinned_ip).compressed:
            raise SourceSecurityError("source peer did not match pinned DNS address")

        if response.status_code in {301, 302, 303, 307, 308}:
            if redirects >= MAX_REDIRECTS:
                raise SourceSecurityError("source redirect limit exceeded")
            location = response.headers.get("location")
            if not location:
                raise SourceSecurityError("source redirect has no location")
            try:
                redirected = ArtworkLocator(
                    provider=current.provider,
                    url=urljoin(current.url, location),
                )
            except ValueError as exc:
                raise SourceSecurityError("source redirect left the provider allowlist") from exc
            if redirected.provider != locator.provider:
                raise SourceSecurityError("source redirect changed provider")
            current = redirected
            continue
        if response.status_code != 200:
            raise SourceArtError(f"source provider returned HTTP {response.status_code}")

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise SourceResourceError("invalid source Content-Length") from exc
            if declared < 0 or declared > max_bytes:
                raise SourceResourceError("source download is too large")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        os.makedirs(temp_dir, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="raw-", dir=temp_dir)
        total = 0
        try:
            with os.fdopen(fd, "wb") as output:
                for chunk in response.chunks:
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceResourceError("source download is too large")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            return DownloadedSource(Path(raw_path), content_type, current)
        except Exception:
            try:
                os.unlink(raw_path)
            except OSError:
                pass
            raise
    raise SourceSecurityError("source redirect limit exceeded")


def _detect_mime(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    stripped = raw.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        return "image/svg+xml"
    raise SourceArtError("unsupported source MIME or magic")


def _safe_svg_to_image(raw: bytes) -> Image.Image:
    lowered = raw.lower()
    forbidden = (
        b"<!doctype", b"<!entity", b"<script", b"<foreignobject", b"<image",
        b"<?xml-stylesheet", b"@import", b"href=\"http", b"href='http",
        b"href=\"//", b"href='//", b"href=\"/", b"href='/",
        b"href=\"..", b"href='..", b"url(http", b"url(//",
        b"@keyframes", b"animation:", b"animation-name", b"transition:",
    )
    if any(token in lowered for token in forbidden):
        raise SourceSecurityError("unsafe SVG content rejected")
    for match in re.finditer(rb"(?:xlink:)?href\s*=\s*(['\"])(.*?)\1", lowered):
        if not match.group(2).startswith(b"#"):
            raise SourceSecurityError("unsafe external SVG reference rejected")
    if b"url(" in lowered and re.sub(rb"url\(\s*#[^)]+\)", b"", lowered).find(b"url(") >= 0:
        raise SourceSecurityError("unsafe external SVG URL rejected")
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(raw)
        node_count = 0
        attribute_count = 0
        path_bytes = 0
        forbidden_elements = {
            "animate",
            "animatemotion",
            "animatetransform",
            "discard",
            "filter",
            "foreignobject",
            "image",
            "script",
            "set",
            "style",
        }
        for element in root.iter():
            node_count += 1
            attribute_count += len(element.attrib)
            local_name = str(element.tag).rsplit("}", 1)[-1].lower()
            if local_name in forbidden_elements or local_name.startswith("fe"):
                raise SourceSecurityError("unsafe SVG element rejected")
            if node_count > 4096 or attribute_count > 8192:
                raise SourceResourceError("SVG complexity exceeds limit")
            for name, value in element.attrib.items():
                if len(value) > 200_000:
                    raise SourceResourceError("SVG attribute exceeds limit")
                if str(name).rsplit("}", 1)[-1].lower() == "d":
                    path_bytes += len(value)
                    if path_bytes > 1_000_000:
                        raise SourceResourceError("SVG path complexity exceeds limit")
        dimension = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(?:px)?$")
        width = dimension.fullmatch(root.attrib.get("width", "").strip())
        height = dimension.fullmatch(root.attrib.get("height", "").strip())
        if width and height:
            w, h = int(float(width.group(1))), int(float(height.group(1)))
        else:
            viewbox = root.attrib.get("viewBox") or root.attrib.get("viewbox") or ""
            pieces = re.split(r"[\s,]+", viewbox.strip())
            if len(pieces) != 4:
                raise SourceResourceError("SVG dimensions are undefined")
            try:
                w, h = int(float(pieces[2])), int(float(pieces[3]))
            except ValueError as exc:
                raise SourceResourceError("SVG dimensions are invalid") from exc
        if w <= 0 or h <= 0 or w > MAX_IMAGE_AXIS or h > MAX_IMAGE_AXIS or w * h > MAX_IMAGE_PIXELS:
            raise SourceResourceError("source image dimensions exceed limit")
        import cairosvg

        png = cairosvg.svg2png(bytestring=raw, output_width=w, output_height=h)
        return Image.open(io.BytesIO(png))
    except SourceArtError:
        raise
    except Exception as exc:
        raise SourceArtError("SVG rasterization failed") from exc


def _load_source_image(raw: bytes, declared_mime: str | None) -> Image.Image:
    detected = _detect_mime(raw)
    declared = (declared_mime or "").split(";", 1)[0].strip().lower()
    if declared and declared != detected:
        raise SourceArtError("declared MIME does not match source magic")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image = _safe_svg_to_image(raw) if detected == "image/svg+xml" else Image.open(io.BytesIO(raw))
    if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
        raise SourceResourceError("animated source images are not allowed")
    width, height = image.size
    if (
        width <= 0
        or height <= 0
        or width > MAX_IMAGE_AXIS
        or height > MAX_IMAGE_AXIS
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise SourceResourceError("source image dimensions exceed limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image.load()
    return ImageOps.exif_transpose(image)


def _hash_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as payload:
        for chunk in iter(lambda: payload.read(128 * 1024), b""):
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise SourceResourceError("cached source derivative is too large")
            hasher.update(chunk)
    return size, hasher.hexdigest()


def _read_bounded(raw: BinaryIO) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_SOURCE_BYTES:
        chunk = raw.read(min(1024 * 1024, MAX_SOURCE_BYTES + 1 - total))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise SourceArtError("source stream must return bytes")
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_SOURCE_BYTES:
        raise SourceResourceError("source download is too large")
    return b"".join(chunks)


class SourceArtStore:
    def __init__(
        self,
        root: str | os.PathLike[str],
        ledger_path: str | os.PathLike[str],
        *,
        staging_root: str | os.PathLike[str] | None = None,
        require_staging_root: bool = False,
        reclaim_staging: bool = False,
    ) -> None:
        self.root = Path(root)
        self.ledger_path = Path(ledger_path)
        self.staging_root = Path(staging_root) if staging_root is not None else self.root / "tmp"
        self.require_staging_root = bool(require_staging_root)
        self.reclaim_staging = bool(reclaim_staging)
        try:
            root_info = self.root.lstat()
        except FileNotFoundError:
            self.root.mkdir(parents=True, exist_ok=True)
            root_info = self.root.lstat()
        except OSError as exc:
            raise SourceResourceError("source artifact root is unavailable") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise SourceResourceError("source artifact root is unsafe")
        self.download_root = self.root / "tmp"
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_config(cls):
        import config

        if config.POSTERSPLUS_SOURCE_STORE_MODE == "remote":
            from remote_source_store import RemoteSourceArtStore

            return RemoteSourceArtStore.from_config()
        if config.POSTERSPLUS_SOURCE_REQUIRE_MOUNTS:
            for path in (
                config.SOURCE_ART_CACHE_DIR,
                config.POSTERSPLUS_SOURCE_INCOMING_DIR,
            ):
                validate_source_mount(
                    path,
                    expectation="owner-local",
                    mountinfo_path=config.POSTERSPLUS_SOURCE_MOUNTINFO_PATH,
                )
        account_incoming = bool(config.POSTERSPLUS_SOURCE_ACCOUNT_INCOMING)
        incoming_root = config.POSTERSPLUS_SOURCE_INCOMING_DIR
        if account_incoming and not incoming_root:
            raise SourceResourceError(
                "source incoming directory is required for incoming accounting"
            )
        return cls(
            config.SOURCE_ART_CACHE_DIR,
            config.SOURCE_ART_LEDGER_PATH,
            staging_root=incoming_root if account_incoming else None,
            require_staging_root=account_incoming,
            reclaim_staging=(config.POSTERSPLUS_SOURCE_REGISTRY_MODE == "owner"),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ledger_path, timeout=15.0)
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_art_ledger (
                    source_art_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    mime TEXT NOT NULL,
                    recipe_version INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    locator_json TEXT,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    pinned INTEGER NOT NULL,
                    reconstructable INTEGER NOT NULL,
                    UNIQUE(kind, recipe_version, sha256)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_source_art_last_used ON source_art_ledger(last_used_at)"
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_art_capacity_reservations (
                    token TEXT PRIMARY KEY,
                    byte_size INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            reservation_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(source_art_capacity_reservations)"
                ).fetchall()
            }
            if "created_at" not in reservation_columns:
                # Existing owner ledgers predate the active-file protection
                # timestamp. Default zero is deliberately conservative: an
                # old active token protects every staged file until expiry.
                connection.execute(
                    "ALTER TABLE source_art_capacity_reservations "
                    "ADD COLUMN created_at REAL NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_source_art_reservation_expiry "
                "ON source_art_capacity_reservations(expires_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_art_verification_ledger (
                    signature TEXT PRIMARY KEY,
                    key_json TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    result TEXT NOT NULL,
                    verified_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_source_art_verification_source "
                "ON source_art_verification_ledger(kind, source_sha256)"
            )

    def _bounded_temp_bytes(
        self,
        *,
        limit: int = 256,
        exclude_path: str | os.PathLike[str] | None = None,
    ) -> int:
        total = 0
        visited = 0
        excluded = Path(exclude_path).absolute() if exclude_path is not None else None
        # Owner mode has two physical staging pools: the Oracle-facing
        # incoming NFS directory and the legacy local source_art/tmp pool.
        # The latter contains active downloads and crash orphans from old
        # writers, so omitting it would make the owner's cap optimistic.
        scan_roots: list[tuple[Path, bool]] = [(self.staging_root, self.require_staging_root)]
        legacy_temp_root = self.root / "tmp"
        if legacy_temp_root.absolute() != self.staging_root.absolute():
            scan_roots.append((legacy_temp_root, False))

        for temp_root, strict in scan_roots:
            if strict:
                try:
                    root_info = temp_root.lstat()
                except FileNotFoundError as exc:
                    raise SourceResourceError("source staging mount unavailable") from exc
                except OSError as exc:
                    raise SourceResourceError("source staging mount unavailable") from exc
                if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
                    raise SourceResourceError("source staging mount unavailable")
            try:
                entries = os.scandir(temp_root)
            except FileNotFoundError:
                if strict:
                    raise SourceResourceError("source staging mount unavailable")
                continue
            except OSError as exc:
                raise SourceResourceError("source cache capacity unknown") from exc
            with entries:
                for entry in entries:
                    visited += 1
                    if visited > limit:
                        raise SourceResourceError("source cache capacity unknown")
                    try:
                        if entry.is_symlink():
                            if strict:
                                raise SourceResourceError(
                                    "source staging contains a symlink"
                                )
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            if strict:
                                # The owner protocol only permits direct-child
                                # staging files. Skipping a directory (or any
                                # other non-regular entry) would make its
                                # bytes invisible to the hard-cap calculation.
                                raise SourceResourceError(
                                    "source staging contains a non-regular entry"
                                )
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if strict and int(info.st_nlink) != 1:
                            raise SourceResourceError(
                                "source staging contains a hard link"
                            )
                        if excluded is not None and Path(entry.path).absolute() == excluded:
                            continue
                        total += max(0, int(info.st_size))
                    except OSError as exc:
                        raise SourceResourceError("source cache capacity unknown") from exc
        return total

    def _reclaim_expired_staging_locked(
        self,
        connection: sqlite3.Connection,
        now: float,
    ) -> None:
        """Reclaim only stale owner protocol files while the ledger is locked.

        The owner is the sole writer allowed to remove incoming files. Active
        reservations protect files created after their reservation window; the
        clock-skew margin covers NFS timestamp granularity. Files with unknown
        names are retained, while symlinks, directories, hard links, and scan
        overflow fail closed. The method intentionally does not recurse.
        """

        if not self.reclaim_staging:
            return
        import config

        try:
            active_rows = connection.execute(
                "SELECT expires_at, created_at "
                "FROM source_art_capacity_reservations WHERE expires_at>?",
                (now,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise SourceResourceError("source reservation state is unavailable") from exc

        active_floor: float | None = None
        for expires_at, created_at in active_rows:
            if isinstance(expires_at, bool) or isinstance(created_at, bool):
                raise SourceResourceError("source reservation state is invalid")
            try:
                expiry = float(expires_at)
                created = float(created_at)
            except (TypeError, ValueError, OverflowError) as exc:
                raise SourceResourceError("source reservation state is invalid") from exc
            if not (expiry > now) or not math.isfinite(created):
                raise SourceResourceError("source reservation state is invalid")
            if created <= 0:
                active_floor = 0.0
                break
            active_floor = created if active_floor is None else min(active_floor, created)

        try:
            stale_age = max(0, int(config.SOURCE_CACHE_RAW_MAX_AGE_SECONDS))
        except (AttributeError, TypeError, ValueError) as exc:
            raise SourceResourceError("source staging age policy is invalid") from exc
        stale_before = now - stale_age
        protected_before = (
            None
            if active_floor is None
            else active_floor - STAGING_RESERVATION_CLOCK_SKEW_SECONDS
        )

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            staging_fd = os.open(self.staging_root, flags)
        except OSError as exc:
            raise SourceResourceError("source staging mount unavailable") from exc

        candidates: list[tuple[str, int, int]] = []
        overflow = False
        deleted = 0
        try:
            try:
                entries = os.scandir(staging_fd)
            except (OSError, TypeError) as exc:
                raise SourceResourceError("source staging scan unavailable") from exc
            with entries:
                for visited, entry in enumerate(entries, start=1):
                    if visited > STAGING_RECLAIM_SCAN_LIMIT:
                        overflow = True
                        break
                    try:
                        if entry.is_symlink():
                            raise SourceResourceError(
                                "source staging contains a symlink"
                            )
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            raise SourceResourceError(
                                "source staging contains a non-regular entry"
                            )
                        if int(info.st_nlink) != 1:
                            raise SourceResourceError(
                                "source staging contains a hard link"
                            )
                        if not _RECLAIMABLE_STAGED_FILENAME_RE.fullmatch(entry.name):
                            continue
                        if float(info.st_mtime) >= stale_before:
                            continue
                        if (
                            protected_before is not None
                            and float(info.st_mtime) >= protected_before
                        ):
                            continue
                        candidates.append(
                            (entry.name, int(info.st_dev), int(info.st_ino))
                        )
                    except OSError as exc:
                        raise SourceResourceError("source staging scan unavailable") from exc

            # Recheck every candidate through the no-follow directory fd before
            # unlinking. A register/download race therefore preserves a changed
            # file instead of deleting it under a stale directory entry.
            for name, expected_dev, expected_ino in candidates:
                try:
                    info = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise SourceResourceError("source staging recheck unavailable") from exc
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISREG(info.st_mode)
                    or int(info.st_nlink) != 1
                    or int(info.st_dev) != expected_dev
                    or int(info.st_ino) != expected_ino
                ):
                    raise SourceResourceError("source staging changed during cleanup")
                try:
                    os.unlink(name, dir_fd=staging_fd)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise SourceResourceError("source staging cleanup failed") from exc
                deleted += 1
            if deleted:
                try:
                    os.fsync(staging_fd)
                except OSError:
                    pass
        finally:
            os.close(staging_fd)

        if overflow:
            raise SourceResourceError("source staging cleanup scan incomplete")

    def _artifact_path(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str,
        sha256: str,
        mime: str | None = None,
        missing_is_none: bool = False,
    ) -> Path | None:
        """Validate a ledger path without following symlinks or traversal."""

        if (
            not isinstance(kind, str)
            or kind not in RECIPE_VERSIONS
            or not isinstance(sha256, str)
            or not _SHA256_RE.fullmatch(sha256)
        ):
            raise SourceResourceError("source artifact metadata is invalid")
        if mime is not None and (
            not isinstance(mime, str) or mime not in {"image/jpeg", "image/png"}
        ):
            raise SourceResourceError("source artifact MIME is invalid")
        root = self.root.absolute()
        candidate = Path(path).absolute()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise SourceResourceError("source artifact escaped source root") from exc
        value = relative.as_posix()
        if not _ARTIFACT_RELPATH_RE.fullmatch(value):
            raise SourceResourceError("source artifact path is invalid")
        suffix = ".jpg" if mime == "image/jpeg" else ".png" if mime == "image/png" else None
        expected = f"{kind}/{sha256[:2]}/{sha256}{suffix or ''}"
        if value.split("/", 1)[0] != kind or value.split("/")[-1].split(".")[0] != sha256:
            raise SourceResourceError("source artifact path does not match metadata")
        if suffix is not None and value != expected:
            raise SourceResourceError("source artifact extension does not match MIME")
        for parent in (root, root / kind, root / kind / sha256[:2]):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                if missing_is_none:
                    return None
                raise SourceResourceError("source artifact path is unavailable")
            except OSError as exc:
                raise SourceResourceError("source artifact path is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SourceResourceError("source artifact path is unsafe")
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if missing_is_none:
                return None
            raise SourceResourceError("source artifact is unavailable")
        except OSError as exc:
            raise SourceResourceError("source artifact is unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SourceDigestMismatch("source artifact is not a private regular file")
        if int(info.st_nlink) != 1:
            raise SourceDigestMismatch("source artifact has an unexpected hard link")
        return candidate

    def _ensure_artifact_dirs(self, kind: str, sha256: str) -> Path:
        if (
            not isinstance(kind, str)
            or kind not in RECIPE_VERSIONS
            or not isinstance(sha256, str)
            or not _SHA256_RE.fullmatch(sha256)
        ):
            raise SourceResourceError("source artifact metadata is invalid")
        try:
            root_info = self.root.lstat()
        except OSError as exc:
            raise SourceResourceError("source artifact root is unavailable") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise SourceResourceError("source artifact root is unsafe")
        current = self.root
        for component in (kind, sha256[:2]):
            current = current / component
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                    info = current.lstat()
                except OSError as exc:
                    raise SourceResourceError("source artifact directory is unavailable") from exc
            except OSError as exc:
                raise SourceResourceError("source artifact directory is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise SourceResourceError("source artifact directory is unsafe")
        return current

    def reserve_capacity(
        self,
        required_bytes: int,
        *,
        ttl_seconds: int = CAPACITY_RESERVATION_SECONDS,
    ) -> str:
        """Atomically reserve bounded staging bytes across worker processes."""

        import config

        if isinstance(required_bytes, bool) or not isinstance(required_bytes, int):
            raise SourceResourceError("invalid source cache reservation")
        required = required_bytes
        if required <= 0 or required > MAX_SOURCE_BYTES:
            raise SourceResourceError("invalid source cache reservation")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= 900
        ):
            raise SourceResourceError("invalid source cache reservation TTL")
        token = secrets.token_urlsafe(24)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            if self.reclaim_staging:
                self._reclaim_expired_staging_locked(connection, now)
            connection.execute(
                "DELETE FROM source_art_capacity_reservations WHERE expires_at<=?",
                (now,),
            )
            temp_bytes = self._bounded_temp_bytes(
                limit=(STAGING_RECLAIM_SCAN_LIMIT if self.reclaim_staging else 256)
            )
            current = max(
                0,
                int(
                    connection.execute(
                        "SELECT COALESCE(SUM(byte_size), 0) FROM source_art_ledger"
                    ).fetchone()[0]
                    or 0
                ),
            )
            reserved = max(
                0,
                int(
                    connection.execute(
                        "SELECT COALESCE(SUM(byte_size), 0) "
                        "FROM source_art_capacity_reservations"
                    ).fetchone()[0]
                    or 0
                ),
            )
            if (
                current + reserved + temp_bytes + required
                > config.SOURCE_CACHE_MAX_BYTES
            ):
                connection.rollback()
                raise SourceResourceError("source cache hard limit reached")
            connection.execute(
                "INSERT INTO source_art_capacity_reservations "
                "(token, byte_size, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token, required, now + ttl_seconds, now),
            )
            connection.commit()
        return token

    def release_capacity(self, token: str) -> None:
        if not isinstance(token, str) or not token or len(token) > 128:
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM source_art_capacity_reservations WHERE token=?", (token,)
            )
            connection.commit()

    @staticmethod
    def _validate_reservation_token(token: str) -> str:
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= MAX_RESERVATION_TOKEN_LENGTH
            or not re.fullmatch(r"[A-Za-z0-9_-]+", token)
        ):
            raise SourceResourceError("invalid source cache reservation")
        return token

    def get(self, sha256: str, kind: str, recipe_version: int, *, now: datetime | None = None) -> SourceDerivative | None:
        used = _utc(now)
        with self._connect() as connection:
            row = connection.execute(
                """SELECT source_art_id, kind, sha256, byte_size, mime, recipe_version,
                          path, width, height, locator_json, created_at, last_used_at,
                          pinned, reconstructable
                   FROM source_art_ledger
                   WHERE sha256=? AND kind=? AND recipe_version=?""",
                (sha256, kind, recipe_version),
            ).fetchone()
            if not row:
                return None
            path = self._artifact_path(
                row[6],
                kind=row[1],
                sha256=row[2],
                mime=row[4],
                missing_is_none=True,
            )
            if path is None:
                return None
            size, actual_digest = _hash_file(path)
            if size != row[3] or actual_digest != row[2]:
                raise SourceDigestMismatch("cached source derivative failed digest verification")
            connection.execute(
                "UPDATE source_art_ledger SET last_used_at=MAX(last_used_at, ?) WHERE source_art_id=?",
                (used.timestamp(), row[0]),
            )
            connection.commit()
        effective_last_used = max(float(row[11]), used.timestamp())
        locator = ArtworkLocator.model_validate_json(row[9]) if row[9] else None
        return SourceDerivative(
            source_art_id=row[0], kind=row[1], sha256=row[2], byte_size=row[3],
            mime=row[4], recipe_version=row[5], path=row[6], width=row[7], height=row[8],
            locator=locator,
            created_at=datetime.fromtimestamp(row[10], timezone.utc),
            last_used_at=datetime.fromtimestamp(effective_last_used, timezone.utc),
            pinned=bool(row[12]), reconstructable=bool(row[13]),
        )

    def _verified_source_row(
        self,
        connection: sqlite3.Connection,
        key: SourceVerificationKey,
    ) -> tuple[str, int, str] | None:
        row = connection.execute(
            "SELECT path, byte_size, sha256, mime FROM source_art_ledger "
            "WHERE kind=? AND sha256=? ORDER BY recipe_version DESC LIMIT 1",
            (key.kind, key.source_sha256),
        ).fetchone()
        if row is None:
            return None
        path = self._artifact_path(
            row[0],
            kind=key.kind,
            sha256=key.source_sha256,
            mime=row[3],
        )
        assert path is not None
        size, digest = _hash_file(path)
        if size != int(row[1]) or digest != row[2] or digest != key.source_sha256:
            raise SourceDigestMismatch("source derivative failed digest verification")
        return str(row[0]), int(row[1]), str(row[2])

    @staticmethod
    def _verification_memo_from_row(row: tuple) -> SourceVerificationMemo:
        try:
            key_payload = json.loads(str(row[1]))
            key = _verification_key(key_payload)
            result = _verification_result(row[4])
            verified_at = datetime.fromtimestamp(float(row[5]), timezone.utc)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SourceArtError("source verification memo is corrupt") from exc
        if verification_key_signature(key) != row[0] or key.source_sha256 != row[3]:
            raise SourceArtError("source verification memo signature mismatch")
        return SourceVerificationMemo(
            signature=str(row[0]),
            key=key,
            result=result,
            verified_at=verified_at,
            source_sha256=str(row[3]),
        )

    def lookup_verification(
        self,
        key: SourceVerificationKey | Mapping[str, Any],
    ) -> SourceVerificationMemo | None:
        normalized = _verification_key(key)
        signature = verification_key_signature(normalized)
        with self._connect() as connection:
            source = self._verified_source_row(connection, normalized)
            if source is None:
                return None
            row = connection.execute(
                "SELECT signature, key_json, kind, source_sha256, result, verified_at "
                "FROM source_art_verification_ledger WHERE signature=?",
                (signature,),
            ).fetchone()
        if row is None:
            return None
        return self._verification_memo_from_row(row)

    def register_verification(
        self,
        key: SourceVerificationKey | Mapping[str, Any],
        *,
        result: Literal["textless", "text", "unknown"],
        verified_at: datetime | None = None,
    ) -> SourceVerificationMemo:
        normalized = _verification_key(key)
        result = _verification_result(result)
        observed = _utc(verified_at)
        signature = verification_key_signature(normalized)
        key_json = json.dumps(
            normalized.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._verified_source_row(connection, normalized) is None:
                connection.rollback()
                raise SourceResourceError("source derivative is not registered")
            existing = connection.execute(
                "SELECT signature, key_json, kind, source_sha256, result, verified_at "
                "FROM source_art_verification_ledger WHERE signature=?",
                (signature,),
            ).fetchone()
            if existing is not None:
                memo = self._verification_memo_from_row(existing)
                if memo.key != normalized or memo.result != result:
                    connection.rollback()
                    raise SourceArtError("source verification conflict")
                connection.commit()
                return memo
            connection.execute(
                "INSERT INTO source_art_verification_ledger "
                "(signature, key_json, kind, source_sha256, result, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    signature,
                    key_json,
                    normalized.kind,
                    normalized.source_sha256,
                    result,
                    observed.timestamp(),
                ),
            )
            connection.commit()
        return SourceVerificationMemo(
            signature=signature,
            key=normalized,
            result=result,
            verified_at=observed,
            source_sha256=normalized.source_sha256,
        )

    def install(
        self,
        *,
        kind: Literal["poster", "backdrop", "logo"],
        recipe_version: int,
        payload: bytes,
        mime: Literal["image/jpeg", "image/png"],
        width: int,
        height: int,
        locator: ArtworkLocator | None,
        now: datetime | None,
        pinned: bool,
        reconstructable: bool,
        reservation_token: str | None = None,
        staged_path: str | os.PathLike[str] | None = None,
    ) -> SourceDerivative:
        used = _utc(now)
        if not isinstance(payload, bytes) or not payload or len(payload) > MAX_SOURCE_BYTES:
            raise SourceResourceError("invalid source derivative payload")
        if (
            not isinstance(kind, str)
            or kind not in RECIPE_VERSIONS
            or isinstance(recipe_version, bool)
            or not isinstance(recipe_version, int)
            or recipe_version != RECIPE_VERSIONS[kind]
        ):
            raise SourceArtError(f"unsupported {kind} recipe version")
        if not isinstance(mime, str) or mime not in {"image/jpeg", "image/png"}:
            raise SourceArtError("unsupported normalized source MIME")
        if kind in {"poster", "backdrop"} and mime != "image/jpeg":
            raise SourceArtError("poster and backdrop derivatives must be JPEG")
        if kind == "logo" and mime != "image/png":
            raise SourceArtError("logo derivatives must be PNG")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or isinstance(height, bool)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise SourceArtError("invalid source derivative dimensions")
        digest = hashlib.sha256(payload).hexdigest()
        source_art_id = f"{kind}-r{recipe_version}-{digest}"
        suffix = ".jpg" if mime == "image/jpeg" else ".png"
        locator_json = locator.model_dump_json() if locator is not None else None
        directory = self._ensure_artifact_dirs(kind, digest)
        destination = directory / f"{digest}{suffix}"
        temp_path: str | None = None
        linked_new = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if reservation_token is not None:
                    reservation_token = self._validate_reservation_token(reservation_token)
                    reservation = connection.execute(
                        "SELECT byte_size, expires_at FROM source_art_capacity_reservations "
                        "WHERE token=?",
                        (reservation_token,),
                    ).fetchone()
                    if reservation is None or float(reservation[1]) <= time.time():
                        raise SourceResourceError("source reservation expired")
                    if int(reservation[0]) < len(payload):
                        raise SourceResourceError("source reservation is too small")
                existing = connection.execute(
                    "SELECT byte_size FROM source_art_ledger WHERE source_art_id=?",
                    (source_art_id,),
                ).fetchone()
                if existing is None:
                    import config

                    temp_bytes = self._bounded_temp_bytes(exclude_path=staged_path)
                    connection.execute(
                        "DELETE FROM source_art_capacity_reservations WHERE expires_at<=?",
                        (time.time(),),
                    )
                    current = max(
                        0,
                        int(
                            connection.execute(
                                "SELECT COALESCE(SUM(byte_size), 0) FROM source_art_ledger"
                            ).fetchone()[0]
                            or 0
                        ),
                    )
                    other_reserved = max(
                        0,
                        int(
                            connection.execute(
                                "SELECT COALESCE(SUM(byte_size), 0) "
                                "FROM source_art_capacity_reservations WHERE token<>?",
                                (reservation_token or "",),
                            ).fetchone()[0]
                            or 0
                        ),
                    )
                    if (
                        current + other_reserved + temp_bytes + len(payload)
                        > config.SOURCE_CACHE_MAX_BYTES
                    ):
                        raise SourceResourceError("source cache hard limit reached")

                fd, temp_path = tempfile.mkstemp(prefix="install-", dir=directory)
                with os.fdopen(fd, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temp_path, destination)
                    linked_new = True
                except FileExistsError:
                    try:
                        destination_info = destination.lstat()
                    except OSError as exc:
                        raise SourceDigestMismatch(
                            "content-addressed destination is unavailable"
                        ) from exc
                    if (
                        stat.S_ISLNK(destination_info.st_mode)
                        or not stat.S_ISREG(destination_info.st_mode)
                        or int(destination_info.st_nlink) != 1
                    ):
                        raise SourceDigestMismatch(
                            "content-addressed destination is not a private regular file"
                        )
                    existing_size, existing_digest = _hash_file(destination)
                    if existing_size != len(payload) or existing_digest != digest:
                        raise SourceDigestMismatch("content-addressed destination mismatch")

                connection.execute(
                    """
                    INSERT OR IGNORE INTO source_art_ledger
                        (source_art_id, kind, sha256, byte_size, mime, recipe_version,
                         path, width, height, locator_json, created_at, last_used_at,
                         pinned, reconstructable)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_art_id, kind, digest, len(payload), mime, recipe_version,
                        str(destination), width, height, locator_json,
                        used.timestamp(), used.timestamp(), int(pinned), int(reconstructable),
                    ),
                )
                connection.execute(
                    """UPDATE source_art_ledger
                       SET last_used_at=MAX(last_used_at, ?), locator_json=COALESCE(locator_json, ?),
                           pinned=MAX(pinned, ?), reconstructable=MAX(reconstructable, ?)
                       WHERE source_art_id=?""",
                    (used.timestamp(), locator_json, int(pinned), int(reconstructable), source_art_id),
                )
                if reservation_token:
                    connection.execute(
                        "DELETE FROM source_art_capacity_reservations WHERE token=?",
                        (reservation_token,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                if linked_new:
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                raise
            finally:
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
        installed = self.get(digest, kind, recipe_version, now=used)
        if installed is None:  # Defensive: the atomically installed file must be readable.
            raise SourceArtError("source derivative installation did not converge")
        return installed


def _normalize_payload(
    kind: Literal["poster", "backdrop", "logo"],
    raw: BinaryIO,
    recipe_version: int,
    *,
    declared_mime: str | None = None,
) -> tuple[bytes, Literal["image/jpeg", "image/png"], int, int]:
    expected_recipe = RECIPE_VERSIONS[kind]
    if recipe_version != expected_recipe:
        raise SourceArtError(f"unsupported {kind} recipe version")
    payload = _read_bounded(raw)
    image = _load_source_image(payload, declared_mime)

    if kind == "poster":
        from tmdb import normalise_poster

        image = normalise_poster(image.convert("RGBA")).convert("RGB")
        out_mime: Literal["image/jpeg", "image/png"] = "image/jpeg"
    elif kind == "backdrop":
        from tmdb import _crop_and_normalise_backdrop

        image = _crop_and_normalise_backdrop(image.convert("RGBA"), "v2", False).convert("RGB")
        out_mime = "image/jpeg"
    else:
        image = image.convert("RGBA")
        alpha = image.getchannel("A")
        bbox = alpha.getbbox()
        if bbox:
            image = image.crop(bbox)
        if image.width > 1000 or image.height > 400:
            scale = min(1000 / image.width, 400 / image.height)
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        out_mime = "image/png"

    encoded = io.BytesIO()
    if out_mime == "image/jpeg":
        image.save(
            encoded,
            format="JPEG",
            quality=92,
            optimize=False,
            progressive=False,
            subsampling=0,
        )
    else:
        image.save(encoded, format="PNG", optimize=False, compress_level=9)
    return encoded.getvalue(), out_mime, image.width, image.height


def normalize_and_store(
    kind: Literal["poster", "backdrop", "logo"],
    raw: BinaryIO,
    recipe_version: int,
    *,
    declared_mime: str | None = None,
    store: SourceArtStore | None = None,
    locator: ArtworkLocator | None = None,
    now: datetime | None = None,
    pinned: bool = False,
    reconstructable: bool | None = None,
) -> SourceDerivative:
    payload, out_mime, width, height = _normalize_payload(
        kind,
        raw,
        recipe_version,
        declared_mime=declared_mime,
    )
    active_store = store or SourceArtStore.from_config()
    return active_store.install(
        kind=kind,
        recipe_version=recipe_version,
        payload=payload,
        mime=out_mime,
        width=width,
        height=height,
        locator=locator,
        now=now,
        pinned=pinned,
        reconstructable=(locator is not None) if reconstructable is None else reconstructable,
    )


async def _default_downloader(
    locator: ArtworkLocator,
    kind: Literal["poster", "backdrop", "logo"],
    *,
    temp_dir: str | os.PathLike[str],
) -> DownloadedSource:
    return await asyncio.to_thread(
        download_source,
        locator,
        kind,
        temp_dir=temp_dir,
    )


async def fetch_derivative(
    locator: ArtworkLocator,
    *,
    kind: Literal["poster", "backdrop", "logo"],
    recipe_version: int,
    expected_sha256: str | None = None,
    store: SourceArtStore | None = None,
    now: datetime | None = None,
    downloader: Callable[..., object] = _default_downloader,
) -> SourceDerivative:
    active_store = store or await asyncio.to_thread(SourceArtStore.from_config)
    if expected_sha256:
        cached = await asyncio.to_thread(
            active_store.get,
            expected_sha256,
            kind,
            recipe_version,
            now=now,
        )
        if cached is not None:
            return cached
    # Reserve one bounded raw download before writing it.  Normalisation removes
    # the raw file before the final derivative is installed, so the source pool
    # cannot transiently cross its hard allocation.
    reservation_token = await asyncio.to_thread(
        active_store.reserve_capacity, MAX_SOURCE_BYTES
    )
    temp_dir = Path(getattr(active_store, "download_root", active_store.root / "tmp"))
    download_permit = _get_source_download_semaphore()
    permit_acquired = False
    try:
        await download_permit.acquire()
        permit_acquired = True
        downloaded = downloader(locator, kind, temp_dir=temp_dir)
        if hasattr(downloaded, "__await__"):
            downloaded = await downloaded
        if not isinstance(downloaded, DownloadedSource):
            raise TypeError("downloader must return DownloadedSource")

        def normalize_download() -> SourceDerivative:
            if not downloaded.content_type:
                raise SourceArtError("source MIME header is required")
            with downloaded.path.open("rb") as raw:
                payload, mime, width, height = _normalize_payload(
                    kind,
                    raw,
                    recipe_version,
                    declared_mime=downloaded.content_type,
                )
            try:
                downloaded.path.unlink()
            except OSError:
                pass
            digest = hashlib.sha256(payload).hexdigest()
            if expected_sha256 and digest != expected_sha256:
                raise SourceDigestMismatch("normalized source digest did not match snapshot")
            return active_store.install(
                kind=kind,
                recipe_version=recipe_version,
                payload=payload,
                mime=mime,
                width=width,
                height=height,
                locator=downloaded.locator,
                now=now,
                pinned=False,
                reconstructable=True,
                reservation_token=reservation_token,
            )

        derivative = await asyncio.to_thread(normalize_download)
        return derivative
    finally:
        if "downloaded" in locals() and isinstance(downloaded, DownloadedSource):
            try:
                downloaded.path.unlink()
            except OSError:
                pass
        if permit_acquired:
            download_permit.release()
        await asyncio.to_thread(active_store.release_capacity, reservation_token)


__all__ = [
    "CAPACITY_RESERVATION_SECONDS",
    "DownloadedSource",
    "MAX_SOURCE_BYTES",
    "PinnedHTTPResponse",
    "RECIPE_VERSIONS",
    "SourceArtError",
    "SourceArtStore",
    "SourceDerivative",
    "SourceDigestMismatch",
    "SourceMountInfo",
    "SourceResourceError",
    "SourceSecurityError",
    "SourceVerificationKey",
    "SourceVerificationMemo",
    "download_source",
    "fetch_derivative",
    "normalize_verification_key",
    "normalize_and_store",
    "resolve_public_addresses",
    "exact_source_mount",
    "read_source_mountinfo",
    "validate_source_mount",
    "verification_key_signature",
    "validate_locator_for_kind",
]
