from __future__ import annotations

import asyncio
import hashlib
import io
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from pydantic import ValidationError

from integration_contract import (
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    ArtworkLocator,
    EnrichmentRequest,
    FactProvenance,
    NormalizedFactsEnvelope,
    ProviderRating,
    SourceArtReference,
)
from source_art import SourceArtError, SourceArtStore, normalize_and_store
from tmdb import V2ArtworkCandidate, V2TMDBMetadata
from v2_enrich import EnrichmentRuntime, ProviderHooks, enrich

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
PRESETS = ("clean-notch@4", "prestige@3", "minimalist@4")


def _locator(path: str) -> ArtworkLocator:
    return ArtworkLocator(
        provider="tmdb",
        url=f"https://image.tmdb.org/t/p/original/{path}",
    )


def _candidate(kind: str, path: str, locale: str = "neutral", score: float = 0.0):
    return V2ArtworkCandidate(
        kind=kind,
        locator=_locator(path),
        locale=locale,
        vote_average=score,
        vote_count=int(score * 10),
    )


def _facts() -> NormalizedFactsEnvelope:
    return NormalizedFactsEnvelope(
        values={"genre": "Sci-Fi"},
        provenance=(
            FactProvenance(
                fields=("genre",),
                source="bingecat",
                observed_at=NOW - timedelta(days=30),
                checked_at=NOW - timedelta(days=29),
                expires_at=NOW - timedelta(days=1),
            ),
        ),
    )


def _request(
    *,
    known_facts: NormalizedFactsEnvelope | None = None,
    known_ratings: tuple[ProviderRating, ...] = (),
    known_source_art: tuple[SourceArtReference, ...] = (),
) -> EnrichmentRequest:
    return EnrichmentRequest.model_validate(
        {
            "schema": CONTRACT_SCHEMA,
            "version": CONTRACT_VERSION,
            "media": {"media_type": "movie", "tmdb_id": 11, "imdb_id": None},
            "locales": ["en"],
            "titles_by_locale": {"en": "The Matrix"},
            "preset_refs": list(PRESETS),
            "artwork_only": True,
            "known_facts": (known_facts or NormalizedFactsEnvelope()).model_dump(mode="json"),
            "known_ratings": [item.model_dump(mode="json") for item in known_ratings],
            "known_source_art": [item.model_dump(mode="json") for item in known_source_art],
        }
    )


def _old_poster() -> SourceArtReference:
    digest = "a" * 64
    return SourceArtReference(
        source_art_id="old-textless-poster",
        kind="poster",
        role="textless_poster",
        policy_key="fallback.textless",
        sha256=digest,
        byte_size=1234,
        mime="image/jpeg",
        recipe_version=1,
        locator=_locator("old.jpg"),
        locale="neutral",
        reconstructable=True,
        observed_at=NOW - timedelta(days=2),
        checked_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=10),
        textless_verified=True,
        verification_recipe="ppocr.textless.v1",
        verified_at=NOW - timedelta(days=2),
        verification_source_digest=digest,
    )


def _old_primary_poster() -> SourceArtReference:
    """A usable non-verified portrait that a verified candidate may upgrade."""

    return _old_poster().model_copy(
        update={
            "role": "primary",
            "policy_key": "original.primary",
            "textless_verified": None,
            "verification_recipe": None,
            "verified_at": None,
            "verification_source_digest": None,
        }
    )


def _old_fallback_backdrop() -> SourceArtReference:
    digest = "b" * 64
    return SourceArtReference(
        source_art_id="old-fallback-backdrop",
        kind="backdrop",
        role="fallback_backdrop",
        policy_key="fallback.backdrop",
        sha256=digest,
        byte_size=1234,
        mime="image/jpeg",
        recipe_version=5,
        locator=_locator("old-backdrop.jpg"),
        locale="neutral",
        reconstructable=False,
        observed_at=NOW - timedelta(days=30),
        checked_at=NOW - timedelta(days=29),
        expires_at=NOW - timedelta(days=1),
    )


def _old_logo() -> SourceArtReference:
    digest = "c" * 64
    return SourceArtReference(
        source_art_id="old-logo",
        kind="logo",
        role="logo",
        policy_key="logo.native_original.en",
        sha256=digest,
        byte_size=1234,
        mime="image/png",
        recipe_version=1,
        locator=_locator("old-logo.png"),
        locale="en",
        reconstructable=False,
        observed_at=NOW - timedelta(days=30),
        checked_at=NOW - timedelta(days=29),
        expires_at=NOW - timedelta(days=1),
    )


def _old_rating(*, expired: bool) -> ProviderRating:
    return ProviderRating(
        provider="imdb",
        score=8.0,
        scale=10.0,
        normalized_score=80.0,
        vote_count=123,
        source="bingecat",
        observed_at=NOW - timedelta(days=2),
        checked_at=NOW - timedelta(days=1),
        expires_at=NOW - timedelta(hours=1) if expired else NOW + timedelta(days=7),
    )


def _hooks(
    calls: Counter,
    candidates: tuple[V2ArtworkCandidate, ...],
    *,
    fail_kinds: frozenset[str] = frozenset(),
):
    async def forbidden(name: str, *_args, **_kwargs):
        calls[name] += 1
        raise AssertionError(f"unexpected provider call: {name}")

    async def fetch_tmdb(*_args, **kwargs):
        calls["tmdb"] += 1
        assert kwargs == {
            "need_images": True,
            "need_credits": False,
            "need_external_ids": False,
            "need_original_assets": False,
            "cache_mode": "off",
        }
        return V2TMDBMetadata(
            tmdb_id="11",
            media_type="movie",
            title="The Matrix",
            original_title="The Matrix",
            original_language="en",
            candidates=candidates,
        )

    async def materialize(candidate, evaluated_at, runtime):
        calls["materialize"] += 1
        if candidate.kind in fail_kinds:
            raise SourceArtError(f"fixture {candidate.kind} failed")
        digest = hashlib.sha256(candidate.locator.url.encode()).hexdigest()
        textless = runtime.art_role == "textless_poster"
        return SourceArtReference(
            source_art_id=f"fixture-{candidate.kind}-{digest[:12]}",
            kind=candidate.kind,
            role=runtime.art_role,
            policy_key=runtime.art_policy_key,
            sha256=digest,
            byte_size=1234,
            mime="image/png" if candidate.kind == "logo" else "image/jpeg",
            recipe_version={"poster": 1, "backdrop": 5, "logo": 1}[candidate.kind],
            locator=candidate.locator,
            locale=candidate.locale,
            reconstructable=True,
            observed_at=evaluated_at,
            checked_at=evaluated_at,
            expires_at=evaluated_at + timedelta(days=30),
            textless_verified=True if textless else None,
            verification_recipe="ppocr.textless.v1" if textless else None,
            verified_at=evaluated_at if textless else None,
            verification_source_digest=digest if textless else None,
        )

    return ProviderHooks(
        resolve_identity=lambda *args, **kwargs: forbidden("identity", *args, **kwargs),
        fetch_ratings=lambda *args, **kwargs: forbidden("ratings", *args, **kwargs),
        fetch_tmdb=fetch_tmdb,
        fetch_trending=lambda *args, **kwargs: forbidden("trending", *args, **kwargs),
        fetch_release=lambda *args, **kwargs: forbidden("release", *args, **kwargs),
        fetch_tvdb=lambda *args, **kwargs: forbidden("tvdb", *args, **kwargs),
        materialize_art=materialize,
    )


def _runtime(hooks: ProviderHooks, source_store: SourceArtStore | None = None) -> EnrichmentRuntime:
    return EnrichmentRuntime(
        client=None,
        pool=None,
        tmdb_key="tmdb-key",
        mdblist_key="mdblist-key",
        stateless_metadata=True,
        hooks=hooks,
        source_store=source_store,
    )


def test_artwork_only_contract_is_strict_optional_and_serializes_false():
    base = {
        "schema": CONTRACT_SCHEMA,
        "version": CONTRACT_VERSION,
        "media": {"media_type": "movie", "tmdb_id": 11},
        "locales": ["en"],
        "titles_by_locale": {"en": "The Matrix"},
        "preset_refs": ["minimalist@4"],
    }
    default = EnrichmentRequest.model_validate(base)
    assert default.artwork_only is False
    assert default.model_dump(mode="json")["artwork_only"] is False
    assert EnrichmentRequest.model_validate({**base, "artwork_only": True}).artwork_only is True
    with pytest.raises(ValidationError):
        EnrichmentRequest.model_validate({**base, "artwork_only": None})
    with pytest.raises(ValidationError):
        EnrichmentRequest.model_validate({**base, "artwork_only": 1})


def test_artwork_only_reuses_facts_and_makes_one_image_request_for_all_fixed_presets():
    candidates = (
        _candidate("poster", "poster-en.jpg", "en", 10),
        _candidate("poster", "poster-neutral-a.jpg", "neutral", 8),
        _candidate("poster", "poster-neutral-b.jpg", "neutral", 7),
        _candidate("backdrop", "backdrop-a.jpg", "neutral", 3),
        _candidate("backdrop", "backdrop-b.jpg", "neutral", 2),
        _candidate("logo", "logo-en.png", "en", 1),
    )
    calls = Counter()
    known_facts = _facts()
    result = asyncio.run(
        enrich(
            _request(known_facts=known_facts),
            NOW,
            runtime=_runtime(_hooks(calls, candidates)),
        )
    )

    assert calls == Counter({"tmdb": 1, "materialize": 3})
    assert result.facts == known_facts
    assert result.media.model_dump(mode="json") == {
        "media_type": "movie",
        "tmdb_id": 11,
        "imdb_id": None,
    }
    assert {item.role for item in result.source_art} == {
        "textless_poster",
        "fallback_backdrop",
        "logo",
    }
    assert not any(item.provider in {"mdblist", "tvdb", "tmdb_trending", "tmdb_release"}
                   for item in result.provider_statuses)


def test_artwork_only_preserves_expired_captured_ratings_exactly():
    captured = (_old_rating(expired=True),)
    candidates = (
        _candidate("poster", "poster.jpg", "neutral", 0),
        _candidate("backdrop", "backdrop.jpg", "neutral", 0),
        _candidate("logo", "logo.png", "en", 0),
    )
    calls = Counter()
    result = asyncio.run(
        enrich(
            _request(known_ratings=captured),
            NOW,
            runtime=_runtime(_hooks(calls, candidates)),
        )
    )
    assert result.ratings == captured


def test_artwork_only_keeps_usable_old_poster_when_zero_score_candidates_do_not_improve():
    candidates = (
        _candidate("poster", "new-a.jpg", "neutral", 0),
        _candidate("poster", "new-b.jpg", "neutral", 0),
        _candidate("poster", "new-c.jpg", "neutral", 0),
        _candidate("backdrop", "backdrop-a.jpg", "neutral", 1),
        _candidate("backdrop", "backdrop-b.jpg", "neutral", 0),
        _candidate("logo", "logo-en.png", "en", 1),
    )
    calls = Counter()
    old = _old_poster()
    result = asyncio.run(
        enrich(
            _request(known_source_art=(old,)),
            NOW,
            runtime=_runtime(_hooks(calls, candidates)),
        )
    )

    assert calls == Counter({"tmdb": 1, "materialize": 2})
    assert old in result.source_art
    assert not any(item.locator and item.locator.url.endswith("new-a.jpg") for item in result.source_art)


def test_artwork_only_does_not_widen_missing_poster_to_tvdb_when_old_fallback_art_is_usable():
    candidates = (
        _candidate("poster", "poster-0.jpg", "neutral", 0),
        _candidate("poster", "poster-1.jpg", "neutral", 0),
        _candidate("poster", "poster-2.jpg", "neutral", 0),
        _candidate("backdrop", "backdrop-0.jpg", "neutral", 8),
        _candidate("backdrop", "backdrop-1.jpg", "neutral", 7),
        _candidate("logo", "logo.png", "en", 8),
    )
    calls = Counter()
    old_backdrop = _old_fallback_backdrop()
    old_logo = _old_logo()
    result = asyncio.run(
        enrich(
            _request(known_source_art=(old_backdrop, old_logo)),
            NOW,
            runtime=_runtime(
                _hooks(calls, candidates, fail_kinds=frozenset({"poster"}))
            ),
        )
    )

    assert old_backdrop in result.source_art
    assert old_logo in result.source_art
    assert calls == Counter({"tmdb": 1, "materialize": 3})
    assert any(
        status.provider == "source_art"
        and status.status == "missing"
        and status.missing_fields == ("poster",)
        for status in result.provider_statuses
    )


def test_artwork_only_upgrades_old_primary_poster_to_verified_portrait():
    candidates = (
        _candidate("poster", "new-a.jpg", "neutral", 8),
        _candidate("poster", "new-b.jpg", "neutral", 1),
        _candidate("poster", "new-c.jpg", "neutral", 0),
        _candidate("backdrop", "backdrop-a.jpg", "neutral", 1),
        _candidate("backdrop", "backdrop-b.jpg", "neutral", 0),
        _candidate("logo", "logo-en.png", "en", 1),
    )
    calls = Counter()
    old = _old_primary_poster()
    result = asyncio.run(
        enrich(
            _request(known_source_art=(old,)),
            NOW,
            runtime=_runtime(_hooks(calls, candidates)),
        )
    )

    assert calls == Counter({"tmdb": 1, "materialize": 3})
    assert old in result.source_art
    assert any(item.locator and item.locator.url.endswith("new-a.jpg") for item in result.source_art)


def test_artwork_only_does_not_churn_verified_portrait_at_equal_locale():
    candidates = (
        _candidate("poster", "new-a.jpg", "neutral", 8),
        _candidate("poster", "new-b.jpg", "neutral", 1),
        _candidate("poster", "new-c.jpg", "neutral", 0),
        _candidate("backdrop", "backdrop-a.jpg", "neutral", 1),
        _candidate("logo", "logo-en.png", "en", 1),
    )
    calls = Counter()
    old = _old_poster()
    result = asyncio.run(
        enrich(
            _request(known_source_art=(old,)),
            NOW,
            runtime=_runtime(_hooks(calls, candidates)),
        )
    )

    assert calls == Counter({"tmdb": 1, "materialize": 2})
    assert old in result.source_art
    assert not any(item.locator and item.locator.url.endswith("new-a.jpg") for item in result.source_art)


def test_artwork_only_keeps_stale_but_intact_art_and_does_not_refresh_verification(tmp_path):
    store = SourceArtStore(tmp_path / "source-art", tmp_path / "source-art.sqlite")

    def normalized(kind: str, size: tuple[int, int], color: tuple[int, int, int], path: str):
        raw = io.BytesIO()
        Image.new("RGB", size, color).save(raw, format="JPEG", quality=90)
        return normalize_and_store(
            kind,
            io.BytesIO(raw.getvalue()),
            {"poster": 1, "backdrop": 5}[kind],
            store=store,
            locator=_locator(path),
            now=NOW - timedelta(days=10),
            reconstructable=False,
        )

    poster = normalized("poster", (900, 1350), (10, 20, 30), "old-poster.jpg")
    backdrop = normalized("backdrop", (1600, 900), (30, 40, 50), "old-backdrop.jpg")
    old_verified_at = NOW - timedelta(days=8)
    old_checked_at = NOW - timedelta(days=7)
    old_expires_at = NOW - timedelta(days=1)
    old_poster = SourceArtReference(
        source_art_id=poster.source_art_id,
        kind="poster",
        role="textless_poster",
        policy_key="fallback.textless",
        sha256=poster.sha256,
        byte_size=poster.byte_size,
        mime=poster.mime,
        recipe_version=poster.recipe_version,
        locator=poster.locator,
        locale="pt",
        reconstructable=False,
        observed_at=NOW - timedelta(days=9),
        checked_at=old_checked_at,
        expires_at=old_expires_at,
        textless_verified=True,
        verification_recipe="ppocr.textless.v1",
        verified_at=old_verified_at,
        verification_source_digest=poster.sha256,
    )
    old_backdrop = SourceArtReference(
        source_art_id=backdrop.source_art_id,
        kind="backdrop",
        role="fallback_backdrop",
        policy_key="fallback.backdrop",
        sha256=backdrop.sha256,
        byte_size=backdrop.byte_size,
        mime=backdrop.mime,
        recipe_version=backdrop.recipe_version,
        locator=backdrop.locator,
        locale="neutral",
        reconstructable=False,
        observed_at=NOW - timedelta(days=9),
        checked_at=old_checked_at,
        expires_at=old_expires_at,
    )
    candidates = (
        _candidate("poster", "new-poster-0.jpg", "pt", 0),
        _candidate("poster", "new-poster-1.jpg", "pt", 0),
        _candidate("poster", "new-poster-2.jpg", "pt", 0),
        _candidate("backdrop", "new-backdrop.jpg", "neutral", 0),
        _candidate("backdrop", "new-backdrop-2.jpg", "neutral", 0),
        _candidate("logo", "logo.png", "en", 0),
    )
    calls = Counter()
    result = asyncio.run(
        enrich(
            _request(known_source_art=(old_poster, old_backdrop)),
            NOW,
            runtime=_runtime(_hooks(calls, candidates), source_store=store),
        )
    )

    assert old_poster in result.source_art
    assert old_backdrop in result.source_art
    assert calls == Counter({"tmdb": 1, "materialize": 1})
