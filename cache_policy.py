"""Bounded cache accounting and eviction policy.

This module deliberately keeps policy separate from the hot legacy cache
helpers.  Reads and inserts do not walk the cache volume; the ledger and the
SQLite composite table provide cheap accounting, while this module performs a
bounded reconciliation during maintenance or an authenticated operator call.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config


SCHEMA = "postersplus_cache_usage"
VERSION = 1


@dataclass(frozen=True, slots=True)
class CachePoolUsage:
    name: str
    bytes: int
    hard_bytes: int
    high_watermark_bytes: int
    target_bytes: int
    details: dict[str, int] = field(default_factory=dict)

    @property
    def over_hard(self) -> bool:
        return self.bytes > self.hard_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes": int(self.bytes),
            "hard_bytes": int(self.hard_bytes),
            "high_watermark_bytes": int(self.high_watermark_bytes),
            "target_bytes": int(self.target_bytes),
            "over_hard": self.over_hard,
            **{key: int(value) for key, value in self.details.items()},
        }


@dataclass(frozen=True, slots=True)
class CacheUsage:
    generated_at: int
    pools: dict[str, CachePoolUsage]

    @property
    def total_bytes(self) -> int:
        return sum(pool.bytes for pool in self.pools.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "version": VERSION,
            "generated_at": self.generated_at,
            "total_bytes": self.total_bytes,
            "pools": {name: pool.to_dict() for name, pool in self.pools.items()},
        }


@dataclass(frozen=True, slots=True)
class PruneResult:
    started_at: int
    finished_at: int
    deleted_items: int
    deleted_bytes: int
    source_deleted_items: int = 0
    legacy_deleted_items: int = 0
    temp_deleted_items: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "version": VERSION,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "deleted_items": self.deleted_items,
            "deleted_bytes": self.deleted_bytes,
            "source_deleted_items": self.source_deleted_items,
            "legacy_deleted_items": self.legacy_deleted_items,
            "temp_deleted_items": self.temp_deleted_items,
        }


def _file_size(path: str | os.PathLike[str]) -> int:
    try:
        return max(0, os.stat(path, follow_symlinks=False).st_size)
    except OSError:
        return 0


def _bounded_files(root: str | os.PathLike[str], *, limit: int = 20_000):
    """Yield regular files below *root* without following links.

    This is only used by maintenance/accounting, never by a poster request or
    cache insert.  A hard limit prevents a damaged volume from turning an
    operator endpoint into an unbounded directory walk.
    """
    base = Path(root)
    seen = 0
    if not base.exists():
        return
    stack = [base]
    while stack and seen < limit:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if seen >= limit:
                break
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    seen += 1
                    yield Path(entry.path)
            except OSError:
                continue


def _source_usage() -> tuple[int, int, int]:
    """Return (derivative bytes, temporary bytes, missing ledger entries)."""
    derivative = 0
    missing = 0
    ledger = Path(config.SOURCE_ART_LEDGER_PATH)
    if ledger.exists():
        try:
            with sqlite3.connect(ledger, timeout=2.0) as db:
                rows = db.execute(
                    "SELECT path, byte_size FROM source_art_ledger"
                ).fetchall()
            for path, _recorded in rows:
                size = _file_size(path)
                if size:
                    derivative += size
                else:
                    missing += 1
        except sqlite3.Error:
            # A partially initialized ledger is safe to report as zero; the
            # next maintenance cycle will reconcile it.
            pass
    temp_root = Path(config.SOURCE_ART_CACHE_DIR) / "tmp"
    temp = sum(_file_size(path) for path in _bounded_files(temp_root))
    return derivative, temp, missing


def _legacy_usage() -> tuple[int, int, int, int]:
    """Return (composite blob bytes, sqlite bytes, wal bytes, temp bytes)."""
    composite = 0
    if os.path.exists(config.DB_PATH):
        try:
            with sqlite3.connect(config.DB_PATH, timeout=2.0) as db:
                composite = int(
                    db.execute(
                        "SELECT COALESCE(SUM(LENGTH(jpeg_bytes)), 0) "
                        "FROM final_poster_cache"
                    ).fetchone()[0]
                    or 0
                )
        except sqlite3.Error:
            pass
    try:
        sqlite_bytes = _file_size(config.DB_PATH)
    except OSError:
        sqlite_bytes = 0
    wal_bytes = _file_size(f"{config.DB_PATH}-wal") + _file_size(f"{config.DB_PATH}-shm")
    # SQLite temporary files and atomic-write leftovers are bounded and never
    # include the database/WAL themselves.
    db_dir = Path(config.DB_PATH).parent
    temp = 0
    for path in _bounded_files(db_dir):
        if path.name.startswith((".tmp-", "tmp-", "install-")):
            temp += _file_size(path)
    return composite, sqlite_bytes, wal_bytes, temp


def get_usage() -> CacheUsage:
    source_bytes, source_temp, missing = _source_usage()
    composite, sqlite_bytes, wal_bytes, legacy_temp = _legacy_usage()
    source_pool = CachePoolUsage(
        "source_derivatives",
        source_bytes + source_temp,
        config.SOURCE_CACHE_MAX_BYTES,
        config.SOURCE_CACHE_HIGH_WATERMARK_BYTES,
        config.SOURCE_CACHE_TARGET_BYTES,
        {
            "derivative_bytes": source_bytes,
            "temp_bytes": source_temp,
            "missing_ledger_entries": missing,
        },
    )
    legacy_pool = CachePoolUsage(
        "legacy",
        composite + sqlite_bytes + wal_bytes + legacy_temp,
        config.LEGACY_CACHE_MAX_BYTES,
        config.LEGACY_CACHE_HIGH_WATERMARK_BYTES,
        config.LEGACY_CACHE_TARGET_BYTES,
        {
            "composite_bytes": composite,
            "sqlite_bytes": sqlite_bytes,
            "sqlite_wal_bytes": wal_bytes,
            "temp_bytes": legacy_temp,
        },
    )
    return CacheUsage(
        generated_at=int(time.time()),
        pools={source_pool.name: source_pool, legacy_pool.name: legacy_pool},
    )


def _remove_source_to_target(target: int, max_items: int) -> tuple[int, int]:
    ledger = Path(config.SOURCE_ART_LEDGER_PATH)
    if not ledger.exists():
        return 0, 0
    deleted_items = deleted_bytes = 0
    try:
        with sqlite3.connect(ledger, timeout=5.0) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT source_art_id, path, byte_size FROM source_art_ledger "
                "WHERE pinned=0 AND reconstructable=1 ORDER BY last_used_at ASC "
                "LIMIT ?",
                (max_items,),
            ).fetchall()
            current = _source_usage()[0]
            for source_id, path, recorded in rows:
                if current <= target or deleted_items >= max_items:
                    break
                size = _file_size(path) or max(0, int(recorded or 0))
                try:
                    os.unlink(path)
                except OSError:
                    pass
                db.execute("DELETE FROM source_art_ledger WHERE source_art_id=?", (source_id,))
                deleted_items += 1
                deleted_bytes += size
                current = max(0, current - size)
            db.commit()
    except sqlite3.Error:
        return deleted_items, deleted_bytes
    return deleted_items, deleted_bytes


def _remove_legacy_to_target(target: int, max_items: int) -> tuple[int, int]:
    try:
        with sqlite3.connect(config.DB_PATH, timeout=5.0) as db:
            db.execute("BEGIN IMMEDIATE")
            composite, sqlite_bytes, wal_bytes, temp = _legacy_usage()
            # Database overhead cannot be removed without VACUUM; preserve it
            # and only evict reconstructable composite blobs to reach target.
            blob_target = max(0, target - sqlite_bytes - wal_bytes - temp)
            rows = db.execute(
                "SELECT cache_key, LENGTH(jpeg_bytes) FROM final_poster_cache "
                "ORDER BY cached_at ASC LIMIT ?",
                (max_items,),
            ).fetchall()
            deleted_items = deleted_bytes = 0
            for key, size in rows:
                if composite <= blob_target or deleted_items >= max_items:
                    break
                db.execute("DELETE FROM final_poster_cache WHERE cache_key=?", (key,))
                value = max(0, int(size or 0))
                composite = max(0, composite - value)
                deleted_items += 1
                deleted_bytes += value
            db.commit()
            return deleted_items, deleted_bytes
    except sqlite3.Error:
        return 0, 0


def _remove_expired_temp(max_items: int) -> tuple[int, int]:
    cutoff = time.time() - max(0, config.SOURCE_CACHE_RAW_MAX_AGE_SECONDS)
    removed = removed_bytes = 0
    roots = [Path(config.SOURCE_ART_CACHE_DIR) / "tmp", Path(config.DB_PATH).parent]
    for root in roots:
        for path in _bounded_files(root):
            if removed >= max_items or not path.name.startswith((".tmp-", "tmp-", "install-")):
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                size = path.stat().st_size
                path.unlink()
                removed += 1
                removed_bytes += size
            except OSError:
                continue
    return removed, removed_bytes


def prune_to_targets(*, max_items: int | None = None) -> PruneResult:
    started = int(time.time())
    budget = max(1, min(int(max_items or config.CACHE_PRUNE_MAX_ITEMS), config.CACHE_PRUNE_MAX_ITEMS))
    temp_items, temp_bytes = _remove_expired_temp(budget)
    remaining = max(0, budget - temp_items)
    source_items = source_bytes = legacy_items = legacy_bytes = 0
    usage = get_usage()
    source = usage.pools["source_derivatives"]
    if remaining and source.bytes > source.target_bytes:
        source_items, source_bytes = _remove_source_to_target(source.target_bytes, remaining)
        remaining -= source_items
    usage = get_usage()
    legacy = usage.pools["legacy"]
    if remaining and legacy.bytes > legacy.target_bytes:
        legacy_items, legacy_bytes = _remove_legacy_to_target(legacy.target_bytes, remaining)
    finished = int(time.time())
    return PruneResult(
        started_at=started,
        finished_at=finished,
        deleted_items=temp_items + source_items + legacy_items,
        deleted_bytes=temp_bytes + source_bytes + legacy_bytes,
        source_deleted_items=source_items,
        legacy_deleted_items=legacy_items,
        temp_deleted_items=temp_items,
    )


class FileLeaderLock:
    """Process-wide non-blocking leader lock for provider/background loops."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = str(path or config.CACHE_LEADER_LOCK_PATH)
        self._fd: int | None = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        import fcntl

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        import fcntl

        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "FileLeaderLock":
        if not self.acquire():
            raise RuntimeError("cache background leader is already held")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


__all__ = [
    "CachePoolUsage", "CacheUsage", "FileLeaderLock", "PruneResult",
    "get_usage", "prune_to_targets", "SCHEMA", "VERSION",
]
