"""Cache-budget, physical accounting and scheduler leadership regressions."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

import cache
import cache_policy
import config
from source_art import SourceArtStore, SourceResourceError


def _close_cache_connection() -> None:
    connection = getattr(cache._local, "conn", None)
    if connection is not None:
        connection.close()
        delattr(cache._local, "conn")
    cache._initialised = False
    cache._composite_l1.clear()


def _init_cache(tmp_path: Path) -> Path:
    _close_cache_connection()
    db_path = tmp_path / "cache.db"
    with (
        patch.object(cache, "DB_PATH", str(db_path)),
        patch.object(cache, "TMDB_POSTER_CACHE_DIR", str(tmp_path / "posters")),
        patch.object(cache, "TMDB_LOGO_CACHE_DIR", str(tmp_path / "logos")),
    ):
        cache.init_db()
    return db_path


def test_default_allocations_are_decimal_and_total_twenty_gb() -> None:
    assert config.SOURCE_CACHE_MAX_BYTES == 15_000_000_000
    assert config.SOURCE_CACHE_HIGH_WATERMARK_BYTES == 13_500_000_000
    assert config.SOURCE_CACHE_TARGET_BYTES == 12_000_000_000
    assert config.LEGACY_CACHE_MAX_BYTES == 5_000_000_000
    assert config.LEGACY_CACHE_HIGH_WATERMARK_BYTES == 4_500_000_000
    assert config.LEGACY_CACHE_TARGET_BYTES == 4_000_000_000
    assert config.SOURCE_CACHE_MAX_BYTES + config.LEGACY_CACHE_MAX_BYTES == 20_000_000_000


def test_file_leader_lock_is_single_process_wide(tmp_path: Path) -> None:
    path = tmp_path / "leader.lock"
    first = cache_policy.FileLeaderLock(path)
    second = cache_policy.FileLeaderLock(path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_l1_enforces_real_byte_cap_ttl_and_digest() -> None:
    now = time.time()
    with (
        patch.object(cache, "COMPOSITE_MEM_ENTRIES", 10),
        patch.object(cache, "COMPOSITE_MEM_MAX_BYTES", 5),
    ):
        cache._composite_l1.clear()
        first = b"1234"
        cache._l1_put(
            "first",
            cache._CompositeL1Entry(
                first,
                "IMAGE/WEBP",
                now + 60,
                hashlib.sha256(first).hexdigest(),
            ),
        )
        second = b"5678"
        cache._l1_put(
            "second",
            cache._CompositeL1Entry(
                second,
                "image/webp",
                now + 60,
                hashlib.sha256(second).hexdigest(),
            ),
        )
        assert list(cache._composite_l1) == ["second"]
        assert cache._composite_l1["second"].content_type == "image/webp"
        assert cache.composite_l1_stats() == {"entries": 1, "bytes": 4}

        cache._composite_l1["expired"] = cache._CompositeL1Entry(
            b"old", "image/webp", now - 1, hashlib.sha256(b"old").hexdigest()
        )
        cache._composite_l1["tampered"] = cache._CompositeL1Entry(
            b"bad", "image/webp", now + 60, "0" * 64
        )
        with patch.object(cache, "get_db", side_effect=RuntimeError("no L2")):
            assert cache.get_cached_final_poster("expired") is None
            assert cache.get_cached_final_poster("tampered") is None
        assert "expired" not in cache._composite_l1
        assert "tampered" not in cache._composite_l1
        cache._composite_l1.clear()


def test_usage_has_exact_pools_and_never_double_counts_sqlite_blobs(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    ledger_path = tmp_path / "source.sqlite"
    source_root = tmp_path / "source"
    source_file = source_root / "poster" / "aa" / "asset.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"source-derivative")
    blob = b"composite-payload"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE final_poster_cache (jpeg_bytes BLOB)")
        db.execute("INSERT INTO final_poster_cache VALUES (?)", (blob,))
    with sqlite3.connect(ledger_path) as db:
        db.execute(
            "CREATE TABLE source_art_ledger (path TEXT, byte_size INTEGER, last_used_at REAL)"
        )
        db.execute(
            "INSERT INTO source_art_ledger VALUES (?, ?, ?)",
            (str(source_file), source_file.stat().st_size, time.time()),
        )

    with (
        patch.object(cache_policy.config, "DB_PATH", str(db_path)),
        patch.object(cache_policy.config, "SOURCE_ART_CACHE_DIR", str(source_root)),
        patch.object(cache_policy.config, "SOURCE_ART_LEDGER_PATH", str(ledger_path)),
    ):
        usage = cache_policy.get_usage().to_dict()

    assert list(usage["pools"]) == list(cache_policy.POOL_NAMES)
    for pool in usage["pools"].values():
        assert set(pool) == {
            "bytes",
            "hard_limit_bytes",
            "high_watermark_bytes",
            "target_bytes",
        }
        assert all(isinstance(value, int) for value in pool.values())
    assert usage["pools"]["source_derivatives"]["bytes"] == len(b"source-derivative")
    assert usage["pools"]["legacy_composites"]["bytes"] == len(blob)
    expected_sqlite = max(db_path.stat().st_size - len(blob), 0) + ledger_path.stat().st_size
    assert usage["pools"]["sqlite"]["bytes"] == expected_sqlite
    assert usage["total_bytes"] == (
        source_file.stat().st_size + db_path.stat().st_size + ledger_path.stat().st_size
    )
    legacy_names = ("legacy_composites", "sqlite", "sqlite_wal", "temp")
    legacy_total = sum(usage["pools"][name]["bytes"] for name in legacy_names)
    for name in legacy_names:
        pool = usage["pools"][name]
        other = legacy_total - pool["bytes"]
        assert pool["hard_limit_bytes"] == max(
            0, config.LEGACY_CACHE_MAX_BYTES - other
        )


def test_source_prune_rejects_outside_paths_and_never_unlinks_symlinks(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"do-not-delete")
    symlink = source_root / "linked.jpg"
    symlink.symlink_to(outside)
    ledger = tmp_path / "ledger.sqlite"
    with sqlite3.connect(ledger) as db:
        db.execute(
            "CREATE TABLE source_art_ledger ("
            "source_art_id TEXT PRIMARY KEY, path TEXT, byte_size INTEGER, "
            "pinned INTEGER, reconstructable INTEGER, last_used_at REAL)"
        )
        db.executemany(
            "INSERT INTO source_art_ledger VALUES (?, ?, ?, 0, 1, ?)",
            (
                ("outside", str(outside), outside.stat().st_size, 1.0),
                ("symlink", str(symlink), outside.stat().st_size, 2.0),
            ),
        )
    with (
        patch.object(cache_policy.config, "SOURCE_ART_CACHE_DIR", str(source_root)),
        patch.object(cache_policy.config, "SOURCE_ART_LEDGER_PATH", str(ledger)),
    ):
        result = cache_policy._remove_source_to_target(0, 10)
    assert result.items == 2
    assert result.bytes == 0
    assert outside.read_bytes() == b"do-not-delete"
    assert symlink.is_symlink()


def test_prune_waits_for_high_watermark_and_reports_before_after(monkeypatch) -> None:
    def usage(source: int, legacy: int) -> cache_policy.CacheUsage:
        limits = (100, 90, 80)
        pools = {
            name: cache_policy.CachePoolUsage(
                name,
                source if name == "source_derivatives" else (
                    legacy if name == "legacy_composites" else 0
                ),
                *limits,
            )
            for name in cache_policy.POOL_NAMES
        }
        return cache_policy.CacheUsage(1, pools)

    calls: list[str] = []
    monkeypatch.setattr(cache_policy.config, "SOURCE_CACHE_HIGH_WATERMARK_BYTES", 90)
    monkeypatch.setattr(cache_policy.config, "SOURCE_CACHE_TARGET_BYTES", 80)
    monkeypatch.setattr(cache_policy.config, "LEGACY_CACHE_HIGH_WATERMARK_BYTES", 90)
    monkeypatch.setattr(cache_policy.config, "LEGACY_CACHE_TARGET_BYTES", 80)
    monkeypatch.setattr(cache_policy, "get_usage", lambda: usage(85, 85))
    monkeypatch.setattr(cache_policy, "_remove_expired_temp", lambda _limit: cache_policy.Eviction())
    monkeypatch.setattr(
        cache_policy,
        "_remove_source_to_target",
        lambda *_args: calls.append("source") or cache_policy.Eviction(),
    )
    monkeypatch.setattr(
        cache_policy,
        "_remove_legacy_to_target",
        lambda *_args: calls.append("legacy") or cache_policy.Eviction(),
    )
    payload = cache_policy.prune_to_targets(max_items=10).to_dict()
    assert calls == []
    assert set(payload) == {
        "schema",
        "version",
        "started_at",
        "finished_at",
        "before",
        "after",
        "evictions",
    }
    assert list(payload["evictions"]) == list(cache_policy.POOL_NAMES)


def test_temp_prune_never_touches_database_or_non_temp_files(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    ordinary = tmp_path / "poster.webp"
    temporary = tmp_path / "tmp-abandoned"
    db_path.write_bytes(b"database")
    ordinary.write_bytes(b"poster")
    temporary.write_bytes(b"temporary")
    old = time.time() - 10_000
    for path in (db_path, ordinary, temporary):
        os.utime(path, (old, old))
    with (
        patch.object(cache_policy.config, "DB_PATH", str(db_path)),
        patch.object(cache_policy.config, "SOURCE_ART_CACHE_DIR", str(tmp_path / "source")),
        patch.object(cache_policy.config, "SOURCE_CACHE_RAW_MAX_AGE_SECONDS", 1),
    ):
        result = cache_policy._remove_expired_temp(10)
    assert result == cache_policy.Eviction(1, len(b"temporary"))
    assert db_path.read_bytes() == b"database"
    assert ordinary.read_bytes() == b"poster"
    assert not temporary.exists()


def test_bounded_walk_charges_directories_and_non_files(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(10):
        (root / f"dir-{index}").mkdir()
    state = cache_policy.ScanState()
    assert list(cache_policy._bounded_files(root, limit=3, state=state)) == []
    assert state.visited_entries == 3
    assert state.incomplete is True


def test_incomplete_temp_accounting_is_conservative(monkeypatch, tmp_path: Path) -> None:
    def incomplete_scan(_root, *, limit=0, state=None):
        assert state is not None
        state.incomplete = True
        if False:
            yield Path("unused")

    monkeypatch.setattr(cache_policy, "_bounded_files", incomplete_scan)
    monkeypatch.setattr(cache_policy.config, "SOURCE_ART_CACHE_DIR", str(tmp_path / "source"))
    monkeypatch.setattr(cache_policy.config, "DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setattr(
        cache_policy.config, "SOURCE_ART_LEDGER_PATH", str(tmp_path / "ledger.db")
    )
    temp_bytes, incomplete = cache_policy._temp_bytes()
    assert incomplete is True
    assert temp_bytes >= (
        config.SOURCE_CACHE_MAX_BYTES + config.LEGACY_CACHE_MAX_BYTES
    )


def test_composite_insert_rejects_payload_that_cannot_fit_hard_limit(tmp_path: Path) -> None:
    db_path = _init_cache(tmp_path)
    try:
        baseline = sum(
            path.stat().st_size
            for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
            if path.exists()
        )
        with (
            patch.object(cache.runtime_config, "DB_PATH", str(db_path)),
            patch.object(cache.runtime_config, "SOURCE_ART_LEDGER_PATH", str(tmp_path / "none.sqlite")),
            patch.object(cache.runtime_config, "LEGACY_CACHE_MAX_BYTES", baseline + 64 * 1024),
        ):
            accepted = cache.set_cached_final_poster("too-large", b"x" * (2 * 1024 * 1024))
        assert accepted is False
        assert cache.get_db().execute(
            "SELECT 1 FROM final_poster_cache WHERE cache_key='too-large'"
        ).fetchone() is None
    finally:
        _close_cache_connection()


def test_source_install_is_atomic_at_hard_limit(tmp_path: Path) -> None:
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    with patch.object(config, "SOURCE_CACHE_MAX_BYTES", 4):
        with pytest.raises(SourceResourceError, match="hard limit"):
            store.install(
                kind="poster",
                recipe_version=1,
                payload=b"12345",
                mime="image/jpeg",
                width=1,
                height=1,
                locator=None,
                now=None,
                pinned=False,
                reconstructable=False,
            )
    with sqlite3.connect(tmp_path / "ledger.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM source_art_ledger").fetchone()[0] == 0
    assert not tuple((tmp_path / "source").rglob("*.jpg"))


def test_source_capacity_reservations_are_atomic_and_explicitly_released(
    tmp_path: Path,
) -> None:
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")

    def reserve() -> str | None:
        try:
            return store.reserve_capacity(15)
        except SourceResourceError:
            return None

    with patch.object(config, "SOURCE_CACHE_MAX_BYTES", 20):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: reserve(), range(2)))
        accepted = [token for token in results if token is not None]
        assert len(accepted) == 1
        with sqlite3.connect(tmp_path / "ledger.sqlite") as db:
            assert db.execute(
                "SELECT COALESCE(SUM(byte_size), 0) "
                "FROM source_art_capacity_reservations"
            ).fetchone()[0] == 15
        store.release_capacity(accepted[0])
        replacement = store.reserve_capacity(20)
        store.release_capacity(replacement)


def test_source_capacity_fails_closed_when_temp_scan_is_incomplete(tmp_path: Path) -> None:
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    temp = tmp_path / "source" / "tmp"
    temp.mkdir()
    for index in range(257):
        (temp / f"entry-{index}").mkdir()
    with pytest.raises(SourceResourceError, match="capacity unknown"):
        store.reserve_capacity(1)


def test_cache_endpoints_require_hmac_reject_replay_and_bound_body(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from uuid import uuid4

    import main
    from service_auth import MemoryNonceStore, build_auth_headers

    secret = b"cache-contract-secret"
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", secret.decode())
    monkeypatch.setattr(main, "_V2_NONCE_STORE", MemoryNonceStore())
    offloaded: list[object] = []

    async def fake_to_thread(function, *args, **kwargs):
        offloaded.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(main.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(
        main,
        "get_cache_usage",
        lambda: cache_policy.CacheUsage(
            int(time.time()),
            {
                name: cache_policy.CachePoolUsage(name, 0, 10, 9, 8)
                for name in cache_policy.POOL_NAMES
            },
        ),
    )
    client = TestClient(main.app)

    timestamp = int(time.time())
    request_id = uuid4()
    get_headers = build_auth_headers(
        method="GET",
        path="/v2/cache/usage",
        body=b"",
        request_id=request_id,
        timestamp=timestamp,
        secret=secret,
        caller="bingecat",
        audience="postersplus",
    )
    assert client.get("/v2/cache/usage").status_code == 401
    response = client.get("/v2/cache/usage", headers=get_headers)
    assert response.status_code == 200
    assert offloaded == [main.get_cache_usage]
    assert list(response.json()["pools"]) == list(cache_policy.POOL_NAMES)
    assert client.get("/v2/cache/usage", headers=get_headers).status_code == 403

    huge = b"x" * (256 * 1024 + 1)
    huge_headers = build_auth_headers(
        method="POST",
        path="/v2/cache/prune",
        body=huge,
        request_id=uuid4(),
        timestamp=timestamp,
        secret=secret,
        caller="bingecat",
        audience="postersplus",
    )
    too_large = client.post("/v2/cache/prune", content=huge, headers=huge_headers)
    assert too_large.status_code == 413
    assert too_large.json()["detail"] == "request_body_too_large"

    body = b"{}"
    query_headers = build_auth_headers(
        method="POST",
        path="/v2/cache/prune",
        body=body,
        request_id=uuid4(),
        timestamp=timestamp,
        secret=secret,
        caller="bingecat",
        audience="postersplus",
    )
    rejected_query = client.post(
        "/v2/cache/prune?path=/tmp", content=body, headers=query_headers
    )
    assert rejected_query.status_code == 400
    assert rejected_query.json()["detail"] == "query_string_not_allowed"
