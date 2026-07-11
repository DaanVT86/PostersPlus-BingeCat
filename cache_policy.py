"""Bounded physical cache accounting and eviction policy.

The public contract reports physical bytes exactly once.  Composite BLOB bytes
are shown separately from SQLite overhead, so ``sum(pool.bytes)`` describes the
space on disk instead of double-counting the database file that contains them.
All filesystem reconciliation is bounded and never follows symlinks.

Temporary staging has no independent persistent allocation: it is charged to
Core's shared 5 GB legacy group while active.  The deployment-wide 10 GB
headroom absorbs short staging spikes, but is never advertised as cache budget.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config


SCHEMA = "postersplus_cache_usage"
VERSION = 1
POOL_NAMES = (
    "source_derivatives",
    "legacy_composites",
    "sqlite",
    "sqlite_wal",
    "temp",
)
RECONCILE_LIMIT = 256
FILE_WALK_LIMIT = 20_000


@dataclass(frozen=True, slots=True)
class CachePoolUsage:
    name: str
    bytes: int
    hard_limit_bytes: int
    high_watermark_bytes: int
    target_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "bytes": max(0, int(self.bytes)),
            "hard_limit_bytes": max(0, int(self.hard_limit_bytes)),
            "high_watermark_bytes": max(0, int(self.high_watermark_bytes)),
            "target_bytes": max(0, int(self.target_bytes)),
        }


@dataclass(frozen=True, slots=True)
class CacheUsage:
    generated_at: int
    pools: dict[str, CachePoolUsage]

    @property
    def total_bytes(self) -> int:
        return sum(pool.bytes for pool in self.pools.values())

    @property
    def source_group_bytes(self) -> int:
        return self.pools["source_derivatives"].bytes

    @property
    def legacy_group_bytes(self) -> int:
        return sum(
            self.pools[name].bytes
            for name in ("legacy_composites", "sqlite", "sqlite_wal", "temp")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "version": VERSION,
            "generated_at": int(self.generated_at),
            "total_bytes": int(self.total_bytes),
            "pools": {name: self.pools[name].to_dict() for name in POOL_NAMES},
        }


@dataclass(frozen=True, slots=True)
class Eviction:
    items: int = 0
    bytes: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"items": max(0, int(self.items)), "bytes": max(0, int(self.bytes))}


@dataclass(slots=True)
class ScanState:
    visited_entries: int = 0
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class PruneResult:
    started_at: int
    finished_at: int
    before: CacheUsage
    after: CacheUsage
    evictions: dict[str, Eviction]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "version": VERSION,
            "started_at": int(self.started_at),
            "finished_at": int(self.finished_at),
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "evictions": {
                name: self.evictions.get(name, Eviction()).to_dict()
                for name in POOL_NAMES
            },
        }


def _file_size(path: str | os.PathLike[str]) -> int:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return 0
    return max(0, int(info.st_size)) if stat.S_ISREG(info.st_mode) else 0


def _bounded_files(
    root: str | os.PathLike[str],
    *,
    limit: int = FILE_WALK_LIMIT,
    state: ScanState | None = None,
):
    """Yield files while charging every encountered entry to one hard limit."""

    scan = state if state is not None else ScanState()
    base = Path(root)
    if not base.exists() or base.is_symlink():
        return
    stack = [base]
    while stack:
        if scan.visited_entries >= limit:
            scan.incomplete = True
            return
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if scan.visited_entries >= limit:
                    scan.incomplete = True
                    return
                scan.visited_entries += 1
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path)
                except OSError:
                    continue


def _sum_query(path: str | os.PathLike[str], sql: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        with sqlite3.connect(path, timeout=2.0) as db:
            row = db.execute(sql).fetchone()
        return max(0, int((row or (0,))[0] or 0))
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def _source_derivative_bytes() -> int:
    """Cheap ledger aggregate plus a bounded corruption reconciliation sample."""

    ledger = config.SOURCE_ART_LEDGER_PATH
    total = _sum_query(
        ledger,
        "SELECT COALESCE(SUM(byte_size), 0) FROM source_art_ledger",
    )
    if not os.path.exists(ledger):
        return total
    # Sampling catches stale/corrupt rows without stat'ing an unbounded ledger.
    try:
        with sqlite3.connect(ledger, timeout=2.0) as db:
            rows = db.execute(
                "SELECT path, byte_size FROM source_art_ledger "
                "ORDER BY last_used_at ASC LIMIT ?",
                (RECONCILE_LIMIT,),
            ).fetchall()
        for path, recorded in rows:
            if not _source_path_is_regular(path):
                total = max(0, total - max(0, int(recorded or 0)))
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return total


def _composite_bytes() -> int:
    return _sum_query(
        config.DB_PATH,
        "SELECT COALESCE(SUM(LENGTH(jpeg_bytes)), 0) FROM final_poster_cache",
    )


def _source_path_parts(path: object) -> tuple[str, ...] | None:
    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    root = Path(config.SOURCE_ART_CACHE_DIR).absolute()
    candidate = Path(path)
    if not candidate.is_absolute():
        return None
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def _open_source_parent(path: object) -> tuple[int, str] | None:
    """Open the parent through no-follow dirfds, eliminating symlink races."""

    parts = _source_path_parts(path)
    if parts is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(config.SOURCE_ART_CACHE_DIR, flags)
    except OSError:
        return None
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except OSError:
        os.close(fd)
        return None


def _source_path_is_regular(path: object) -> bool:
    opened = _open_source_parent(path)
    if opened is None:
        return False
    parent_fd, name = opened
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return stat.S_ISREG(info.st_mode)
    except OSError:
        return False
    finally:
        os.close(parent_fd)


def _unlink_source_regular(path: object) -> int:
    opened = _open_source_parent(path)
    if opened is None:
        return 0
    parent_fd, name = opened
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return 0
        size = max(0, int(info.st_size))
        os.unlink(name, dir_fd=parent_fd)
        return size
    except OSError:
        return 0
    finally:
        os.close(parent_fd)


def _temp_bytes() -> tuple[int, bool]:
    total = 0
    counted: set[Path] = set()
    scan = ScanState()
    source_tmp = Path(config.SOURCE_ART_CACHE_DIR) / "tmp"
    for path in _bounded_files(source_tmp, state=scan):
        absolute = path.absolute()
        counted.add(absolute)
        total += _file_size(path)
    db_dir = Path(config.DB_PATH).parent
    ledger_dir = Path(config.SOURCE_ART_LEDGER_PATH).parent
    seen_roots: set[Path] = set()
    for root in (db_dir, ledger_dir):
        root = root.absolute()
        if root in seen_roots:
            continue
        seen_roots.add(root)
        for path in _bounded_files(root, state=scan):
            absolute = path.absolute()
            if absolute in counted:
                continue
            if path.name.startswith((".tmp-", "tmp-", "install-", "raw-")):
                counted.add(absolute)
                total += _file_size(path)
    if scan.incomplete:
        # The exact tail is unknowable.  Report a conservative allocation-sized
        # sentinel so cleanup is triggered and callers never see a false low.
        total = max(
            total,
            config.SOURCE_CACHE_MAX_BYTES + config.LEGACY_CACHE_MAX_BYTES,
        )
    return total, scan.incomplete


def get_usage() -> CacheUsage:
    source = _source_derivative_bytes()
    composites = _composite_bytes()
    cache_db_size = _file_size(config.DB_PATH)
    ledger_db_size = _file_size(config.SOURCE_ART_LEDGER_PATH)
    # BLOB bytes live inside cache.db.  Only the remainder is SQLite overhead.
    sqlite_overhead = max(cache_db_size - composites, 0) + ledger_db_size
    wal = sum(
        _file_size(path)
        for path in (
            f"{config.DB_PATH}-wal",
            f"{config.DB_PATH}-shm",
            f"{config.SOURCE_ART_LEDGER_PATH}-wal",
            f"{config.SOURCE_ART_LEDGER_PATH}-shm",
        )
    )
    temp, _temp_incomplete = _temp_bytes()
    source_limits = (
        config.SOURCE_CACHE_MAX_BYTES,
        config.SOURCE_CACHE_HIGH_WATERMARK_BYTES,
        config.SOURCE_CACHE_TARGET_BYTES,
    )
    values = {
        "source_derivatives": source,
        "legacy_composites": composites,
        "sqlite": sqlite_overhead,
        "sqlite_wal": wal,
        "temp": temp,
    }
    pools = {
        "source_derivatives": CachePoolUsage(
            "source_derivatives", values["source_derivatives"], *source_limits
        )
    }
    legacy_names = ("legacy_composites", "sqlite", "sqlite_wal", "temp")
    legacy_total = sum(values[name] for name in legacy_names)
    for name in legacy_names:
        other_bytes = legacy_total - values[name]
        pools[name] = CachePoolUsage(
            name,
            values[name],
            max(0, config.LEGACY_CACHE_MAX_BYTES - other_bytes),
            max(0, config.LEGACY_CACHE_HIGH_WATERMARK_BYTES - other_bytes),
            max(0, config.LEGACY_CACHE_TARGET_BYTES - other_bytes),
        )
    return CacheUsage(generated_at=int(time.time()), pools=pools)


def _remove_source_to_target(target: int, max_items: int) -> Eviction:
    ledger = config.SOURCE_ART_LEDGER_PATH
    if not os.path.exists(ledger):
        return Eviction()
    removed_items = removed_bytes = 0
    try:
        with sqlite3.connect(ledger, timeout=5.0) as db:
            db.execute("BEGIN IMMEDIATE")
            current = max(
                0,
                int(
                    db.execute(
                        "SELECT COALESCE(SUM(byte_size), 0) FROM source_art_ledger"
                    ).fetchone()[0]
                    or 0
                ),
            )
            rows = db.execute(
                "SELECT source_art_id, path, byte_size FROM source_art_ledger "
                "WHERE pinned=0 AND reconstructable=1 ORDER BY last_used_at ASC LIMIT ?",
                (max_items,),
            ).fetchall()
            for source_id, path, recorded in rows:
                if current <= target:
                    break
                recorded_size = max(0, int(recorded or 0))
                actual = _unlink_source_regular(path)
                db.execute("DELETE FROM source_art_ledger WHERE source_art_id=?", (source_id,))
                current = max(0, current - recorded_size)
                removed_items += 1
                removed_bytes += actual
            db.commit()
    except (sqlite3.Error, TypeError, ValueError):
        return Eviction(removed_items, removed_bytes)
    return Eviction(removed_items, removed_bytes)


def _remove_legacy_to_target(target: int, max_items: int) -> Eviction:
    if not os.path.exists(config.DB_PATH):
        return Eviction()
    removed_items = removed_bytes = 0
    try:
        with sqlite3.connect(config.DB_PATH, timeout=5.0) as db:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            usage = get_usage()
            current = usage.legacy_group_bytes
            rows = db.execute(
                "SELECT cache_key, LENGTH(jpeg_bytes) FROM final_poster_cache "
                "ORDER BY cached_at ASC, cache_key ASC LIMIT ?",
                (max_items,),
            ).fetchall()
            for key, raw_size in rows:
                if current <= target:
                    break
                size = max(0, int(raw_size or 0))
                db.execute("DELETE FROM final_poster_cache WHERE cache_key=?", (key,))
                current = max(0, current - size)
                removed_items += 1
                removed_bytes += size
            db.commit()
            # Both calls are bounded; they make freed pages reusable/reclaimable
            # without a blocking full VACUUM.
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            db.execute("PRAGMA incremental_vacuum(1000)")
    except (sqlite3.Error, TypeError, ValueError):
        return Eviction(removed_items, removed_bytes)
    return Eviction(removed_items, removed_bytes)


def _remove_expired_temp(max_items: int) -> Eviction:
    cutoff = time.time() - max(0, config.SOURCE_CACHE_RAW_MAX_AGE_SECONDS)
    removed_items = removed_bytes = 0
    roots = (
        (Path(config.SOURCE_ART_CACHE_DIR) / "tmp", False),
        (Path(config.DB_PATH).parent, True),
    )
    scan = ScanState()
    for root, require_temp_name in roots:
        for path in _bounded_files(root, state=scan):
            if removed_items >= max_items:
                return Eviction(removed_items, removed_bytes)
            if require_temp_name and not path.name.startswith(
                (".tmp-", "tmp-", "install-", "raw-")
            ):
                continue
            try:
                info = path.stat(follow_symlinks=False)
                if info.st_mtime >= cutoff:
                    continue
                path.unlink()
                removed_items += 1
                removed_bytes += max(0, int(info.st_size))
            except OSError:
                continue
    return Eviction(removed_items, removed_bytes)


def prune_to_targets(*, max_items: int | None = None) -> PruneResult:
    started = int(time.time())
    budget = max(
        1,
        min(int(max_items or config.CACHE_PRUNE_MAX_ITEMS), config.CACHE_PRUNE_MAX_ITEMS),
    )
    before = get_usage()
    evictions: dict[str, Eviction] = {}
    temp = _remove_expired_temp(budget)
    evictions["temp"] = temp
    remaining = max(0, budget - temp.items)

    # A pool is intentionally untouched between target and high watermark.
    if (
        remaining
        and before.source_group_bytes > config.SOURCE_CACHE_HIGH_WATERMARK_BYTES
    ):
        source = _remove_source_to_target(config.SOURCE_CACHE_TARGET_BYTES, remaining)
        evictions["source_derivatives"] = source
        remaining -= source.items

    middle = get_usage()
    if (
        remaining
        and middle.legacy_group_bytes > config.LEGACY_CACHE_HIGH_WATERMARK_BYTES
    ):
        legacy = _remove_legacy_to_target(config.LEGACY_CACHE_TARGET_BYTES, remaining)
        evictions["legacy_composites"] = legacy

    after = get_usage()
    return PruneResult(
        started_at=started,
        finished_at=int(time.time()),
        before=before,
        after=after,
        evictions=evictions,
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
    "CachePoolUsage",
    "CacheUsage",
    "Eviction",
    "FileLeaderLock",
    "POOL_NAMES",
    "PruneResult",
    "SCHEMA",
    "VERSION",
    "get_usage",
    "prune_to_targets",
]
