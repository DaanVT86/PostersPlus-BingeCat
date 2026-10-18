"""Landscape (16:9) renderer: portrait identity guard, revision, envelope,
contract isolation and offline goldens through the real enrich + render path."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import source_art
import v2_enrich as enrich_module
import v2_render as render
from integration_contract import (
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    EnrichmentRequest,
    EnrichmentResult,
    FactProvenance,
    MediaIdentity,
    NormalizedFacts,
    NormalizedFactsEnvelope,
    ProviderRating,
    RenderInputBundle,
)
from landscape_spec import (
    LandscapeEnrichmentRequest,
    LandscapeEnrichmentResult,
    LandscapeRenderInputBundle,
    LandscapeRenderSnapshot,
    canonicalize_landscape_config,
)
from render_spec import canonicalize_config
from service_auth import MemoryNonceStore, build_auth_headers
from source_art import DownloadedSource, SourceArtStore
from tmdb import V2ArtworkCandidate, V2TMDBMetadata
from integration_contract import ArtworkLocator

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
SECRET = b"landscape-route-secret"

# Portrait identity captured at fork base 1431484, before landscape existed.
# The approved production build (baked image, Python 3.11) binds this
# fingerprint to the published portrait revision.
APPROVED_FINGERPRINT = "a7ed1df863f30aab040b1ab6d005cf5918268fea5de6cf7ccb7b0652d099a9e5"
PORTRAIT_RENDERER_REVISION = "260504a9d0d0daa577848316c51518c5de78c4110642bac621cc92d3538f2b29"
APPROVED_POLICY_REVISION = "e0857c57098953ff71207c7173957d03769fba4067cda156dcc08238e35fc5be"
APPROVED_SOURCE_RECIPE_REVISION = "91cac721c050ee45072277ad4afe3e3880243c478fa73638acf18bfe92ef82ca"
# Library-independent portrait code identity (code roots + assets + config),
# also captured at base.  ``ast.dump`` differs per Python minor, hence the map.
PORTRAIT_CODE_IDENTITY = {
    (3, 11): "6def692e5c3b7b88d5f61b5bb32fcd789d0ffbe537b7b30863d93a4bde6a3878",
    (3, 12): "980745e7b8a1367910f4d959d879a27bc6c4874300145df9534bb2ebeb49e0d8",
}
IN_APPROVED_RUNTIME = render.RENDERER_BUILD_FINGERPRINT == APPROVED_FINGERPRINT

# Landscape goldens (content sha256 of the lossless WebP) for the approved runtime.
LANDSCAPE_GOLDENS = {
    "textless": "486783a6ea85e81f0c11df2acc56c0d8798956780996d2b9eb23550a3a37c702",
    "original": "c270e4c8dd088aa273cf6ca5de15d9b293e60018cd49bd1e27bdcfc429cdc315",
    "no_art": "e79804f1d96160a0e02e1147cfdd2c49bb15d9b63c1b01ab8eb81d777f6aaed5",
    "fallback_to_original": "a104eed22f8ceff6e51b7d8405dbac09839040594b92842892fc48a972e63855",
}


# --- portrait identity ---------------------------------------------------------


def test_portrait_renderer_revision_is_byte_identical(monkeypatch):
    assert render._RENDERER_COMPATIBILITY == {APPROVED_FINGERPRINT: PORTRAIT_RENDERER_REVISION}
    if IN_APPROVED_RUNTIME:
        assert render.RENDERER_REVISION == PORTRAIT_RENDERER_REVISION
        assert render.RENDER_POLICY_REVISION == APPROVED_POLICY_REVISION
        assert render.SOURCE_RECIPE_REVISION == APPROVED_SOURCE_RECIPE_REVISION

    expected = PORTRAIT_CODE_IDENTITY.get(sys.version_info[:2])
    if expected is None:
        pytest.skip("no pinned portrait code identity for this Python version")
    monkeypatch.setattr(render, "_image_library_versions", lambda: {"pinned": "environment-independent"})
    assert render._compute_renderer_build_fingerprint() == expected


def test_landscape_code_is_outside_portrait_identity_and_inside_its_own():
    manifest = set(render.RENDERER_REVISION_MANIFEST)
    assert not any("landscape" in entry for entry in manifest)
    base = Path(render.__file__).parent

    source = (base / "v2_render.py").read_text()
    old = "image.size != LANDSCAPE_CANVAS_SIZE"
    assert old in source
    changed = {"v2_render.py": source.replace(old, "image.size == LANDSCAPE_CANVAS_SIZE", 1)}
    assert render._compute_renderer_build_fingerprint(source_overrides=changed) == render.RENDERER_BUILD_FINGERPRINT
    assert render._compute_landscape_renderer_revision(source_overrides=changed) != render.LANDSCAPE_RENDERER_REVISION

    drawing = (base / "landscape.py").read_text()
    assert "_BAND_ALPHA      = 212" in drawing
    changed = {"landscape.py": drawing.replace("_BAND_ALPHA      = 212", "_BAND_ALPHA      = 213", 1)}
    assert render._compute_landscape_renderer_revision(source_overrides=changed) != render.LANDSCAPE_RENDERER_REVISION

    revision = render.LANDSCAPE_RENDERER_REVISION
    assert len(revision) == 64 and revision == revision.lower()
    assert revision != render.RENDERER_REVISION


def test_startup_guard_accepts_both_revisions_and_rejects_landscape_drift():
    base = Path(render.__file__).parent

    def load(**expected):
        env = {**os.environ, **expected}
        return subprocess.run(
            [sys.executable, "-c", "import v2_render"],
            cwd=base, env=env, capture_output=True, text=True,
        )

    both = load(
        POSTERSPLUS_EXPECTED_RENDERER_REVISION=render.RENDERER_REVISION,
        POSTERSPLUS_EXPECTED_LANDSCAPE_RENDERER_REVISION=render.LANDSCAPE_RENDERER_REVISION,
    )
    assert both.returncode == 0, both.stderr
    drift = load(
        POSTERSPLUS_EXPECTED_RENDERER_REVISION=render.RENDERER_REVISION,
        POSTERSPLUS_EXPECTED_LANDSCAPE_RENDERER_REVISION="0" * 64,
    )
    assert drift.returncode != 0
    assert "landscape renderer revision mismatch" in drift.stderr



# --- config and envelope -----------------------------------------------------------


def _portrait_config(**changes) -> dict:
    raw = {
        "rating_display_mode": 0,
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": "trending",
        "badge_display_mode": 0,
        "logo_language": "en",
        "movie_weights": "imdb:1.0",
    }
    raw.update(changes)
    return json.loads(canonicalize_config(raw).canonical_json())


def _landscape_config(art="textless", badge="top_left", **changes) -> dict:
    return {
        **_portrait_config(**changes),
        "shape": "landscape",
        "landscape_art": art,
        "landscape_badge_pos": badge,
    }


def test_landscape_config_round_trips_and_never_shares_the_portrait_hash():
    portrait = _portrait_config()
    raw = _landscape_config()
    spec = canonicalize_landscape_config(raw)
    assert spec.canonical_config() == raw
    assert spec.sha256() == hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    assert spec.sha256() != canonicalize_config(portrait).sha256()
    assert canonicalize_landscape_config(_landscape_config(art="original")).sha256() != spec.sha256()
    assert canonicalize_landscape_config(_landscape_config(badge="logo")).sha256() != spec.sha256()
    for bad in ({"landscape_art": "poster"}, {"landscape_badge_pos": "middle"}, {"shape": "portrait"}):
        with pytest.raises(ValueError):
            canonicalize_landscape_config({**raw, **bad})


def test_portrait_render_rejects_a_landscape_config():
    raw = _landscape_config()
    bundle = RenderInputBundle(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        media=MediaIdentity(media_type="movie", tmdb_id=11),
        locale="en",
        canonical_config=raw,
        config_sha256=canonicalize_landscape_config(raw).sha256(),
        snapshot_sha256="0" * 64,
        snapshot={"evaluated_at": NOW, "titles_by_locale": {"en": "Example"}},
    )
    with pytest.raises(render.RenderInputError, match="canonical_config_required"):
        render.render(bundle)


def _signed_headers(method: str, path: str, body: bytes):
    return build_auth_headers(
        method=method, path=path, body=body, request_id=uuid4(),
        timestamp=int(datetime.now(timezone.utc).timestamp()),
        secret=SECRET, caller="bingecat", audience="postersplus",
    )


def _client(monkeypatch, store=None):
    import main

    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", SECRET.decode())
    monkeypatch.setattr(main, "_V2_NONCE_STORE", MemoryNonceStore())
    if store is not None:
        monkeypatch.setattr(main, "_V2_SOURCE_STORE", store)
    return TestClient(main.app)


def test_landscape_catalog_lists_every_preset_and_portrait_catalog_is_unchanged(monkeypatch):
    client = _client(monkeypatch)
    portrait = client.get("/v2/presets", headers=_signed_headers("GET", "/v2/presets", b"")).json()
    assert set(portrait) == {"schema", "version", "renderer_revision", "presets"}
    assert portrait["renderer_revision"] == render.RENDERER_REVISION

    response = client.get(
        "/v2/presets/landscape",
        headers=_signed_headers("GET", "/v2/presets/landscape", b""),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["shape"] == "landscape"
    assert payload["renderer_revision"] == render.LANDSCAPE_RENDERER_REVISION
    assert payload["portrait_renderer_revision"] == render.RENDERER_REVISION
    assert [item["ref"] for item in payload["presets"]] == [item["ref"] for item in portrait["presets"]]
    for item, twin in zip(payload["presets"], portrait["presets"]):
        assert item["portrait_config_sha256"] == twin["config_sha256"]
        assert item["canonical_config"] == {
            **twin["canonical_config"],
            "shape": "landscape",
            "landscape_art": "textless",
            "landscape_badge_pos": "top_left",
        }
        spec = canonicalize_landscape_config(item["canonical_config"])
        assert item["config_sha256"] == spec.sha256() != twin["config_sha256"]
        assert item["requirements"]["ratings"] and item["requirements"]["release_year"]
        assert not item["requirements"]["certification"] and not item["requirements"]["ocr"]
        assert len(item["requirements_sha256"]) == 64


# --- offline goldens through enrich + render --------------------------------------


def _backdrop(color, *, title_text=False) -> bytes:
    image = Image.new("RGB", (1600, 900), color)
    draw = ImageDraw.Draw(image)
    for x in range(0, 1600, 80):
        draw.rectangle((x, 0, x + 39, 899), fill=tuple(min(255, c + 40) for c in color))
    draw.ellipse((300, 150, 900, 750), fill=(220, 120, 40))
    if title_text:
        draw.rectangle((80, 640, 700, 820), fill=(250, 250, 250))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _logo() -> bytes:
    image = Image.new("RGBA", (700, 180), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((20, 30, 680, 150), fill=(255, 255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _candidate(kind, locale, name, average=5.0):
    size = {"backdrop": "w1280", "logo": "original"}[kind]
    suffix = "png" if kind == "logo" else "jpg"
    return V2ArtworkCandidate(
        kind=kind,
        locator=ArtworkLocator(provider="tmdb", url=f"https://image.tmdb.org/t/p/{size}/{name}.{suffix}"),
        locale=locale,
        vote_average=average,
        vote_count=10,
    )


def _known_facts():
    values = {"genre": "Sci-Fi", "release_year": 2024, "trending_rank": 3}
    return NormalizedFactsEnvelope(
        values=NormalizedFacts.model_validate(values),
        provenance=(
            FactProvenance(
                fields=tuple(values), source="bingecat",
                observed_at=NOW - timedelta(hours=2), checked_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(days=7),
            ),
        ),
    )


def _rating():
    return ProviderRating(
        provider="imdb", score=8.7, scale=10.0, normalized_score=87.0, vote_count=2_000_000,
        source="mdblist", observed_at=NOW - timedelta(hours=2),
        checked_at=NOW - timedelta(hours=1), expires_at=NOW + timedelta(days=1),
    )


def _enrich(monkeypatch, store, candidates, payloads, configs, *, now=NOW, **request_changes):
    async def downloader(locator, kind, *, temp_dir):
        path = Path(temp_dir) / f"raw-{uuid4().hex[:6]}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[locator.url])
        mime = "image/png" if locator.url.endswith(".png") else "image/jpeg"
        return DownloadedSource(path=path, content_type=mime, locator=locator)

    # Production materializer and normalization; only the network is faked.
    monkeypatch.setattr(
        enrich_module,
        "fetch_derivative",
        functools.partial(source_art.fetch_derivative, downloader=downloader),
    )

    async def fetch_tmdb(*_args, **_kwargs):
        return V2TMDBMetadata(
            tmdb_id="11", media_type="movie", title="Example Title",
            original_title="Example Title", original_language="en",
            candidates=tuple(candidates),
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected provider call")

    hooks = enrich_module.ProviderHooks(
        resolve_identity=forbidden, fetch_ratings=forbidden, fetch_tmdb=fetch_tmdb,
        fetch_trending=forbidden, fetch_release=forbidden,
        fetch_tvdb=lambda *_a, **_k: [],
        materialize_art=enrich_module._default_materialize_art,
    )
    runtime = enrich_module.EnrichmentRuntime(
        client=None, pool=None, tmdb_key="offline", mdblist_key="",
        stateless_metadata=True, hooks=hooks, source_store=store,
    )
    request = LandscapeEnrichmentRequest(
        schema=CONTRACT_SCHEMA, version=CONTRACT_VERSION,
        media=MediaIdentity(media_type="movie", tmdb_id=11, imdb_id="tt0000011"),
        locales=("en",), titles_by_locale={"en": "Example Title"},
        canonical_configs=tuple(configs),
        known_ratings=(_rating(),), known_facts=_known_facts(),
        **request_changes,
    )
    return asyncio.run(enrich_module.enrich(request, now, runtime=runtime))


def _render_landscape(result, raw_config, store):
    spec = canonicalize_landscape_config(raw_config)
    snapshot = LandscapeRenderSnapshot(
        evaluated_at=result.evaluated_at, titles_by_locale=result.titles_by_locale,
        ratings=result.ratings, facts=result.facts,
        source_art=result.source_art, landscape_art=result.landscape_art,
    )
    bundle = LandscapeRenderInputBundle(
        schema=CONTRACT_SCHEMA, version=CONTRACT_VERSION, media=result.media, locale="en",
        canonical_config=raw_config, config_sha256=spec.sha256(),
        snapshot_sha256=render.canonical_landscape_snapshot_sha256(
            snapshot, media=result.media, spec=spec, locale="en"
        ),
        snapshot=snapshot,
    )
    return bundle, render.render_landscape(bundle, source_store=store)


CASES = {
    # case: (landscape_art, badge, backdrops)
    "textless": ("textless", "top_left", (("neutral", "plain", (40, 70, 110)), ("en", "titled", (90, 40, 60)))),
    "original": ("original", "logo", (("neutral", "plain", (40, 70, 110)), ("en", "titled", (90, 40, 60)))),
    "no_art": ("textless", "top_right", ()),
    "fallback_to_original": ("textless", "top_left", (("en", "titled", (90, 40, 60)),)),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_landscape_golden_through_enrich_and_render(monkeypatch, tmp_path, case):
    art, badge, backdrops = CASES[case]
    store = SourceArtStore(tmp_path / "art", tmp_path / "art.sqlite")
    # The no-art case also has no logo, so it exercises the fallback title.
    candidates = [] if case == "no_art" else [_candidate("logo", "en", "logo")]
    payloads = {item.locator.url: _logo() for item in candidates}
    for locale, name, color in backdrops:
        candidate = _candidate("backdrop", locale, name)
        candidates.append(candidate)
        payloads[candidate.locator.url] = _backdrop(color, title_text=locale != "neutral")
    raw = _landscape_config(art=art, badge=badge)

    result = _enrich(monkeypatch, store, candidates, payloads, [raw])
    assert isinstance(result, LandscapeEnrichmentResult)
    # Only the shared logo is portrait-typed art; no poster/backdrop is fetched.
    assert {item.role for item in result.source_art} == (set() if case == "no_art" else {"logo"})
    policies = {item.policy_key for item in result.landscape_art}
    expected_policies = {
        "textless": {"landscape.textless"},
        "original": {"landscape.original"},
        "no_art": set(),
        "fallback_to_original": {"landscape.original"},
    }[case]
    assert policies == expected_policies
    if case == "no_art":
        assert any(s.missing_fields == ("landscape",) for s in result.provider_statuses)

    bundle, (payload, metadata) = _render_landscape(result, raw, store)
    _, (again, _) = _render_landscape(result, raw, store)
    assert payload == again
    assert metadata.renderer_revision == render.LANDSCAPE_RENDERER_REVISION
    assert metadata.content_sha256 == hashlib.sha256(payload).hexdigest()
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.size == (1000, 563)

    projection = render.landscape_snapshot_visual_projection(
        bundle.snapshot, media=bundle.media, spec=canonicalize_landscape_config(raw), locale="en"
    )
    roles = sorted(item["role"] for item in projection["source_art"])
    assert roles == {
        "textless": ["landscape_backdrop", "logo"],
        "original": ["landscape_backdrop"],
        "no_art": [],
        "fallback_to_original": ["landscape_backdrop"],
    }[case]
    assert bool(projection["titles_by_locale"]) == (case == "no_art")
    assert projection["facts"]["values"]["release_year"] == 2024
    assert [item["provider"] for item in projection["ratings"]] == ["imdb"]

    if IN_APPROVED_RUNTIME:
        assert metadata.content_sha256 == LANDSCAPE_GOLDENS[case]


def _backdrops(*specs):
    candidates, payloads = [], {}
    for locale, name in specs:
        candidate = _candidate("backdrop", locale, name)
        candidates.append(candidate)
        color = (40 + len(name) * 9, 70, 110)  # distinct bytes, distinct content ID
        payloads[candidate.locator.url] = _backdrop(color, title_text=locale != "neutral")
    return candidates, payloads


@pytest.mark.parametrize("first,then", [("textless", "original"), ("original", "textless")])
def test_carried_landscape_art_does_not_satisfy_another_requested_policy(
    monkeypatch, tmp_path, first, then
):
    store = SourceArtStore(tmp_path / "art", tmp_path / "art.sqlite")
    candidates, payloads = _backdrops(("neutral", "plain"), ("en", "titled"))
    carried = _enrich(monkeypatch, store, candidates, payloads, [_landscape_config(art=first)])
    assert {item.policy_key for item in carried.landscape_art} == {f"landscape.{first}"}

    result = _enrich(
        monkeypatch, store, candidates, payloads, [_landscape_config(art=then)],
        known_landscape_art=carried.landscape_art,
    )
    assert sorted(item.policy_key for item in result.landscape_art) == sorted(
        [f"landscape.{first}", f"landscape.{then}"]
    )


def test_artwork_only_refreshes_the_requested_policy_over_a_carried_fallback(monkeypatch, tmp_path):
    store = SourceArtStore(tmp_path / "art", tmp_path / "art.sqlite")
    raw = _landscape_config(art="textless")
    candidates, payloads = _backdrops(("en", "titled"))
    fallback = _enrich(monkeypatch, store, candidates, payloads, [raw])
    assert [item.policy_key for item in fallback.landscape_art] == ["landscape.original"]

    # The requested textless art appears later: the artwork-only lane fetches
    # it although the original fallback is still fresh.
    candidates, payloads = _backdrops(("en", "titled"), ("neutral", "plain"))
    refreshed = _enrich(
        monkeypatch, store, candidates, payloads, [raw],
        artwork_only=True, known_landscape_art=fallback.landscape_art,
    )
    by_policy = {item.policy_key: item for item in refreshed.landscape_art}
    assert set(by_policy) == {"landscape.textless", "landscape.original"}
    assert by_policy["landscape.original"] == fallback.landscape_art[0]

    # Once expired, the textless reference is replaced, not duplicated; the
    # expired fallback stays carried because the requested art was found.
    candidates, payloads = _backdrops(("en", "titled"), ("neutral", "refreshed"))
    later = _enrich(
        monkeypatch, store, candidates, payloads, [raw],
        now=NOW + timedelta(days=40),
        artwork_only=True, known_landscape_art=refreshed.landscape_art,
    )
    textless = [item for item in later.landscape_art if item.policy_key == "landscape.textless"]
    assert len(textless) == 1
    assert textless[0].source_art_id != by_policy["landscape.textless"].source_art_id
    assert [item for item in later.landscape_art if item.policy_key == "landscape.original"] == [
        by_policy["landscape.original"]
    ]
    assert not any(s.missing_fields == ("landscape",) for s in later.provider_statuses)


def test_landscape_render_endpoint_and_portrait_enrich_contract(monkeypatch, tmp_path):
    store = SourceArtStore(tmp_path / "art", tmp_path / "art.sqlite")
    candidate = _candidate("backdrop", "neutral", "plain")
    logo = _candidate("logo", "en", "logo")
    payloads = {candidate.locator.url: _backdrop((40, 70, 110)), logo.locator.url: _logo()}
    raw = _landscape_config()
    result = _enrich(monkeypatch, store, [candidate, logo], payloads, [raw, _portrait_config()])
    assert isinstance(result, LandscapeEnrichmentResult)
    bundle, (payload, _metadata) = _render_landscape(result, raw, store)

    client = _client(monkeypatch, store)
    body = bundle.model_dump_json(by_alias=True).encode()
    response = client.post("/v2/render", content=body, headers=_signed_headers("POST", "/v2/render", body))
    assert response.status_code == 200, response.text
    assert response.content == payload
    assert response.headers["x-postersplus-renderer-revision"] == render.LANDSCAPE_RENDERER_REVISION
    assert response.headers["x-postersplus-config-sha256"] == canonicalize_landscape_config(raw).sha256()

    # Portrait-only enrichment keeps the exact portrait result type and JSON.
    portrait = _enrich(monkeypatch, store, [candidate, logo], payloads, [_portrait_config()])
    assert type(portrait) is EnrichmentResult
    assert "landscape_art" not in portrait.model_dump(mode="json")
    # A portrait request body still validates against the portrait model.
    request = EnrichmentRequest(
        schema=CONTRACT_SCHEMA, version=CONTRACT_VERSION,
        media=MediaIdentity(media_type="movie", tmdb_id=11), locales=("en",),
        titles_by_locale={"en": "Example Title"}, canonical_configs=(_portrait_config(),),
    )
    import main

    assert main._v2_landscape_body(request.model_dump_json(by_alias=True).encode()) is False
    assert main._v2_landscape_body(bundle.model_dump_json(by_alias=True).encode()) is True


def test_source_owner_registry_accepts_only_canvas_sized_landscape_derivatives():
    import source_registry

    def payload(size):
        output = io.BytesIO()
        Image.new("RGB", size, (10, 20, 30)).save(output, format="JPEG", quality=92)
        return output.getvalue()

    good = payload((1000, 563))
    source_registry._validate_normalized(
        payload=good, kind="landscape", recipe_version=1,
        expected_digest=hashlib.sha256(good).hexdigest(), expected_size=len(good),
        mime="image/jpeg", width=1000, height=563,
    )
    bad = payload((500, 750))
    with pytest.raises(source_art.SourceArtError):
        source_registry._validate_normalized(
            payload=bad, kind="landscape", recipe_version=1,
            expected_digest=hashlib.sha256(bad).hexdigest(), expected_size=len(bad),
            mime="image/jpeg", width=500, height=750,
        )
