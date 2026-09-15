from __future__ import annotations

import asyncio
import hashlib
import io
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

import text_detect
import v2_enrich as enrich_module
from integration_contract import ArtworkLocator
from source_art import SourceArtError, SourceArtStore
from text_detect import (
    OCRMemoEntry,
    _bounded_env_int,
    build_ocr_memo_key,
    ocr_memo_payload,
    ocr_runtime_signature,
    parse_ocr_memo_entry,
    title_context_sha256,
)
from tmdb import V2ArtworkCandidate
from v2_enrich import EnrichmentRuntime, ProviderHooks

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def test_ocr_key_hashes_normalized_ordered_title_context_and_all_variants():
    expected_titles = ["thematrix", "amelie"]
    expected_hash = hashlib.sha256(
        json.dumps(
            expected_titles,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert title_context_sha256(["The Matrix", "the-matrix", "Amélie"]) == expected_hash

    image_sha = "a" * 64
    base = build_ocr_memo_key(
        image_sha,
        "poster",
        ["The Matrix", "the-matrix"],
        model_hash="a" * 64,
        rules="Rules.V1",
        runtime="RapidOCR-3.9",
        arch="ARM64",
    )
    changed_model = build_ocr_memo_key(
        image_sha,
        "poster",
        ["The Matrix", "the-matrix"],
        model_hash="b" * 64,
        rules="Rules.V1",
        runtime="RapidOCR-3.9",
        arch="ARM64",
    )
    changed_rules = build_ocr_memo_key(
        image_sha,
        "poster",
        ["The Matrix", "the-matrix"],
        model_hash="a" * 64,
        rules="Rules.V2",
        runtime="RapidOCR-3.9",
        arch="ARM64",
    )
    changed_arch = build_ocr_memo_key(
        image_sha,
        "poster",
        ["The Matrix", "the-matrix"],
        model_hash="a" * 64,
        rules="Rules.V1",
        runtime="RapidOCR-3.9",
        arch="x86_64",
    )

    assert base.payload()["model"] == "ppocrv5-mobile"
    assert base.payload()["runtime"] == "rapidocr_onnxruntime"
    assert base.payload()["detection_rules"] == "rules.v1"
    assert base.payload()["architecture"] == "arm64"
    assert len(base.runtime_version) <= 80
    assert len(base.signature) == 64
    assert len({base.signature, changed_model.signature, changed_rules.signature, changed_arch.signature}) == 4


def test_ocr_runtime_token_hashes_complete_long_binding_inputs(monkeypatch):
    image_sha = "e" * 64
    long_runtime_a = "runtime-" + ("v" * 160) + "A"
    long_runtime_b = "runtime-" + ("v" * 160) + "B"
    long_rules_a = "rules-" + ("q" * 160) + "A"
    long_rules_b = "rules-" + ("q" * 160) + "B"
    model_a = "a" * 64
    model_b = ("a" * 63) + "b"

    base = build_ocr_memo_key(
        image_sha,
        "poster",
        "Example",
        model_hash=model_a,
        rules=long_rules_a,
        runtime=long_runtime_a,
        arch="x86_64",
    )
    changed_model = build_ocr_memo_key(
        image_sha,
        "poster",
        "Example",
        model_hash=model_b,
        rules=long_rules_a,
        runtime=long_runtime_a,
        arch="x86_64",
    )
    changed_rules = build_ocr_memo_key(
        image_sha,
        "poster",
        "Example",
        model_hash=model_a,
        rules=long_rules_b,
        runtime=long_runtime_a,
        arch="x86_64",
    )
    changed_runtime = build_ocr_memo_key(
        image_sha,
        "poster",
        "Example",
        model_hash=model_a,
        rules=long_rules_a,
        runtime=long_runtime_b,
        arch="x86_64",
    )

    assert len(base.runtime_version) == 67
    assert base.runtime_version.startswith("v1-")
    assert base.detection_rules == changed_rules.detection_rules
    assert len(
        {
            base.runtime_version,
            changed_model.runtime_version,
            changed_rules.runtime_version,
            changed_runtime.runtime_version,
        }
    ) == 4

    # Runtime diagnostics are intentionally separate from the bounded wire
    # token; long package version values must still affect that token.
    monkeypatch.setattr(text_detect, "_package_version", lambda _name: "x" * 240)
    first = text_detect.ocr_runtime_version()
    monkeypatch.setattr(text_detect, "_package_version", lambda _name: "y" * 240)
    second = text_detect.ocr_runtime_version()
    assert len(first) == len(second) == 67
    assert first != second

    monkeypatch.setattr(text_detect, "DETECT_RES_SIG", "sig-" + ("z" * 160) + "A")
    detect_a = text_detect.ocr_runtime_version()
    monkeypatch.setattr(text_detect, "DETECT_RES_SIG", "sig-" + ("z" * 160) + "B")
    detect_b = text_detect.ocr_runtime_version()
    assert detect_a != detect_b


def test_unknown_memo_is_a_cached_authoritative_decision_not_a_miss():
    key = build_ocr_memo_key("b" * 64, "backdrop", "Example")
    payload = {
        "found": True,
        "memo": {
            "signature": key.signature,
            "key": key.payload(),
            "result": "unknown",
            "verified_at": NOW.isoformat(),
            "source_sha256": key.source_sha256,
        },
    }
    entry = parse_ocr_memo_entry(payload, expected_key=key)
    assert entry == OCRMemoEntry(key.signature, "unknown", NOW)
    assert entry is not None
    assert entry.value is None
    assert parse_ocr_memo_entry({"found": False}, expected_key=key) is None


def test_memo_parser_rejects_wrong_nested_key_or_source():
    key = build_ocr_memo_key("c" * 64, "poster", "Example")
    wrong_key = build_ocr_memo_key("d" * 64, "poster", "Example")
    with pytest.raises(ValueError, match="key mismatch"):
        parse_ocr_memo_entry(
            {
                "signature": key.signature,
                "key": wrong_key.payload(),
                "result": "textless",
            },
            expected_key=key,
        )
    with pytest.raises(ValueError, match="source mismatch"):
        parse_ocr_memo_entry(
            {
                "signature": key.signature,
                "result": "textless",
                "source_sha256": wrong_key.source_sha256,
            },
            expected_key=key,
        )


def test_local_source_owner_memo_round_trip_preserves_unknown(tmp_path):
    store = SourceArtStore(tmp_path / "source-art", tmp_path / "source-art.sqlite")
    payload = b"deterministic-normalized-poster"
    locator = ArtworkLocator(
        provider="tmdb",
        url="https://image.tmdb.org/t/p/w500/poster.jpg",
    )
    derivative = store.install(
        kind="poster",
        recipe_version=1,
        payload=payload,
        mime="image/jpeg",
        width=500,
        height=750,
        locator=locator,
        now=NOW,
        pinned=False,
        reconstructable=True,
    )
    key = build_ocr_memo_key(derivative.sha256, "poster", "Example")
    registered = store.register_verification(
        key.payload(),
        result="unknown",
        verified_at=NOW,
    )
    assert registered.result == "unknown"
    hit = parse_ocr_memo_entry(store.lookup_verification(key.payload()), expected_key=key)
    assert hit is not None
    assert hit.decision == "unknown"
    assert hit.value is None
    assert ocr_memo_payload(key, False)["result"] == "textless"


@pytest.mark.parametrize("scan_result", [True, None])
def test_default_materializer_caches_text_and_unknown_without_rescanning(
    tmp_path,
    monkeypatch,
    scan_result,
):
    store = SourceArtStore(tmp_path / "source-art", tmp_path / "source-art.sqlite")
    payload = io.BytesIO()
    Image.new("RGB", (500, 750), (10, 20, 30)).save(payload, format="JPEG")
    locator = ArtworkLocator(
        provider="tmdb",
        url="https://image.tmdb.org/t/p/w500/poster.jpg",
    )
    derivative = store.install(
        kind="poster",
        recipe_version=1,
        payload=payload.getvalue(),
        mime="image/jpeg",
        width=500,
        height=750,
        locator=locator,
        now=NOW,
        pinned=False,
        reconstructable=True,
    )
    candidate = V2ArtworkCandidate("poster", locator, "neutral")
    runtime = EnrichmentRuntime(
        client=None,
        pool=None,
        tmdb_key="",
        mdblist_key="",
        stateless_metadata=True,
        hooks=ProviderHooks.all(lambda *_args, **_kwargs: None),
        source_store=store,
        require_ocr=True,
        ocr_titles=("Example",),
        art_role="textless_poster",
        art_policy_key="fallback.textless",
    )
    scans = 0

    async def fixture_derivative(*_args, **_kwargs):
        return derivative

    def scan(*_args, **_kwargs):
        nonlocal scans
        scans += 1
        return scan_result

    monkeypatch.setattr(enrich_module, "fetch_derivative", fixture_derivative)
    monkeypatch.setattr(text_detect, "poster_has_burned_in_text", scan)
    for _ in range(2):
        with pytest.raises(SourceArtError):
            asyncio.run(enrich_module._default_materialize_art(candidate, NOW, runtime))
    assert scans == 1


def test_detection_threads_env_reader_is_bounded_without_import_failure(monkeypatch):
    monkeypatch.setenv("TEXTLESS_DETECTION_THREADS", "99")
    assert _bounded_env_int("TEXTLESS_DETECTION_THREADS", 2, minimum=1, maximum=4) == 4
    monkeypatch.setenv("TEXTLESS_DETECTION_THREADS", "-9")
    assert _bounded_env_int("TEXTLESS_DETECTION_THREADS", 2, minimum=1, maximum=4) == 1
    monkeypatch.setenv("TEXTLESS_DETECTION_THREADS", "not-an-int")
    assert _bounded_env_int("TEXTLESS_DETECTION_THREADS", 2, minimum=1, maximum=4) == 2
    assert len(ocr_runtime_signature()) <= 80
