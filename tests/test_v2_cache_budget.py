"""Focused cache-budget and scheduler leadership regression tests."""

from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import patch

import cache
import cache_policy


def test_file_leader_lock_is_single_process_wide():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "leader.lock")
        first = cache_policy.FileLeaderLock(path)
        second = cache_policy.FileLeaderLock(path)
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert second.acquire() is True
        second.release()


def test_l1_entry_has_digest_and_expiry_and_invalidates_stale():
    with patch.object(cache, "COMPOSITE_MEM_ENTRIES", 4), patch.dict(
        os.environ, {"COMPOSITE_MEM_MAX_BYTES": "100"}, clear=False
    ):
        cache._composite_l1.clear()
        cache._l1_put(
            "fresh",
            cache._CompositeL1Entry(b"payload", "image/webp", time.time() + 60, "digest"),
        )
        entry = cache._composite_l1["fresh"]
        assert entry.content_type == "image/webp"
        assert entry.content_hash == "digest"
        assert entry.expires_at > time.time()
        cache._l1_put(
            "expired",
            cache._CompositeL1Entry(b"old", "image/webp", time.time() - 1, "old"),
        )
        # A direct hot-path read removes an expired L1 entry before falling
        # through to L2; no stale bytes can survive in memory.
        with patch.object(cache, "get_db", side_effect=RuntimeError("no L2")):
            assert cache.get_cached_final_poster("expired") is None
        assert "expired" not in cache._composite_l1
        cache._composite_l1.clear()


def test_usage_reports_independent_pools_and_integer_limits(tmp_path):
    db_path = tmp_path / "cache.db"
    source_root = tmp_path / "source"
    ledger = tmp_path / "source.sqlite"
    source_root.mkdir()
    with patch.object(cache_policy.config, "DB_PATH", str(db_path)), patch.object(
        cache_policy.config, "SOURCE_ART_CACHE_DIR", str(source_root)
    ), patch.object(cache_policy.config, "SOURCE_ART_LEDGER_PATH", str(ledger)):
        usage = cache_policy.get_usage().to_dict()
    assert usage["schema"] == cache_policy.SCHEMA
    assert set(usage["pools"]) == {"source_derivatives", "legacy"}
    for pool in usage["pools"].values():
        assert all(isinstance(pool[key], int) for key in ("bytes", "hard_bytes", "high_watermark_bytes", "target_bytes"))

