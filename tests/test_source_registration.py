from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import source_registry
from remote_source_store import RemoteSourceArtStore
from service_auth import build_auth_headers
from source_art import (
    SourceArtStore,
    SourceDigestMismatch,
    SourceResourceError,
    SourceVerificationKey,
    exact_source_mount,
    validate_source_mount,
    verification_key_signature,
)


def _mount_escape(value: str) -> str:
    return value.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")


def _mountinfo(path: Path, filesystem: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    mountpoint = path.parent / "source art"
    path.write_text(
        "101 90 0:51 / /app/cache rw,relatime - ext4 /dev/vda1 rw\n"
        f"102 101 0:52 /export\\040source {_mount_escape(str(mountpoint))} "
        f"ro,relatime - {filesystem} 10.0.0.1:/export/source ro\n",
        encoding="utf-8",
    )
    return path


def test_mountinfo_decodes_and_requires_exact_role_mount(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mount info"
    target = tmp_path / "source art"
    _mountinfo(mountinfo, "nfs4")

    # The fixture target is the decoded parent path, proving escaped fields
    # are compared after decoding rather than by raw text.
    record = exact_source_mount(target, mountinfo_path=mountinfo)
    assert record is not None
    assert record.mountpoint == str(target)
    assert record.root == "/export source"
    assert record.filesystem == "nfs4"
    assert validate_source_mount(
        target, expectation="oracle-nfs", mountinfo_path=mountinfo
    ) == record
    with pytest.raises(SourceResourceError, match="local filesystem"):
        validate_source_mount(target, expectation="owner-local", mountinfo_path=mountinfo)

    local_mountinfo = tmp_path / "local-mountinfo"
    _mountinfo(local_mountinfo, "ext4")
    assert validate_source_mount(
        target, expectation="owner-local", mountinfo_path=local_mountinfo
    ).filesystem == "ext4"

    parent_only = tmp_path / "parent-only"
    parent_only.write_text(
        "101 90 0:51 / /app/cache rw,relatime - ext4 /dev/vda1 rw\n",
        encoding="utf-8",
    )
    with pytest.raises(SourceResourceError, match="exact mountpoint"):
        validate_source_mount(
            "/app/cache/source_art",
            expectation="owner-local",
            mountinfo_path=parent_only,
        )


def test_remote_mount_validation_rejects_non_nfs_and_missing_exact_target(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    incoming_root = tmp_path / "incoming"
    artifact_root.mkdir()
    incoming_root.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"101 90 0:51 / {_mount_escape(str(artifact_root))} ro,relatime - ext4 /dev/vda1 ro\n"
        f"102 90 0:52 / {_mount_escape(str(incoming_root))} rw,relatime - nfs4 10.0.0.1:/incoming rw\n",
        encoding="utf-8",
    )
    store = RemoteSourceArtStore(
        "http://127.0.0.1:18087",
        "test-secret",
        artifact_root,
        incoming_root,
        mountinfo_path=mountinfo,
    )
    with pytest.raises(SourceResourceError, match="exact NFS"):
        store._ensure_mounts()


def _verification_key(source_sha256: str) -> SourceVerificationKey:
    return SourceVerificationKey(
        kind="poster",
        source_sha256=source_sha256,
        title_context_sha256="b" * 64,
        detection_rules="ppocr.textless.v1",
        model="ppocrv5-mobile",
        runtime="rapidocr_onnxruntime",
        runtime_version="v1-m1234567890-r1234567890",
        architecture="x86_64",
    )


def test_local_verification_memo_is_digest_bound_and_corruption_is_rejected(
    tmp_path: Path,
) -> None:
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    payload = b"normalized-jpeg-payload"
    derivative = store.install(
        kind="poster",
        recipe_version=1,
        payload=payload,
        mime="image/jpeg",
        width=1,
        height=1,
        locator=None,
        now=datetime.now(timezone.utc),
        pinned=False,
        reconstructable=False,
    )
    key = _verification_key(derivative.sha256)
    memo = store.register_verification(
        key,
        result="unknown",
        verified_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )
    assert memo.signature == verification_key_signature(key)
    assert store.lookup_verification(key) == memo

    Path(derivative.path).write_bytes(b"tampered")
    with pytest.raises(SourceDigestMismatch):
        store.lookup_verification(key)


def test_expired_reservation_cannot_install_and_ledger_stays_empty(tmp_path: Path) -> None:
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    token = store.reserve_capacity(32, ttl_seconds=1)
    with sqlite3.connect(tmp_path / "ledger.sqlite") as connection:
        connection.execute(
            "UPDATE source_art_capacity_reservations SET expires_at=0 WHERE token=?",
            (token,),
        )
    with pytest.raises(SourceResourceError, match="expired"):
        store.install(
            kind="poster",
            recipe_version=1,
            payload=b"payload",
            mime="image/jpeg",
            width=1,
            height=1,
            locator=None,
            now=datetime.now(timezone.utc),
            pinned=False,
            reconstructable=False,
            reservation_token=token,
        )
    with sqlite3.connect(tmp_path / "ledger.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_art_ledger").fetchone()[0] == 0


def test_owner_staging_nested_directory_blocks_reservation(tmp_path: Path) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "golden-fixture").mkdir()
    store = SourceArtStore(
        tmp_path / "source",
        tmp_path / "ledger.sqlite",
        staging_root=incoming,
        require_staging_root=True,
    )

    with pytest.raises(SourceResourceError, match="non-regular entry"):
        store.reserve_capacity(32)

    with sqlite3.connect(tmp_path / "ledger.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM source_art_capacity_reservations"
        ).fetchone()[0] == 0


def test_owner_capacity_includes_legacy_source_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import config

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    source_root = tmp_path / "source"
    source_root.mkdir()
    legacy_tmp = source_root / "tmp"
    legacy_tmp.mkdir()
    (legacy_tmp / "orphan-download.part").write_bytes(b"orphan")
    monkeypatch.setattr(config, "SOURCE_CACHE_MAX_BYTES", 32)
    store = SourceArtStore(
        source_root,
        tmp_path / "ledger.sqlite",
        staging_root=incoming,
        require_staging_root=True,
    )

    with pytest.raises(SourceResourceError, match="hard limit"):
        store.reserve_capacity(32)


def test_local_from_config_opt_in_accounts_incoming_and_legacy_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config

    source_root = tmp_path / "source"
    incoming = tmp_path / "incoming"
    source_root.mkdir()
    incoming.mkdir()
    (source_root / "tmp").mkdir()
    (source_root / "tmp" / "legacy-raw.part").write_bytes(b"legacy")
    (incoming / "oracle-orphan.part").write_bytes(b"incoming")
    monkeypatch.setattr(config, "POSTERSPLUS_SOURCE_STORE_MODE", "local")
    monkeypatch.setattr(config, "POSTERSPLUS_SOURCE_REGISTRY_MODE", "disabled")
    monkeypatch.setattr(config, "POSTERSPLUS_SOURCE_ACCOUNT_INCOMING", True)
    monkeypatch.setattr(config, "POSTERSPLUS_SOURCE_REQUIRE_MOUNTS", False)
    monkeypatch.setattr(config, "SOURCE_ART_CACHE_DIR", str(source_root))
    monkeypatch.setattr(config, "SOURCE_ART_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setattr(config, "POSTERSPLUS_SOURCE_INCOMING_DIR", str(incoming))
    monkeypatch.setattr(config, "SOURCE_CACHE_MAX_BYTES", 14)

    store = SourceArtStore.from_config()
    assert store.staging_root == incoming
    assert store.require_staging_root is True
    with pytest.raises(SourceResourceError, match="hard limit"):
        store.reserve_capacity(1)


def test_owner_reserve_reclaims_stale_orphan_but_preserves_active_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config

    source_root = tmp_path / "source"
    incoming = tmp_path / "incoming"
    source_root.mkdir()
    incoming.mkdir()
    monkeypatch.setattr(config, "SOURCE_CACHE_RAW_MAX_AGE_SECONDS", 0)
    monkeypatch.setattr(config, "SOURCE_CACHE_MAX_BYTES", 1024)
    store = SourceArtStore(
        source_root,
        tmp_path / "ledger.sqlite",
        staging_root=incoming,
        require_staging_root=True,
        reclaim_staging=True,
    )

    orphan = incoming / "raw-123456"
    orphan.write_bytes(b"orphan")
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    expired_token = store.reserve_capacity(16, ttl_seconds=1)
    with sqlite3.connect(tmp_path / "ledger.sqlite") as connection:
        connection.execute(
            "UPDATE source_art_capacity_reservations SET expires_at=0 WHERE token=?",
            (expired_token,),
        )

    active_token = store.reserve_capacity(32, ttl_seconds=300)
    assert not orphan.exists()
    active = incoming / "raw-abcdef"
    active.write_bytes(b"active")
    # The active reservation started only moments ago.  A small NFS timestamp
    # lag must not make this staged file look like a pre-reservation orphan.
    recent = time.time() - 30
    os.utime(active, (recent, recent))

    next_token = store.reserve_capacity(32, ttl_seconds=300)
    assert active.exists()
    store.release_capacity(active_token)
    store.release_capacity(next_token)


def test_owner_reserve_reclaims_more_than_legacy_scan_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config

    source_root = tmp_path / "source"
    incoming = tmp_path / "incoming"
    source_root.mkdir()
    incoming.mkdir()
    monkeypatch.setattr(config, "SOURCE_CACHE_RAW_MAX_AGE_SECONDS", 0)
    store = SourceArtStore(
        source_root,
        tmp_path / "ledger.sqlite",
        staging_root=incoming,
        require_staging_root=True,
        reclaim_staging=True,
    )
    old = time.time() - 3600
    for index in range(300):
        path = incoming / f"raw-{index:06d}"
        path.write_bytes(b"orphan")
        os.utime(path, (old, old))

    token = store.reserve_capacity(32)
    assert not tuple(incoming.iterdir())
    store.release_capacity(token)


def test_owner_cleanup_preserves_unknown_and_fails_closed_on_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config

    source_root = tmp_path / "source"
    incoming = tmp_path / "incoming"
    source_root.mkdir()
    incoming.mkdir()
    monkeypatch.setattr(config, "SOURCE_CACHE_RAW_MAX_AGE_SECONDS", 0)
    store = SourceArtStore(
        source_root,
        tmp_path / "ledger.sqlite",
        staging_root=incoming,
        require_staging_root=True,
        reclaim_staging=True,
    )
    unknown = incoming / "operator-kept-file.bin"
    unknown.write_bytes(b"keep")
    stale = incoming / "raw-abcdef"
    stale.write_bytes(b"delete")
    old = time.time() - 3600
    os.utime(unknown, (old, old))
    os.utime(stale, (old, old))
    token = store.reserve_capacity(32)
    assert unknown.exists()
    assert not stale.exists()
    store.release_capacity(token)

    outside = tmp_path / "outside"
    outside.write_bytes(b"do-not-follow")
    symlink = incoming / "raw-123456"
    symlink.symlink_to(outside)
    stale_again = incoming / "raw-654321"
    stale_again.write_bytes(b"must-remain-on-failure")
    os.utime(stale_again, (old, old))
    with pytest.raises(SourceResourceError, match="symlink"):
        store.reserve_capacity(32)
    assert symlink.is_symlink()
    assert stale_again.exists()
    assert outside.read_bytes() == b"do-not-follow"


def test_remote_adapter_does_not_open_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact_root = tmp_path / "artifacts"
    incoming_root = tmp_path / "incoming"
    artifact_root.mkdir()
    incoming_root.mkdir()

    def requester(action: str, url: str, body: bytes, headers: dict[str, str]):
        assert action == "reserve"
        payload = json.loads(body)
        return 200, {
            "schema": "postersplus.source_art",
            "version": 1,
            "reservation_token": "reservation-token",
            "expires_at": "2026-09-14T12:05:00+00:00",
            "reserved_bytes": payload["required_bytes"],
        }

    def forbidden(*args, **kwargs):
        raise AssertionError("Oracle remote source store must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    store = RemoteSourceArtStore(
        "http://127.0.0.1:18087",
        "test-secret",
        artifact_root,
        incoming_root,
        require_mounts=False,
        requester=requester,
    )
    assert store.ledger_path is None
    assert store.reserve_capacity(32) == "reservation-token"


def test_owner_auth_rejects_tamper_replay_and_wrong_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_registry.config, "POSTERSPLUS_SOURCE_REGISTRY_MODE", "owner")
    monkeypatch.setattr(source_registry.config, "POSTERSPLUS_SOURCE_STORE_MODE", "local")
    monkeypatch.setattr(source_registry.config, "POSTERSPLUS_SOURCE_REGISTRY_SECRET", "test-secret")
    monkeypatch.setattr(source_registry.config, "SOURCE_ART_CACHE_DIR", str(tmp_path / "source"))
    monkeypatch.setattr(source_registry.config, "SOURCE_ART_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setattr(source_registry.config, "POSTERSPLUS_SOURCE_INCOMING_DIR", str(tmp_path / "incoming"))
    monkeypatch.setattr(source_registry.config, "POSTERSPLUS_SOURCE_REGISTRY_NONCE_DB_PATH", str(tmp_path / "nonces.sqlite"))
    monkeypatch.setattr(source_registry.config, "POSTERSPLUS_SOURCE_REQUIRE_MOUNTS", False)
    monkeypatch.setattr(source_registry, "_runtime", source_registry.SourceOwnerRuntime())
    (tmp_path / "source").mkdir()
    (tmp_path / "incoming").mkdir()

    payload = {
        "schema": "postersplus.source_art",
        "version": 1,
        "required_bytes": 32,
        "ttl_seconds": 300,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path = "/v2/source-art/reserve"
    request_id = uuid4()

    def headers_for(
        signed_body: bytes = body,
        signed_path: str = path,
        signed_request_id: UUID = request_id,
    ):
        headers = build_auth_headers(
            method="POST",
            path=signed_path,
            body=signed_body,
            request_id=signed_request_id,
            timestamp=int(time.time()),
            secret=b"test-secret",
            caller="oracle-core",
            audience="postersplus-source-owner",
        )
        headers["Content-Type"] = "application/json"
        return headers

    with TestClient(source_registry.app) as client:
        first = client.post(path, content=body, headers=headers_for())
        assert first.status_code == 200
        replay = client.post(path, content=body, headers=headers_for())
        assert replay.status_code == 403

        tampered = client.post(
            path,
            content=body.replace(b"32", b"33"),
            headers=headers_for(signed_request_id=uuid4()),
        )
        assert tampered.status_code == 403

        wrong_path = client.post(
            "/v2/source-art/lookup",
            content=body,
            headers=headers_for(signed_path=path, signed_request_id=uuid4()),
        )
        assert wrong_path.status_code == 403


def test_owner_surface_has_no_enrich_or_render_routes() -> None:
    assert {
        route.path for route in source_registry.owner_router.routes
    } == {
        "/v2/source-art/reserve",
        "/v2/source-art/release",
        "/v2/source-art/lookup",
        "/v2/source-art/register",
        "/v2/source-art/verification-lookup",
        "/v2/source-art/verification-register",
    }


def test_renderer_revision_excludes_freshness_only_changes() -> None:
    import v2_render

    assert not any(
        "_validate_freshness" in entry
        for entry in v2_render.RENDERER_REVISION_MANIFEST
    )
    baseline = subprocess.run(
        ["git", "show", "HEAD:v2_render.py"],
        cwd=Path(__file__).parents[1],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert v2_render._compute_renderer_revision(
        source_overrides={"v2_render.py": baseline}
    ) == v2_render.RENDERER_REVISION


def test_invalid_source_mode_fails_closed_without_env_preserving_default() -> None:
    env = os.environ.copy()
    env.pop("POSTERSPLUS_SOURCE_REGISTRY_MODE", None)
    env.pop("POSTERSPLUS_SOURCE_REQUIRE_MOUNTS", None)
    env.pop("POSTERSPLUS_SOURCE_ACCOUNT_INCOMING", None)
    env["POSTERSPLUS_SOURCE_STORE_MODE"] = "remtoe"
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "POSTERSPLUS_SOURCE_STORE_MODE" in result.stderr

    env.pop("POSTERSPLUS_SOURCE_STORE_MODE", None)
    defaulted = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.POSTERSPLUS_SOURCE_STORE_MODE); "
            "print(config.POSTERSPLUS_SOURCE_ACCOUNT_INCOMING)",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert defaulted.stdout.splitlines() == ["local", "False"]

    env["POSTERSPLUS_SOURCE_ACCOUNT_INCOMING"] = "true"
    opted_in = subprocess.run(
        [sys.executable, "-c", "import config; print(config.POSTERSPLUS_SOURCE_ACCOUNT_INCOMING)"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert opted_in.stdout.strip() == "True"
