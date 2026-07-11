"""SSRF-safe source retrieval and deterministic content-addressed derivatives."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import ipaddress
import os
import re
import secrets
import socket
import sqlite3
import ssl
import tempfile
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Literal, Mapping
from urllib.parse import unquote, urljoin, urlsplit

from PIL import Image, ImageOps

from integration_contract import ArtworkLocator


MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_AXIS = 8192
MAX_REDIRECTS = 2
CAPACITY_RESERVATION_SECONDS = 300
RECIPE_VERSIONS = {"poster": 1, "backdrop": 5, "logo": 1}


class SourceArtError(RuntimeError):
    pass


class SourceSecurityError(SourceArtError):
    pass


class SourceResourceError(SourceArtError):
    pass


class SourceDigestMismatch(SourceArtError):
    pass


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
        response = connection.getresponse()
        peer_ip = str(connection.sock.getpeername()[0]) if connection.sock else ""
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
    def __init__(self, root: str | os.PathLike[str], ledger_path: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.ledger_path = Path(ledger_path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_config(cls) -> "SourceArtStore":
        import config

        return cls(config.SOURCE_ART_CACHE_DIR, config.SOURCE_ART_LEDGER_PATH)

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
                    expires_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_source_art_reservation_expiry "
                "ON source_art_capacity_reservations(expires_at)"
            )

    def _bounded_temp_bytes(self, *, limit: int = 256) -> int:
        temp_root = self.root / "tmp"
        try:
            entries = os.scandir(temp_root)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise SourceResourceError("source cache capacity unknown") from exc
        total = 0
        visited = 0
        with entries:
            for entry in entries:
                visited += 1
                if visited > limit:
                    raise SourceResourceError("source cache capacity unknown")
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    total += max(0, int(entry.stat(follow_symlinks=False).st_size))
                except OSError as exc:
                    raise SourceResourceError("source cache capacity unknown") from exc
        return total

    def reserve_capacity(self, required_bytes: int) -> str:
        """Atomically reserve bounded staging bytes across worker processes."""

        import config

        required = max(0, int(required_bytes))
        if required <= 0 or required > MAX_SOURCE_BYTES:
            raise SourceResourceError("invalid source cache reservation")
        token = secrets.token_urlsafe(24)
        now = time.time()
        temp_bytes = self._bounded_temp_bytes()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM source_art_capacity_reservations WHERE expires_at<=?",
                (now,),
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
                "(token, byte_size, expires_at) VALUES (?, ?, ?)",
                (token, required, now + CAPACITY_RESERVATION_SECONDS),
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
            if not row or not Path(row[6]).is_file():
                return None
            path = Path(row[6])
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
    ) -> SourceDerivative:
        used = _utc(now)
        digest = hashlib.sha256(payload).hexdigest()
        source_art_id = f"{kind}-r{recipe_version}-{digest}"
        suffix = ".jpg" if mime == "image/jpeg" else ".png"
        locator_json = locator.model_dump_json() if locator is not None else None
        directory = self.root / kind / digest[:2]
        destination = directory / f"{digest}{suffix}"
        temp_path: str | None = None
        linked_new = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT byte_size FROM source_art_ledger WHERE source_art_id=?",
                    (source_art_id,),
                ).fetchone()
                if existing is None:
                    import config

                    temp_bytes = self._bounded_temp_bytes()
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

                directory.mkdir(parents=True, exist_ok=True)
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
                        destination.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise SourceDigestMismatch(
                            "content-addressed destination is unavailable"
                        ) from exc
                    if destination.is_symlink() or not destination.is_file():
                        raise SourceDigestMismatch(
                            "content-addressed destination is not a regular file"
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
    temp_dir = active_store.root / "tmp"
    try:
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
        await asyncio.to_thread(active_store.release_capacity, reservation_token)


__all__ = [
    "DownloadedSource",
    "PinnedHTTPResponse",
    "SourceArtError",
    "SourceArtStore",
    "SourceDerivative",
    "SourceDigestMismatch",
    "SourceResourceError",
    "SourceSecurityError",
    "download_source",
    "fetch_derivative",
    "normalize_and_store",
    "resolve_public_addresses",
    "validate_locator_for_kind",
]
