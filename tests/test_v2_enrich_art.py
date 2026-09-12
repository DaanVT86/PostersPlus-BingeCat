from __future__ import annotations

import asyncio
import hashlib
import io
import json
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import source_art as source_art_module
import tvdb as tvdb_module
import v2_enrich as enrich_module
from awards import _RateLimited
from bingecat_resolver import ResolvedIdentity, resolve_v2_identity
from integration_contract import (
    ArtworkLocator,
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    EnrichmentRequest,
    EnrichmentResult,
    ImmutableRenderSnapshot,
    MediaIdentity,
    ProviderRating,
    RenderInputBundle,
    SourceArtReference,
)
from preset_registry import get_preset
from ratings import RatingDetail, RatingFetchDetails, fetch_rating, fetch_rating_details
from render_spec import canonicalize_config
from service_auth import MemoryNonceStore, build_auth_headers
from source_art import (
    DownloadedSource,
    PinnedHTTPResponse,
    SourceArtError,
    SourceArtStore,
    SourceDigestMismatch,
    SourceResourceError,
    SourceSecurityError,
    download_source,
    fetch_derivative,
    normalize_and_store,
    resolve_public_addresses,
    validate_locator_for_kind,
)
from tmdb import (
    V2ArtworkCandidate,
    V2TMDBMetadata,
    clear_v2_metadata_cache,
    fetch_v2_metadata,
    fetch_v2_release_status,
)
from v2_enrich import (
    EnrichmentRuntime,
    ProviderHooks,
    SourceArtUnavailable,
    UnsupportedPresetVersion,
    enrich,
    freeze_lifecycle_facts,
)
from v2_render import canonical_snapshot_sha256, render


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _image_bytes(fmt: str, size=(900, 1350), color=(30, 80, 120, 255)) -> bytes:
    image = Image.new("RGBA", size, color)
    buf = io.BytesIO()
    if fmt.upper() == "JPEG":
        image.convert("RGB").save(buf, format="JPEG", quality=90)
    else:
        image.save(buf, format=fmt)
    return buf.getvalue()


def _locator(provider="tmdb", url="https://image.tmdb.org/t/p/w500/poster.jpg"):
    return ArtworkLocator(provider=provider, url=url)


def _known_art(
    *,
    expires_at: datetime | None = None,
    role: str = "primary",
    policy_key: str = "original.primary",
) -> SourceArtReference:
    verified = role == "textless_poster"
    return SourceArtReference(
        source_art_id="poster-known-1",
        kind="poster",
        role=role,
        policy_key=policy_key,
        sha256="a" * 64,
        byte_size=12_345,
        mime="image/jpeg",
        recipe_version=1,
        locator=_locator(),
        locale="neutral",
        reconstructable=True,
        observed_at=NOW - timedelta(days=1),
        checked_at=NOW - timedelta(days=1),
        expires_at=expires_at or NOW + timedelta(days=7),
        textless_verified=True if verified else None,
        verification_recipe="ppocr.textless.v1" if verified else None,
        verified_at=NOW - timedelta(days=1) if verified else None,
        verification_source_digest="a" * 64 if verified else None,
    )


def _installed_known_art(store: SourceArtStore) -> SourceArtReference:
    payload = _image_bytes("JPEG", size=(500, 750))
    derivative = store.install(
        kind="poster",
        recipe_version=1,
        payload=payload,
        mime="image/jpeg",
        width=500,
        height=750,
        locator=None,
        now=NOW,
        pinned=True,
        reconstructable=False,
    )
    return SourceArtReference(
        source_art_id=derivative.source_art_id,
        kind="poster",
        role="primary",
        policy_key="original.primary",
        sha256=derivative.sha256,
        byte_size=derivative.byte_size,
        mime=derivative.mime,
        recipe_version=derivative.recipe_version,
        locale="neutral",
        reconstructable=False,
        observed_at=NOW - timedelta(hours=2),
        checked_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=30),
    )


def _reconstructable_known_art(
    store: SourceArtStore,
) -> tuple[SourceArtReference, bytes, Path]:
    derivative = normalize_and_store(
        "poster",
        io.BytesIO(_image_bytes("JPEG", size=(900, 1350))),
        1,
        store=store,
        locator=_locator(),
        now=NOW,
    )
    reference = SourceArtReference(
        source_art_id=derivative.source_art_id,
        kind="poster",
        role="primary",
        policy_key="original.primary",
        sha256=derivative.sha256,
        byte_size=derivative.byte_size,
        mime=derivative.mime,
        recipe_version=derivative.recipe_version,
        locator=derivative.locator,
        locale="neutral",
        reconstructable=True,
        observed_at=NOW - timedelta(hours=2),
        checked_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=30),
    )
    path = Path(derivative.path)
    return reference, path.read_bytes(), path


def _rating(
    *,
    provider: str = "letterboxd",
    normalized_score: float = 84.0,
    expires_at: datetime | None = None,
) -> ProviderRating:
    scale = 5.0 if provider == "letterboxd" else 10.0
    return ProviderRating(
        provider=provider,
        metric="score",
        score=normalized_score * scale / 100.0,
        scale=scale,
        normalized_score=normalized_score,
        vote_count=1234,
        source="mdblist",
        observed_at=NOW - timedelta(hours=1),
        checked_at=NOW - timedelta(hours=1),
        expires_at=expires_at or NOW + timedelta(days=7),
    )


def _request(
    config: dict | list[dict],
    *,
    known_ratings=(),
    known_facts=None,
    known_source_art=None,
    tmdb_id=11,
    imdb_id="tt0133093",
    media_type="movie",
) -> EnrichmentRequest:
    fact_values = known_facts or {}
    fact_envelope = (
        fact_values
        if "values" in fact_values or "provenance" in fact_values
        else {
            "values": fact_values,
            "provenance": [
                {
                    "fields": sorted(fact_values),
                    "source": "bingecat",
                    "observed_at": (NOW - timedelta(days=1)).isoformat(),
                    "checked_at": (NOW - timedelta(hours=1)).isoformat(),
                    "expires_at": (NOW + timedelta(days=7)).isoformat(),
                }
            ]
            if fact_values
            else [],
        }
    )
    return EnrichmentRequest.model_validate(
        {
            "schema": CONTRACT_SCHEMA,
            "version": CONTRACT_VERSION,
            "media": {
                "media_type": media_type,
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
            },
            "locales": ["en", "nl"],
            "titles_by_locale": {"en": "The Matrix", "nl": "The Matrix"},
            "canonical_configs": config if isinstance(config, list) else [config],
            "known_ratings": [item.model_dump(mode="json") for item in known_ratings],
            "known_facts": fact_envelope,
            "known_source_art": [
                item.model_dump(mode="json")
                for item in (known_source_art if known_source_art is not None else [_known_art()])
            ],
        }
    )


def _art_only_config() -> dict:
    return {
        "rating_display_mode": 0,
        "show_award_sash": False,
        "badge_display_mode": 0,
        "use_original_art": True,
        "textless": True,
    }


def _all_fact_config() -> dict:
    return {
        "rating_display_mode": 1,
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": ["wins", "cult", "trending"],
        "badge_display_mode": 3,
        "use_original_art": True,
        "textless": True,
    }


def _preset_rating_only_config(preset_ref: str) -> dict:
    config = json.loads(get_preset(preset_ref).config.canonical_json())
    config.update(
        show_award_sash=False,
        sash_mode="hidden",
        hide_genre=True,
        use_original_art=True,
        textless=True,
    )
    return json.loads(canonicalize_config(config).canonical_json())


def _minimalist_rating_only_config() -> dict:
    return _preset_rating_only_config("minimalist@1")


def _tmdb_metadata(*candidates: V2ArtworkCandidate) -> V2TMDBMetadata:
    return V2TMDBMetadata(
        tmdb_id="11",
        media_type="movie",
        title="The Matrix",
        genre_ids=(28, 878),
        release_date="1999-03-30",
        release_year=1999,
        original_language="en",
        original_title="The Matrix",
        runtime=136,
        tmdb_status="Released",
        candidates=tuple(candidates),
    )


def _runtime(
    hooks: ProviderHooks,
    *,
    stateless=True,
    source_store: SourceArtStore | None = None,
) -> EnrichmentRuntime:
    return EnrichmentRuntime(
        client=object(),
        pool=None,
        tmdb_key="tmdb-key",
        mdblist_key="mdblist-key",
        stateless_metadata=stateless,
        hooks=hooks,
        source_store=source_store,
    )


def _render_enrichment_result(
    config: dict,
    result: EnrichmentResult,
    *,
    source_store: SourceArtStore | None = None,
) -> bytes:
    spec = canonicalize_config(config)
    canonical_config = json.loads(spec.canonical_json())
    snapshot = ImmutableRenderSnapshot(
        evaluated_at=result.evaluated_at,
        titles_by_locale=result.titles_by_locale,
        ratings=result.ratings,
        facts=result.facts,
        source_art=result.source_art,
    )
    bundle = RenderInputBundle(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        media=result.media,
        locale="en",
        canonical_config=canonical_config,
        config_sha256=spec.sha256(),
        snapshot_sha256=canonical_snapshot_sha256(
            snapshot,
            media=result.media,
            spec=spec,
            locale="en",
        ),
        snapshot=snapshot,
    )
    payload, _metadata = render(bundle, source_store=source_store)
    return payload


def test_rating_details_preserve_scale_votes_and_legacy_projection(monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {
                "released": "1999-03-30",
                "age_rating": 16,
                "keywords": [{"name": "cult-classic"}],
                "ratings": [
                    {"source": "letterboxd", "value": 4.3, "votes": "1,234"},
                    {"source": "imdb", "value": 8.7, "vote_count": 2_000_000},
                    {"source": "imdb", "value": -1, "votes": 1_000},
                    {"source": "imdb", "value": 9, "votes": 10**30},
                    {"source": "trakt", "value": 80, "votes": 2_147_483_648},
                    None,
                ],
            }

    class Client:
        calls = 0

        async def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

    client = Client()
    details = asyncio.run(fetch_rating_details(client, "tt0133093", "key", [28], "movie"))
    assert client.calls == 1
    assert details.ratings[0] == RatingDetail(
        provider="letterboxd",
        score=4.3,
        scale=5.0,
        normalized_score=86.0,
        vote_count=1234,
    )
    assert details.ratings[1].scale == 10.0
    assert details.ratings[1].vote_count == 2_000_000
    assert len(details.ratings) == 2

    sanitized_details = RatingFetchDetails(
        details.ratings,
        details.genre,
        details.release_date,
        details.keywords,
        details.age_rating,
    )
    mocked = AsyncMock(return_value=sanitized_details)
    monkeypatch.setattr("ratings.fetch_rating_details", mocked)
    legacy = asyncio.run(fetch_rating(client, "tt0133093", "key", [28], "movie"))
    assert legacy == (
        {"letterboxd": 4.3, "imdb": 8.7},
        details.genre,
        "1999-03-30",
        [{"name": "cult-classic"}],
        16,
    )
    assert mocked.await_count == 1


def test_legacy_rating_projection_keeps_raw_types_and_last_provider_wins():
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {
                "released": "1999-03-30",
                "age_rating": "99",
                "keywords": [{"name": "one"}, {"name": "two"}],
                "ratings": [
                    {"source": "imdb", "value": "8.7", "votes": 1000},
                    {"source": "imdb", "value": "9.1", "votes": 2000},
                ],
            }

    class Client:
        async def get(self, *_args, **_kwargs):
            return Response()

    legacy = asyncio.run(fetch_rating(Client(), "tt0133093", "key", [], "movie"))
    assert legacy == (
        {"imdb": "9.1"},
        "Unknown",
        "1999-03-30",
        [{"name": "one"}, {"name": "two"}],
        99,
    )


def test_resolver_trusts_both_authenticated_ids_without_lookup():
    class NoCalls:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("TMDB must not be called")

    result = asyncio.run(
        resolve_v2_identity(
            pool=None,
            client=NoCalls(),
            tmdb_key="key",
            imdb_id="tt0133093",
            tmdb_id=11,
            media_type="movie",
        )
    )
    assert result == ResolvedIdentity("tt0133093", "11", "movie", "authenticated_v2_input")


def test_enrichment_does_not_resolve_unused_missing_identity_side():
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("no provider identity is needed for fresh art-only input")

    result = asyncio.run(
        enrich(
            _request(_art_only_config(), imdb_id=None),
            NOW,
            runtime=_runtime(ProviderHooks.all(forbidden)),
        )
    )
    assert result.media.tmdb_id == 11
    assert result.media.imdb_id is None
    assert result.provider_statuses == ()
    assert result.partial is False


def test_tmdb_v2_adapter_uses_one_multi_locale_call_and_stateless_mode_skips_cache():
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": 11,
                "title": "The Matrix",
                "release_date": "1999-03-30",
                "genres": [{"id": 28}],
                "original_language": "en",
                "images": {
                    "posters": [
                        {"file_path": "/neutral.jpg", "iso_639_1": None, "vote_average": 8, "vote_count": 20},
                        {"file_path": "/nl.jpg", "iso_639_1": "nl", "vote_average": 7, "vote_count": 10},
                    ],
                    "logos": [{"file_path": "/logo.svg", "iso_639_1": "en", "vote_average": 8}],
                    "backdrops": [{"file_path": "/backdrop.jpg", "iso_639_1": None, "vote_average": 8}],
                },
                "credits": {"cast": [], "crew": []},
                "external_ids": {"imdb_id": "tt0133093"},
            }

    class Client:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None, **_kwargs):
            self.calls.append((url, params or {}))
            return Response()

    clear_v2_metadata_cache()
    client = Client()
    first = asyncio.run(
        fetch_v2_metadata(
            client,
            "11",
            "key",
            "movie",
            ("nl", "en"),
            need_images=True,
            need_credits=True,
            need_external_ids=True,
            need_original_assets=True,
            cache_mode="off",
        )
    )
    assert len(client.calls) == 1
    params = client.calls[0][1]
    assert params["append_to_response"] == "images,credits,external_ids"
    assert set(params["include_image_language"].split(",")) == {"null", "en", "nl"}
    assert {candidate.kind for candidate in first.candidates} == {"poster", "logo", "backdrop"}

    asyncio.run(
        fetch_v2_metadata(
            client, "11", "key", "movie", ("nl", "en"),
            need_images=True, need_credits=True, need_external_ids=True,
            need_original_assets=True, cache_mode="off",
        )
    )
    assert len(client.calls) == 2

    clear_v2_metadata_cache()
    cached_client = Client()
    for _ in range(2):
        asyncio.run(
            fetch_v2_metadata(
                cached_client, "11", "key", "movie", ("nl", "en"),
                need_images=True, need_credits=True, need_external_ids=True,
                need_original_assets=True, cache_mode="read_write",
            )
        )
    assert len(cached_client.calls) == 1


def test_tmdb_v2_adapter_sanitizes_malformed_provider_scalars():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": 11,
                "title": "The Matrix",
                "release_date": "not-a-date",
                "original_language": {"unexpected": True},
                "images": {
                    "posters": [
                        {
                            "file_path": "/poster.jpg",
                            "iso_639_1": None,
                            "vote_average": "NaN",
                            "vote_count": -99,
                        }
                    ]
                },
            }

    class Client:
        async def get(self, *_args, **_kwargs):
            return Response()

    metadata = asyncio.run(
        fetch_v2_metadata(
            Client(), "11", "key", "movie", ("en",),
            need_images=True, need_credits=False, need_external_ids=False,
            need_original_assets=False, cache_mode="off",
        )
    )
    assert metadata.release_date is None
    assert metadata.release_year is None
    assert metadata.original_language is None
    assert metadata.candidates[0].vote_average == 0.0
    assert metadata.candidates[0].vote_count == 0


def test_year_render_requirement_fetches_tmdb_once_and_reuses_known_year():
    calls = Counter()

    async def tmdb(*_args, **_kwargs):
        calls["tmdb"] += 1
        return _tmdb_metadata()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    year_config = {
        **_art_only_config(),
        "rating_display_mode": 1,
        "accent_bar_append_mode": 0,
    }
    result = asyncio.run(
        enrich(
            _request(
                year_config,
                known_ratings=(
                    _rating(),
                    _rating(provider="imdb"),
                    _rating(provider="tmdb"),
                    _rating(provider="trakt"),
                    _rating(provider="tomatoes", normalized_score=82.0),
                ),
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert calls == Counter({"tmdb": 1})
    assert result.facts.values.release_year == 1999
    assert result.facts.values.genre == "Sci-Fi"

    no_calls = ProviderHooks.all(forbidden)
    known = asyncio.run(
        enrich(
            _request(
                year_config,
                known_ratings=(
                    _rating(),
                    _rating(provider="imdb"),
                    _rating(provider="tmdb"),
                    _rating(provider="trakt"),
                    _rating(provider="tomatoes", normalized_score=82.0),
                ),
                known_facts={"release_year": 1999, "genre": "Sci-Fi"},
            ),
            NOW,
            runtime=_runtime(no_calls),
        )
    )
    assert known.facts.values.release_year == 1999
    assert known.provider_statuses == ()


def test_genre_only_frosted_bar_still_requires_tmdb_genre_fact():
    calls = Counter()

    async def tmdb(*_args, **_kwargs):
        calls["tmdb"] += 1
        return _tmdb_metadata()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    config = {
        **_art_only_config(),
        "rating_display_mode": 4,
        "bar_append": "year",
    }
    result = asyncio.run(
        enrich(
            _request(config, known_facts={"release_year": 1999}),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert calls == Counter({"tmdb": 1})
    assert result.facts.values.genre == "Sci-Fi"


def test_enrichment_unions_requirements_and_gates_combined_provider_calls():
    calls = Counter()

    async def ratings(*_args, **_kwargs):
        calls["mdblist"] += 1
        return RatingFetchDetails(
            ratings=(RatingDetail("letterboxd", 4.2, 5.0, 84.0, 1234),),
            genre="Action",
            release_date="1999-03-30",
            keywords=({"name": "best-picture-winner"}, {"name": "cult-classic"}),
            age_rating=16,
        )

    async def trending(*_args, **_kwargs):
        calls["trending"] += 1
        return 7

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("gated provider was called")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=ratings,
        fetch_tmdb=forbidden,
        fetch_trending=trending,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    result = asyncio.run(enrich(_request(_all_fact_config()), NOW, runtime=_runtime(hooks)))

    assert calls == Counter({"mdblist": 1, "trending": 1})
    assert result.ratings[0].scale == 5.0
    assert result.ratings[0].vote_count == 1234
    assert result.facts.values.award_wins == ("Oscar Winner",)
    assert result.facts.values.is_cult is True
    assert result.facts.values.age_rating == 16
    assert result.facts.values.trending_rank == 7
    assert all(status.provider != "quality" for status in result.provider_statuses)


def test_nonexpired_known_values_prevent_all_provider_calls_and_preserve_false():
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("known fresh data must prevent provider calls")

    hooks = ProviderHooks.all(forbidden)
    result = asyncio.run(
        enrich(
            _request(
                _all_fact_config(),
                known_ratings=(_rating(),),
                known_facts={
                    "genre": "Action",
                    "award_wins": [],
                    "award_nominations": [],
                    "keywords": [],
                    "certification": "16",
                    "age_rating": 16,
                    "trending_rank": 999,
                    "is_cult": False,
                    "is_true_story": False,
                    "is_metacritic_must_see": False,
                },
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert result.ratings == (_rating(),)
    assert result.facts.values.is_cult is False
    assert result.source_art == (_known_art(),)


def test_expired_known_fact_group_is_ignored_and_refreshed_once() -> None:
    calls = Counter()

    async def tmdb(*_args, **_kwargs):
        calls["tmdb"] += 1
        return _tmdb_metadata()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    config = {
        **_art_only_config(),
        "rating_display_mode": 1,
        "accent_bar_append_mode": 0,
    }
    expired = {
        "values": {"genre": "Wrong", "release_year": 1980},
        "provenance": [
            {
                "fields": ["genre", "release_year"],
                "source": "bingecat",
                "observed_at": (NOW - timedelta(days=3)).isoformat(),
                "checked_at": (NOW - timedelta(days=2)).isoformat(),
                "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
            }
        ],
    }
    result = asyncio.run(
        enrich(
            _request(
                config,
                known_ratings=(_rating(),),
                known_facts=expired,
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )

    assert calls == Counter({"tmdb": 1})
    assert result.facts.values.release_year == 1999
    assert result.facts.values.genre == "Sci-Fi"
    assert {group.source for group in result.facts.provenance} == {"tmdb"}


def test_only_facts_for_visible_sash_slots_are_required_from_mdblist():
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unrelated MDBList fields must not cause a refresh")

    config = {
        **_art_only_config(),
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": ["wins", "cult"],
    }
    result = asyncio.run(
        enrich(
            _request(
                config,
                known_facts={"award_wins": [], "is_cult": False},
            ),
            NOW,
            runtime=_runtime(ProviderHooks.all(forbidden)),
        )
    )
    assert result.facts.values.award_wins == ()
    assert result.facts.values.is_cult is False
    assert result.provider_statuses == ()


def test_mdblist_fact_refresh_merges_instead_of_dropping_fresh_known_ratings():
    known_imdb = _rating().model_copy(
        update={
            "provider": "imdb",
            "score": 8.7,
            "scale": 10.0,
            "normalized_score": 87.0,
        }
    )

    async def ratings(*_args, **_kwargs):
        return RatingFetchDetails(
            ratings=(RatingDetail("letterboxd", 4.2, 5.0, 84.0, 1234),),
            genre="Action",
            release_date="1999-03-30",
            keywords=({"name": "best-picture-winner"},),
            age_rating=None,
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=ratings,
        fetch_tmdb=forbidden,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    config = {
        **_art_only_config(),
        "rating_display_mode": 1,
        "accent_bar_append_mode": 1,
        "hide_genre": True,
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": ["wins"],
    }
    result = asyncio.run(
        enrich(
            _request(config, known_ratings=(known_imdb,)),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert [item.provider for item in result.ratings] == ["imdb", "letterboxd"]


def test_minimalist_movie_imdb_only_fetches_positive_weight_providers():
    calls = Counter()

    async def ratings(*_args, **_kwargs):
        calls["mdblist"] += 1
        return RatingFetchDetails(
            ratings=(
                RatingDetail("letterboxd", 4.2, 5.0, 84.0, 1_234),
                RatingDetail("trakt", 81.0, 100.0, 81.0, 5_678),
                RatingDetail("metacritic", 74.0, 100.0, 74.0, 2_345),
            ),
            genre="Unknown",
            release_date=None,
            keywords=(),
            age_rating=None,
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=ratings,
        fetch_tmdb=forbidden,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    result = asyncio.run(
        enrich(
            _request(
                _minimalist_rating_only_config(),
                known_ratings=(
                    _rating(provider="imdb", normalized_score=87.0),
                ),
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert calls == Counter({"mdblist": 1})
    assert {item.provider for item in result.ratings} == {
        "imdb",
        "letterboxd",
        "metacritic",
        "trakt",
    }
    status = next(item for item in result.provider_statuses if item.provider == "mdblist")
    assert status.status == "complete"


@pytest.mark.parametrize(
    ("media_type", "providers"),
    (
        ("movie", ("letterboxd", "trakt", "metacritic")),
        ("series", ("trakt", "tomatoes", "metacritic")),
    ),
)
def test_minimalist_complete_weighted_provider_set_skips_mdblist(
    media_type: str,
    providers: tuple[str, ...],
):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("complete weighted ratings must prevent provider calls")

    result = asyncio.run(
        enrich(
            _request(
                _minimalist_rating_only_config(),
                known_ratings=tuple(
                    _rating(provider=provider, normalized_score=80.0 + index)
                    for index, provider in enumerate(providers)
                ),
                media_type=media_type,
            ),
            NOW,
            runtime=_runtime(ProviderHooks.all(forbidden)),
        )
    )
    assert {item.provider for item in result.ratings} == set(providers)
    assert result.provider_statuses == ()


@pytest.mark.parametrize("preset_ref", ("clean-notch@1", "minimalist@1"))
def test_separately_displayed_metacritic_is_required_at_zero_weight(preset_ref: str):
    calls = Counter()

    async def ratings(*_args, **_kwargs):
        calls["mdblist"] += 1
        return RatingFetchDetails(
            ratings=(RatingDetail("metacritic", 74.0, 100.0, 74.0, 2_345),),
            genre="Unknown",
            release_date=None,
            keywords=(),
            age_rating=None,
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=ratings,
        fetch_tmdb=forbidden,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    result = asyncio.run(
        enrich(
            _request(
                _preset_rating_only_config(preset_ref),
                known_ratings=(
                    _rating(provider="letterboxd", normalized_score=84.0),
                    _rating(provider="trakt", normalized_score=81.0),
                ),
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert calls == Counter({"mdblist": 1})
    assert {item.provider for item in result.ratings} == {
        "letterboxd",
        "trakt",
        "metacritic",
    }
    status = next(item for item in result.provider_statuses if item.provider == "mdblist")
    assert status.status == "complete"


def test_namespaced_bingecat_rating_sources_round_trip_through_enrichment():
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("fresh optional evidence must not call providers")

    ratings = tuple(
        _rating(provider=provider, normalized_score=80.0 + index).model_copy(
            update={"source": source}
        )
        for index, (provider, source) in enumerate(
            (
                ("letterboxd", "mdblist:imdb"),
                ("imdb", "imdb:title_metrics"),
                ("tmdb", "tmdb:volatile"),
            )
        )
    )
    result = asyncio.run(
        enrich(
            _request(_art_only_config(), known_ratings=ratings),
            NOW,
            runtime=_runtime(ProviderHooks.all(forbidden)),
        )
    )
    assert {item.source for item in result.ratings} == {
        "imdb:title_metrics",
        "mdblist:imdb",
        "tmdb:volatile",
    }
    assert result.provider_statuses == ()


@pytest.mark.parametrize(
    "invalid_detail",
    (
        RatingDetail("letterboxd", 4.2, 5.0, 84.0, 2_147_483_648),
        RatingDetail("letterboxd", 100_000.0, 100_000.0, 84.0, 100),
    ),
)
def test_unpersistable_provider_rating_becomes_typed_error(invalid_detail):
    async def ratings(*_args, **_kwargs):
        return RatingFetchDetails(
            ratings=(invalid_detail,),
            genre="Unknown",
            release_date=None,
            keywords=(),
            age_rating=None,
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=ratings,
        fetch_tmdb=forbidden,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    result = asyncio.run(
        enrich(
            _request(_minimalist_rating_only_config()),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    status = next(item for item in result.provider_statuses if item.provider == "mdblist")
    assert status.status == "error"
    assert status.retry_at == NOW + timedelta(minutes=5)
    assert not result.ratings
    assert result.partial is False


def test_explicit_known_lifecycle_facts_skip_tmdb_and_release_calls():
    calls = Counter()

    async def counted(*_args, **_kwargs):
        calls["provider"] += 1
        return None

    hooks = ProviderHooks.all(counted)
    config = {
        **_art_only_config(),
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": ["new_season", "returning", "season_finale"],
    }
    result = asyncio.run(
        enrich(
            _request(
                config,
                known_facts={
                    "is_new_season": False,
                    "is_returning": False,
                    "is_season_finale": False,
                    "is_premiere": False,
                    "is_just_added": False,
                },
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert calls == Counter()
    assert result.provider_statuses == ()


def test_fallback_enabled_config_materializes_poster_backdrop_and_logo():
    materialized = []
    candidates = tuple(
        V2ArtworkCandidate(
            kind=kind,
            locator=_locator(
                url={
                    "poster": "https://image.tmdb.org/t/p/w500/poster.jpg",
                    "backdrop": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
                    "logo": "https://image.tmdb.org/t/p/original/logo.png",
                }[kind]
            ),
            locale="neutral",
        )
        for kind in ("poster", "backdrop", "logo")
    )

    async def tmdb(*_args, **_kwargs):
        return _tmdb_metadata(*candidates)

    async def materialize(candidate, evaluated_at, runtime):
        assert runtime.require_ocr is (candidate.kind == "poster")
        materialized.append(candidate.kind)
        verified = candidate.kind == "poster"
        return SourceArtReference(
            source_art_id=f"{candidate.kind}-source",
            kind=candidate.kind,
            role=runtime.art_role,
            policy_key=runtime.art_policy_key,
            sha256=hashlib.sha256(candidate.kind.encode()).hexdigest(),
            byte_size=100,
            mime="image/png" if candidate.kind == "logo" else "image/jpeg",
            recipe_version={"poster": 1, "backdrop": 5, "logo": 1}[candidate.kind],
            locator=candidate.locator,
            locale="neutral",
            reconstructable=True,
            observed_at=evaluated_at,
            checked_at=evaluated_at,
            expires_at=evaluated_at + timedelta(days=30),
            textless_verified=True if verified else None,
            verification_recipe="ppocr.textless.v1" if verified else None,
            verified_at=evaluated_at if verified else None,
            verification_source_digest=(
                hashlib.sha256(candidate.kind.encode()).hexdigest() if verified else None
            ),
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden, fetch_ratings=forbidden, fetch_tmdb=tmdb,
        fetch_trending=forbidden, fetch_release=forbidden, fetch_tvdb=forbidden,
        materialize_art=materialize,
    )
    config = {
        "rating_display_mode": 0,
        "show_award_sash": False,
        "badge_display_mode": 0,
        "use_original_art": False,
        "textless": False,
    }
    result = asyncio.run(
        enrich(_request(config, known_source_art=[]), NOW, runtime=_runtime(hooks))
    )
    assert materialized == ["poster", "backdrop", "logo"]
    assert {item.kind for item in result.source_art} == {"poster", "backdrop", "logo"}
    assert all(
        item.provider != "contract_provenance" for item in result.provider_statuses
    )


def test_logo_art_is_resolved_per_configured_language_not_only_by_kind():
    materialized = []
    candidates = tuple(
        V2ArtworkCandidate(
            "logo",
            _locator(url=f"https://image.tmdb.org/t/p/original/{locale}.png"),
            locale,
            vote_average=8.0,
        )
        for locale in ("en", "nl")
    )

    async def tmdb(*_args, **_kwargs):
        return _tmdb_metadata(*candidates)

    async def materialize(candidate, evaluated_at, runtime):
        assert runtime.require_ocr is False
        materialized.append(candidate.locale)
        return SourceArtReference(
            source_art_id=f"logo-{candidate.locale}",
            kind="logo",
            role=runtime.art_role,
            policy_key=runtime.art_policy_key,
            sha256=hashlib.sha256(candidate.locale.encode()).hexdigest(),
            byte_size=100,
            mime="image/png",
            recipe_version=1,
            locator=candidate.locator,
            locale=candidate.locale,
            reconstructable=True,
            observed_at=evaluated_at,
            checked_at=evaluated_at,
            expires_at=evaluated_at + timedelta(days=30),
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=materialize,
    )
    base_config = {
        "rating_display_mode": 0,
        "show_award_sash": False,
        "badge_display_mode": 0,
        "use_original_art": False,
        "textless": False,
    }
    known_poster = _known_art(
        role="textless_poster",
        policy_key="fallback.textless",
    )
    known_backdrop = _known_art().model_copy(
        update={
                "source_art_id": "backdrop-known",
                "kind": "backdrop",
                "role": "fallback_backdrop",
                "policy_key": "fallback.backdrop",
                "recipe_version": 5,
            "locator": _locator(url="https://image.tmdb.org/t/p/w1280/backdrop.jpg"),
        }
    )
    known_english_logo = _known_art().model_copy(
        update={
                "source_art_id": "logo-en-known",
                "kind": "logo",
                "role": "logo",
                "policy_key": "logo.native_original.en",
                "mime": "image/png",
                "locale": "en",
                "locator": _locator(url="https://image.tmdb.org/t/p/original/en.png"),
                "textless_verified": None,
                "verification_recipe": None,
                "verified_at": None,
                "verification_source_digest": None,
            }
        )
    result = asyncio.run(
        enrich(
            _request(
                [
                    {**base_config, "logo_language": "en"},
                    {**base_config, "logo_language": "nl"},
                ],
                known_source_art=[known_poster, known_backdrop, known_english_logo],
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert materialized == ["nl"]
    assert {(item.kind, item.locale) for item in result.source_art} >= {
        ("logo", "en"),
        ("logo", "nl"),
    }


def test_failed_tmdb_art_materialization_falls_back_to_tvdb_once():
    calls = Counter()
    tmdb_candidate = V2ArtworkCandidate("poster", _locator(), "neutral")
    tvdb_candidate = V2ArtworkCandidate(
        "poster",
        _locator("tvdb", "https://artworks.thetvdb.com/banners/movies/poster.jpg"),
        "neutral",
    )

    async def tmdb(*_args, **_kwargs):
        return _tmdb_metadata(tmdb_candidate)

    async def tvdb(*_args, **_kwargs):
        calls["tvdb_fetch"] += 1
        assert _kwargs["cache_metadata"] is False
        return (tvdb_candidate,)

    async def materialize(candidate, evaluated_at, *_args):
        calls[f"materialize_{candidate.locator.provider}"] += 1
        if candidate.locator.provider == "tmdb":
            raise SourceArtError("bad TMDB source")
        return _known_art(expires_at=evaluated_at + timedelta(days=30)).model_copy(
            update={"locator": candidate.locator}
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden, fetch_ratings=forbidden, fetch_tmdb=tmdb,
        fetch_trending=forbidden, fetch_release=forbidden, fetch_tvdb=tvdb,
        materialize_art=materialize,
    )
    result = asyncio.run(
        enrich(
            _request(_art_only_config(), known_source_art=[]),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert calls == Counter(
        {"materialize_tmdb": 1, "tvdb_fetch": 1, "materialize_tvdb": 1}
    )
    assert result.source_art[0].locator.provider == "tvdb"


def test_tvdb_v2_adapter_skips_untrusted_rows_without_losing_valid_art(monkeypatch):
    async def resolve(*_args, **_kwargs):
        return 123

    async def artworks(*_args, **_kwargs):
        return {
            "posters": [
                {"url": "https://evil.example/poster.jpg", "score": 100},
                {
                    "url": "https://artworks.thetvdb.com/banners/movies/poster.jpg",
                    "score": "NaN",
                    "language": "eng",
                },
            ],
            "logos": [],
            "backgrounds": [],
        }

    monkeypatch.setattr(tvdb_module, "tvdb_enabled", lambda: True)
    monkeypatch.setattr(tvdb_module, "TVDB_USE_POSTERS", True)
    monkeypatch.setattr(tvdb_module, "resolve_tvdb_id", resolve)
    monkeypatch.setattr(tvdb_module, "fetch_tvdb_artworks", artworks)
    candidates = asyncio.run(
        tvdb_module.fetch_v2_artwork_candidates(
            object(),
            media_type="movie",
            imdb_id="tt0133093",
            tmdb_id="11",
            kinds=("poster",),
            cache_metadata=False,
        )
    )
    assert len(candidates) == 1
    assert candidates[0].locator.url.startswith("https://artworks.thetvdb.com/")
    assert candidates[0].vote_average == 0.0


def test_expired_source_art_triggers_one_tmdb_lookup_and_atomic_materialization():
    calls = Counter()
    candidate = V2ArtworkCandidate(
        kind="poster",
        locator=_locator(),
        locale="neutral",
        vote_average=8.0,
        vote_count=20,
    )

    async def tmdb(*_args, **kwargs):
        calls["tmdb"] += 1
        assert kwargs["cache_mode"] == "off"
        return _tmdb_metadata(candidate)

    async def materialize(item, evaluated_at, *_args, **_kwargs):
        calls["materialize"] += 1
        assert item == candidate
        return _known_art(expires_at=evaluated_at + timedelta(days=30))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider called")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=materialize,
    )
    expired = _known_art(expires_at=NOW - timedelta(seconds=1))
    result = asyncio.run(
        enrich(
            _request(_art_only_config(), known_source_art=[expired]),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert calls == Counter({"tmdb": 1, "materialize": 1})
    assert result.source_art[0].expires_at > NOW


def test_fresh_known_source_art_is_verified_locally_without_provider_calls(tmp_path):
    store = SourceArtStore(tmp_path / "sources", tmp_path / "ledger.sqlite")
    reference, _payload, _path = _reconstructable_known_art(store)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("verified local source art must prevent provider calls")

    result = asyncio.run(
        enrich(
            _request(_art_only_config(), known_source_art=[reference]),
            NOW,
            runtime=_runtime(ProviderHooks.all(forbidden), source_store=store),
        )
    )

    assert result.source_art == (reference,)


def test_pruned_fresh_known_source_art_is_rebuilt_by_exact_digest(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = SourceArtStore(tmp_path / "sources", tmp_path / "ledger.sqlite")
    reference, payload, path = _reconstructable_known_art(store)
    path.unlink()
    calls = Counter()

    async def rebuild(locator, **kwargs):
        calls["rebuild"] += 1
        assert locator == reference.locator
        assert kwargs["expected_sha256"] == reference.sha256
        assert kwargs["store"] is store
        return store.install(
            kind=reference.kind,
            recipe_version=reference.recipe_version,
            payload=payload,
            mime=reference.mime,
            width=500,
            height=750,
            locator=reference.locator,
            now=kwargs["now"],
            pinned=False,
            reconstructable=True,
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("exact source reconstruction avoids metadata providers")

    monkeypatch.setattr(enrich_module, "fetch_derivative", rebuild)
    result = asyncio.run(
        enrich(
            _request(_art_only_config(), known_source_art=[reference]),
            NOW,
            runtime=_runtime(ProviderHooks.all(forbidden), source_store=store),
        )
    )

    assert calls == Counter({"rebuild": 1})
    assert result.source_art == (reference,)
    restored = store.get(reference.sha256, "poster", 1, now=NOW)
    assert restored is not None
    assert restored.pinned is False


def test_unrecoverable_fresh_known_source_art_has_one_bounded_typed_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = SourceArtStore(tmp_path / "sources", tmp_path / "ledger.sqlite")
    reference, _payload, path = _reconstructable_known_art(store)
    path.unlink()

    async def unavailable(*_args, **_kwargs):
        raise SourceArtError("sensitive provider and filesystem detail")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider hooks must not run after exact rebuild failure")

    monkeypatch.setattr(enrich_module, "fetch_derivative", unavailable)
    with pytest.raises(SourceArtUnavailable) as captured:
        asyncio.run(
            enrich(
                _request(_art_only_config(), known_source_art=[reference]),
                NOW,
                runtime=_runtime(ProviderHooks.all(forbidden), source_store=store),
            )
        )

    assert captured.value.code == "source_art_unavailable"
    assert captured.value.status_code == 503
    assert str(captured.value) == "source_art_unavailable"
    assert "sensitive" not in str(captured.value)


def test_stateful_enrichment_passes_read_write_cache_mode():
    modes = []

    async def tmdb(*_args, **kwargs):
        modes.append(kwargs["cache_mode"])
        return _tmdb_metadata()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider called")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=lambda *_args, **_kwargs: (),
        materialize_art=forbidden,
    )
    result = asyncio.run(
        enrich(
            _request(_art_only_config(), known_source_art=[]),
            NOW,
            runtime=_runtime(hooks, stateless=False),
        )
    )
    assert modes == ["read_write"]
    assert result.partial is False
    source_status = next(
        item for item in result.provider_statuses if item.provider == "source_art"
    )
    assert source_status.status == "missing"
    assert source_status.expires_at == NOW + timedelta(hours=6)


def test_missing_tmdb_key_is_checked_without_spending_provider_call():
    calls = Counter()

    async def tmdb(*_args, **_kwargs):
        calls["tmdb"] += 1
        return _tmdb_metadata()

    async def tvdb(*_args, **_kwargs):
        calls["tvdb"] += 1
        return ()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden, fetch_ratings=forbidden, fetch_tmdb=tmdb,
        fetch_trending=forbidden, fetch_release=forbidden, fetch_tvdb=tvdb,
        materialize_art=forbidden,
    )
    base = _runtime(hooks)
    runtime = EnrichmentRuntime(
        client=base.client, pool=base.pool, tmdb_key="", mdblist_key=base.mdblist_key,
        stateless_metadata=True, hooks=hooks,
    )
    result = asyncio.run(
        enrich(
            _request(_art_only_config(), known_source_art=[]),
            NOW,
            runtime=runtime,
        )
    )
    assert calls["tmdb"] == 0
    assert result.partial is False
    status = next(item for item in result.provider_statuses if item.provider == "tmdb")
    assert status.status == "missing"
    assert status.expires_at == NOW + timedelta(hours=6)


def test_missing_tmdb_key_gates_trending_and_release_adapters_too():
    calls = Counter()

    async def counted(*_args, **_kwargs):
        calls["forbidden_tmdb_adapter"] += 1
        raise AssertionError("TMDB adapter must not be called without a key")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=counted,
        fetch_trending=counted,
        fetch_release=counted,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    base = _runtime(hooks)
    runtime = EnrichmentRuntime(
        client=base.client,
        pool=base.pool,
        tmdb_key="",
        mdblist_key=base.mdblist_key,
        stateless_metadata=True,
        hooks=hooks,
    )
    config = {
        **_art_only_config(),
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": ["trending", "cinema"],
    }
    result = asyncio.run(enrich(_request(config), NOW, runtime=runtime))
    assert calls == Counter()
    assert result.partial is False
    statuses = {item.provider: item.status for item in result.provider_statuses}
    assert statuses["tmdb"] == "missing"
    assert statuses["tmdb_trending"] == "missing"
    assert statuses["tmdb_release"] == "missing"


def test_release_status_slots_are_resolved_even_without_new_release_slot():
    calls = Counter()

    async def tmdb(*_args, **_kwargs):
        calls["tmdb"] += 1
        return V2TMDBMetadata(
            tmdb_id="11",
            media_type="movie",
            title="The Matrix",
            tmdb_status="In Production",
        )

    async def release(*_args, **_kwargs):
        calls["release"] += 1
        return "Production"

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=release,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    config = {
        **_art_only_config(),
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": ["production"],
    }
    result = asyncio.run(enrich(_request(config), NOW, runtime=_runtime(hooks)))
    assert calls == Counter({"tmdb": 1, "release": 1})
    assert result.facts.values.release_status == "production"


def test_structural_sash_fetches_and_freezes_only_tmdb_metadata():
    calls = Counter()

    async def tmdb(*_args, **_kwargs):
        calls["tmdb"] += 1
        return V2TMDBMetadata(
            tmdb_id="11",
            media_type="movie",
            title="A Short",
            runtime=30,
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    config = {
        **_art_only_config(),
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": ["short_film"],
    }
    result = asyncio.run(enrich(_request(config), NOW, runtime=_runtime(hooks)))
    assert calls == Counter({"tmdb": 1})
    assert result.facts.values.is_short_film is True
    assert result.facts.values.is_just_added is None
    assert result.partial is False


def test_known_source_art_is_recipe_checked_and_bounded_by_kind_and_locale():
    calls = Counter()
    candidate = V2ArtworkCandidate("poster", _locator(), "neutral")

    async def tmdb(*_args, **_kwargs):
        calls["tmdb"] += 1
        return _tmdb_metadata(candidate)

    async def materialize(item, evaluated_at, *_args):
        calls["materialize"] += 1
        assert item == candidate
        return _known_art(expires_at=evaluated_at + timedelta(days=30))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=materialize,
    )
    stale_recipe = _known_art().model_copy(
        update={"source_art_id": "old-recipe", "recipe_version": 999}
    )
    duplicates = [
        _known_art().model_copy(
            update={
                "source_art_id": f"poster-duplicate-{index}",
                "checked_at": NOW - timedelta(minutes=index + 1),
            }
        )
        for index in range(20)
    ]
    result = asyncio.run(
        enrich(
            _request(
                _art_only_config(),
                known_source_art=[stale_recipe, *duplicates],
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    # The current-recipe duplicate is reused, while the incompatible recipe is
    # discarded.  A single locale/kind can never inflate the response bound.
    assert calls == Counter()
    assert len(result.source_art) == 1
    assert result.source_art[0].source_art_id == "poster-duplicate-0"
    assert result.source_art[0].recipe_version == 1


def test_optional_rating_rate_limit_returns_usable_snapshot_and_retry_timestamp(tmp_path):
    async def limited(*_args, **_kwargs):
        return _RateLimited(45)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider called")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=limited,
        fetch_tmdb=forbidden,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _installed_known_art(store)
    config = {
        **_art_only_config(),
        "rating_display_mode": 2,
        "hide_genre": True,
    }
    result = asyncio.run(
        enrich(
            _request(config, known_source_art=[art]),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert result.partial is False
    status = next(item for item in result.provider_statuses if item.provider == "mdblist")
    assert status.status == "rate_limited"
    assert status.retry_at == NOW + timedelta(seconds=45)
    assert status.expires_at == status.retry_at
    assert result.retry_at == status.retry_at
    rendered = _render_enrichment_result(config, result, source_store=store)
    assert rendered[:4] == b"RIFF"


def test_optional_trending_rate_limit_has_separate_retry_without_blocking():
    async def limited(*_args, **_kwargs):
        response = httpx.Response(
            429,
            headers={"Retry-After": "30"},
            request=httpx.Request("GET", "https://api.themoviedb.org/3/trending/movie/day"),
        )
        raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=forbidden,
        fetch_trending=limited,
        fetch_release=forbidden,
        fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    config = {
        **_art_only_config(),
        "show_award_sash": True,
        "sash_mode": "sash",
        "sash_priority": ["trending"],
    }
    result = asyncio.run(enrich(_request(config), NOW, runtime=_runtime(hooks)))
    status = next(item for item in result.provider_statuses if item.provider == "tmdb_trending")
    assert status.status == "rate_limited"
    assert status.missing_fields == ("trending_rank",)
    assert status.retry_at == NOW + timedelta(seconds=30)
    assert status.expires_at == status.retry_at
    assert result.partial is False
    assert result.retry_at == status.retry_at


def test_missing_required_rating_is_explicit_checked_but_snapshot_is_usable():
    async def empty(*_args, **_kwargs):
        return RatingFetchDetails((), "Unknown", None, (), None)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider called")

    hooks = ProviderHooks(
        resolve_identity=forbidden, fetch_ratings=empty, fetch_tmdb=forbidden,
        fetch_trending=forbidden, fetch_release=forbidden, fetch_tvdb=forbidden,
        materialize_art=forbidden,
    )
    result = asyncio.run(
        enrich(
            _request(
                {**_art_only_config(), "rating_display_mode": 2, "hide_genre": True},
            ),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    status = next(item for item in result.provider_statuses if item.provider == "mdblist")
    assert status.status == "missing"
    assert "ratings" in status.missing_fields
    assert status.expires_at == NOW + timedelta(days=7)
    assert status.retry_at is None
    assert result.partial is False
    assert result.retry_at is None


def test_transient_required_original_art_failure_remains_blocking():
    async def tmdb_failure(*_args, **_kwargs):
        raise httpx.ConnectError("temporary TMDB failure")

    async def empty_tvdb(*_args, **_kwargs):
        return ()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb_failure,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=empty_tvdb,
        materialize_art=forbidden,
    )
    result = asyncio.run(
        enrich(
            _request(_art_only_config(), known_source_art=[]),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    status = next(item for item in result.provider_statuses if item.provider == "tmdb")
    assert status.status == "error"
    assert status.retry_at == NOW + timedelta(minutes=5)
    assert status.expires_at == status.retry_at
    assert result.partial is True
    assert result.retry_at == status.retry_at


def test_transient_optional_fallback_art_and_logo_failure_stays_renderable():
    async def tmdb_failure(*_args, **_kwargs):
        raise httpx.ConnectError("temporary TMDB failure")

    async def tvdb_failure(*_args, **_kwargs):
        raise httpx.ConnectError("temporary TVDB failure")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unneeded provider")

    hooks = ProviderHooks(
        resolve_identity=forbidden,
        fetch_ratings=forbidden,
        fetch_tmdb=tmdb_failure,
        fetch_trending=forbidden,
        fetch_release=forbidden,
        fetch_tvdb=tvdb_failure,
        materialize_art=forbidden,
    )
    config = {
        "rating_display_mode": 0,
        "show_award_sash": False,
        "badge_display_mode": 0,
        "use_original_art": False,
        "textless": False,
        "hide_genre": True,
    }
    result = asyncio.run(
        enrich(
            _request(config, known_source_art=[]),
            NOW,
            runtime=_runtime(hooks),
        )
    )
    assert result.partial is False
    assert result.retry_at == NOW + timedelta(minutes=5)
    assert not result.source_art
    assert {
        (item.provider, item.status)
        for item in result.provider_statuses
    } >= {("tmdb", "error"), ("tvdb", "error"), ("source_art", "missing")}
    rendered = _render_enrichment_result(config, result)
    assert rendered[:4] == b"RIFF"


def test_lifecycle_facts_use_explicit_evaluated_at_only():
    data = {
        "tmdb_release_date": "2026-07-01",
        "next_episode": {"air_date": "2026-07-12", "season_number": 2, "episode_number": 1},
        "last_episode": {"air_date": "2026-07-05", "season_number": 1, "episode_number": 8},
        "seasons": [{"season_number": 2, "air_date": "2026-07-12", "episode_count": 8}],
        "number_of_seasons": 2,
        "number_of_episodes": 16,
        "tmdb_status": "Returning Series",
    }
    july = freeze_lifecycle_facts(data, "series", None, evaluated_at=NOW)
    december = freeze_lifecycle_facts(
        data,
        "series",
        None,
        evaluated_at=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    assert july["is_premiere"] is True
    assert july["is_new_season"] is True
    assert december["is_premiere"] is False
    assert december["is_new_season"] is False


def test_pre_release_movie_status_does_not_spend_release_dates_call():
    class NoCalls:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("pre-release TMDB status is already authoritative")

    status = asyncio.run(
        fetch_v2_release_status(
            NoCalls(),
            "11",
            "key",
            "movie",
            "In Production",
            evaluated_at=NOW,
            cache_mode="off",
        )
    )
    assert status == "Production"


def test_normalization_recipes_are_deterministic_and_ledger_converges(tmp_path):
    store = SourceArtStore(tmp_path / "sources", tmp_path / "ledger.sqlite")
    raw = _image_bytes("JPEG", (1200, 1600))
    first = normalize_and_store("poster", io.BytesIO(raw), 1, store=store, now=NOW)
    second = normalize_and_store(
        "poster", io.BytesIO(raw), 1, store=store, now=NOW + timedelta(days=1)
    )
    assert first.sha256 == second.sha256
    assert first.path == second.path
    assert second.created_at == first.created_at
    assert second.last_used_at == NOW + timedelta(days=1)
    assert (first.width, first.height, first.mime) == (500, 750, "image/jpeg")
    assert hashlib.sha256(Path(first.path).read_bytes()).hexdigest() == first.sha256
    with sqlite3.connect(tmp_path / "ledger.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_art_ledger").fetchone()[0] == 1

    def normalize_once(_index):
        return normalize_and_store("poster", io.BytesIO(raw), 1, store=store, now=NOW)

    with ThreadPoolExecutor(max_workers=6) as executor:
        concurrent = list(executor.map(normalize_once, range(12)))
    assert {item.path for item in concurrent} == {first.path}
    with sqlite3.connect(tmp_path / "ledger.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_art_ledger WHERE kind='poster'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT last_used_at FROM source_art_ledger WHERE kind='poster'"
        ).fetchone()[0] == (NOW + timedelta(days=1)).timestamp()

    backdrop = normalize_and_store(
        "backdrop", io.BytesIO(_image_bytes("JPEG", (1600, 900))), 5,
        store=store, now=NOW,
    )
    assert (backdrop.width, backdrop.height, backdrop.mime) == (500, 750, "image/jpeg")

    logo_image = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
    for x in range(20, 80):
        for y in range(15, 35):
            logo_image.putpixel((x, y), (255, 255, 255, 255))
    logo_buf = io.BytesIO()
    logo_image.save(logo_buf, format="PNG")
    logo = normalize_and_store("logo", io.BytesIO(logo_buf.getvalue()), 1, store=store, now=NOW)
    assert (logo.width, logo.height, logo.mime) == (60, 20, "image/png")


def test_normalizer_rejects_wrong_recipe_mime_animation_dimensions_and_unsafe_svg(tmp_path):
    store = SourceArtStore(tmp_path / "sources", tmp_path / "ledger.sqlite")
    png = _image_bytes("PNG", (100, 150))
    with pytest.raises(SourceArtError, match="recipe"):
        normalize_and_store("poster", io.BytesIO(png), 2, store=store, now=NOW)
    with pytest.raises(SourceArtError, match="MIME"):
        normalize_and_store(
            "poster", io.BytesIO(png), 1, declared_mime="image/jpeg", store=store, now=NOW,
        )

    frames = [Image.new("RGBA", (20, 20), (255, 0, 0, 255)), Image.new("RGBA", (20, 20), (0, 0, 255, 255))]
    animated = io.BytesIO()
    frames[0].save(animated, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(SourceResourceError, match="animated"):
        normalize_and_store("poster", io.BytesIO(animated.getvalue()), 1, store=store, now=NOW)

    too_wide = _image_bytes("PNG", (8193, 2))
    with pytest.raises(SourceResourceError, match="dimensions"):
        normalize_and_store("poster", io.BytesIO(too_wide), 1, store=store, now=NOW)

    unsafe_svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(SourceSecurityError, match="SVG"):
        normalize_and_store("logo", io.BytesIO(unsafe_svg), 1, store=store, now=NOW)
    external_svg = b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://evil.test/x";</style></svg>'
    with pytest.raises(SourceSecurityError, match="SVG"):
        normalize_and_store("logo", io.BytesIO(external_svg), 1, store=store, now=NOW)
    namespaced_image = (
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:s="http://www.w3.org/2000/svg" '
        b'width="20" height="20"><s:image href="data:image/png;base64,AA=="/></svg>'
    )
    with pytest.raises(SourceSecurityError, match="SVG"):
        normalize_and_store("logo", io.BytesIO(namespaced_image), 1, store=store, now=NOW)
    animated_svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        b'<rect width="20" height="20"><animate attributeName="x" values="0;10"/></rect></svg>'
    )
    with pytest.raises(SourceSecurityError, match="SVG"):
        normalize_and_store("logo", io.BytesIO(animated_svg), 1, store=store, now=NOW)


def test_safe_svg_logo_is_rasterized_without_upscale(tmp_path):
    store = SourceArtStore(tmp_path / "sources", tmp_path / "ledger.sqlite")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40"><rect width="120" height="40" fill="white"/></svg>'
    logo = normalize_and_store(
        "logo", io.BytesIO(svg), 1, declared_mime="image/svg+xml", store=store, now=NOW,
    )
    assert (logo.width, logo.height, logo.mime) == (120, 40, "image/png")


def test_locator_path_dns_peer_redirect_and_size_guards(tmp_path):
    validate_locator_for_kind(_locator(), "poster")
    validate_locator_for_kind(
        _locator("metahub", "https://images.metahub.space/logo/medium/tt0133093/img"),
        "logo",
    )
    with pytest.raises(SourceSecurityError):
        validate_locator_for_kind(
            _locator("metahub", "https://images.metahub.space/poster/medium/tt0133093/img"),
            "poster",
        )
    with pytest.raises(SourceSecurityError, match="SVG"):
        validate_locator_for_kind(
            _locator(url="https://image.tmdb.org/t/p/w500/poster.svg"),
            "poster",
        )

    def private(_host, _port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    with pytest.raises(SourceSecurityError, match="public"):
        resolve_public_addresses("image.tmdb.org", resolver=private)

    def public(_host, _port):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]

    png = _image_bytes("PNG", (10, 15))

    def peer_mismatch(*_args, **_kwargs):
        return PinnedHTTPResponse(200, {"content-type": "image/png"}, (png,), "1.1.1.1")

    with pytest.raises(SourceSecurityError, match="peer"):
        download_source(
            _locator(), "poster", temp_dir=tmp_path, resolver=public, requester=peer_mismatch,
        )

    def oversized(*_args, **_kwargs):
        return PinnedHTTPResponse(
            200,
            {"content-type": "image/png", "content-length": str(16 * 1024 * 1024 + 1)},
            (),
            "93.184.216.34",
        )

    with pytest.raises(SourceResourceError, match="large"):
        download_source(
            _locator(), "poster", temp_dir=tmp_path, resolver=public, requester=oversized,
        )

    redirects = Counter()

    def loop_redirect(*_args, **_kwargs):
        redirects["calls"] += 1
        return PinnedHTTPResponse(
            302,
            {"location": "https://image.tmdb.org/t/p/w500/again.jpg"},
            (),
            "93.184.216.34",
        )

    with pytest.raises(SourceSecurityError, match="redirect"):
        download_source(
            _locator(), "poster", temp_dir=tmp_path, resolver=public, requester=loop_redirect,
        )
    assert redirects["calls"] == 3

    def cross_host_redirect(*_args, **_kwargs):
        return PinnedHTTPResponse(
            302,
            {"location": "https://example.com/steal.jpg"},
            (),
            "93.184.216.34",
        )

    with pytest.raises(SourceSecurityError, match="redirect"):
        download_source(
            _locator(), "poster", temp_dir=tmp_path, resolver=public,
            requester=cross_host_redirect,
        )


def test_pinned_request_captures_peer_before_connection_close(monkeypatch):
    class Socket:
        @staticmethod
        def getpeername():
            return ("93.184.216.34", 443)

    class Response:
        status = 200

        @staticmethod
        def getheaders():
            return (("content-type", "image/png"),)

        @staticmethod
        def read(_size):
            return b""

    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.sock = Socket()

        def request(self, *_args, **_kwargs):
            return None

        def getresponse(self):
            self.sock = None
            return Response()

        def close(self):
            return None

    monkeypatch.setattr(source_art_module, "_PinnedHTTPSConnection", Connection)

    response = source_art_module._request_pinned(
        "https://image.tmdb.org/t/p/w500/poster.jpg",
        "93.184.216.34",
        "image.tmdb.org",
    )

    assert response.peer_ip == "93.184.216.34"


def test_fetch_derivative_deletes_raw_temp_even_on_digest_mismatch(tmp_path):
    store = SourceArtStore(tmp_path / "sources", tmp_path / "ledger.sqlite")
    raw_path = tmp_path / "raw-download"
    raw_path.write_bytes(_image_bytes("JPEG", (500, 750)))

    async def downloader(*_args, **_kwargs):
        return DownloadedSource(raw_path, "image/jpeg", _locator())

    with pytest.raises(SourceDigestMismatch):
        asyncio.run(
            fetch_derivative(
                _locator(),
                kind="poster",
                recipe_version=1,
                expected_sha256="0" * 64,
                store=store,
                now=NOW,
                downloader=downloader,
            )
        )
    assert not raw_path.exists()
    with sqlite3.connect(tmp_path / "ledger.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_art_ledger").fetchone()[0] == 0
    assert not tuple((tmp_path / "sources").glob("poster/*/*"))

    missing_mime_path = tmp_path / "raw-no-mime"
    missing_mime_path.write_bytes(_image_bytes("PNG", (10, 15)))

    async def no_mime(*_args, **_kwargs):
        return DownloadedSource(missing_mime_path, "", _locator())

    with pytest.raises(SourceArtError, match="MIME"):
        asyncio.run(
            fetch_derivative(
                _locator(), kind="poster", recipe_version=1, store=store,
                now=NOW, downloader=no_mime,
            )
        )
    assert not missing_mime_path.exists()


def test_v2_enrich_route_is_hidden_without_secret_and_uses_raw_strict_json(monkeypatch):
    import main

    payload = {
        "schema": CONTRACT_SCHEMA,
        "version": CONTRACT_VERSION,
        "media": {"media_type": "movie", "tmdb_id": 11, "imdb_id": "tt0133093"},
        "locales": ["en"],
        "titles_by_locale": {"en": "The Matrix"},
        "canonical_configs": [_art_only_config()],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()

    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", "")
    client = TestClient(main.app)
    assert client.post("/v2/enrich", content=body).status_code == 404
    for method in ("GET", "PUT", "DELETE", "OPTIONS", "HEAD"):
        assert client.request(method, "/v2/enrich").status_code == 404

    secret = b"route-secret"
    monkeypatch.setattr(main._cfg, "POSTERSPLUS_BINGECAT_REQUEST_SECRET", secret.decode())
    monkeypatch.setattr(main, "_V2_NONCE_STORE", MemoryNonceStore())
    monkeypatch.setattr(main, "_V2_SOURCE_STORE", object())
    monkeypatch.setattr(main, "_HTTP_CLIENT", object())
    result = EnrichmentResult(
        schema=CONTRACT_SCHEMA,
        version=CONTRACT_VERSION,
        media=MediaIdentity(media_type="movie", tmdb_id=11, imdb_id="tt0133093"),
        evaluated_at=NOW,
        titles_by_locale={"en": "The Matrix"},
    )
    mocked = AsyncMock(return_value=result)
    monkeypatch.setattr(main, "enrich_v2", mocked)

    timestamp = int(datetime.now(timezone.utc).timestamp())
    headers = build_auth_headers(
        method="POST", path="/v2/enrich", body=body, request_id=uuid4(),
        timestamp=timestamp, secret=secret, caller="bingecat", audience="postersplus",
    )
    response = client.post("/v2/enrich", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["schema"] == CONTRACT_SCHEMA
    assert mocked.await_count == 1

    bad_payload = {**payload, "media": {**payload["media"], "tmdb_id": "11"}}
    bad_body = json.dumps(bad_payload, separators=(",", ":")).encode()
    bad_headers = build_auth_headers(
        method="POST", path="/v2/enrich", body=bad_body, request_id=uuid4(),
        timestamp=timestamp, secret=secret, caller="bingecat", audience="postersplus",
    )
    assert client.post("/v2/enrich", content=bad_body, headers=bad_headers).status_code == 422

    mocked.side_effect = UnsupportedPresetVersion("unsupported preset version")
    unsupported_headers = build_auth_headers(
        method="POST", path="/v2/enrich", body=body, request_id=uuid4(),
        timestamp=timestamp, secret=secret, caller="bingecat", audience="postersplus",
    )
    unsupported = client.post("/v2/enrich", content=body, headers=unsupported_headers)
    assert unsupported.status_code == 409
    assert unsupported.json()["detail"] == "unsupported_preset_version"

    mocked.side_effect = SourceArtUnavailable()
    unavailable_headers = build_auth_headers(
        method="POST", path="/v2/enrich", body=body, request_id=uuid4(),
        timestamp=timestamp, secret=secret, caller="bingecat", audience="postersplus",
    )
    unavailable = client.post(
        "/v2/enrich", content=body, headers=unavailable_headers
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "source_art_unavailable"}
