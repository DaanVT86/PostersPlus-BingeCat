"""Graphic-badge / Blockbuster facts reported by v2 enrich on the R1 renderer."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import httpx

import box_office
import v2_render as render
from test_v2_enrich_art import (
    NOW,
    _art_only_config,
    _request,
    _runtime,
    _tmdb_metadata,
)
from v2_enrich import ProviderHooks, enrich

# The same pins as tests/test_v2_landscape.py: these facts must not move R1.
PORTRAIT_CODE_IDENTITY = {
    (3, 11): "6def692e5c3b7b88d5f61b5bb32fcd789d0ffbe537b7b30863d93a4bde6a3878",
    (3, 12): "980745e7b8a1367910f4d959d879a27bc6c4874300145df9534bb2ebeb49e0d8",
}
PORTRAIT_RENDERER_REVISION = "260504a9d0d0daa577848316c51518c5de78c4110642bac621cc92d3538f2b29"


async def _forbidden(*_args, **_kwargs):
    raise AssertionError("unneeded provider")


def _hooks(metadata, box_office_hook=None) -> ProviderHooks:
    async def tmdb(*_args, **_kwargs):
        return metadata

    return ProviderHooks(
        resolve_identity=_forbidden,
        fetch_ratings=_forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=_forbidden,
        fetch_release=_forbidden,
        fetch_tvdb=_forbidden,
        materialize_art=_forbidden,
        fetch_box_office=box_office_hook,
    )


def _year_config() -> dict:
    # Needs release_year, so enrich fetches TMDB metadata (the badge carrier).
    return {**_art_only_config(), "rating_display_mode": 1, "accent_bar_append_mode": 0}


def _enrich(metadata, *, media_type="movie", box_office_hook=None, known_facts=None):
    return asyncio.run(
        enrich(
            _request(_year_config(), media_type=media_type, known_facts=known_facts),
            NOW,
            runtime=_runtime(_hooks(metadata, box_office_hook)),
        )
    )


def test_badge_facts_stay_outside_the_renderer_revision(monkeypatch):
    manifest = "\n".join(render.RENDERER_REVISION_MANIFEST)
    assert "box_office" not in manifest and "v2_enrich" not in manifest
    assert render._RENDERER_COMPATIBILITY == {
        "a7ed1df863f30aab040b1ab6d005cf5918268fea5de6cf7ccb7b0652d099a9e5": PORTRAIT_RENDERER_REVISION
    }
    expected = PORTRAIT_CODE_IDENTITY.get(sys.version_info[:2])
    if expected is not None:
        monkeypatch.setattr(render, "_image_library_versions", lambda: {"pinned": "environment-independent"})
        assert render._compute_renderer_build_fingerprint() == expected


def test_series_networks_and_companies_come_from_the_metadata_call():
    metadata = replace(
        _tmdb_metadata(),
        media_type="series",
        networks=({"id": 49, "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png", "name": "HBO"},),
        companies=(
            {"id": 3268, "logo_path": "/../etc/passwd.png"},
            {"id": "bad", "logo_path": "/x.png"},
            {"id": 7429, "logo_path": None},
        ),
    )
    result = _enrich(metadata, media_type="series", box_office_hook=_forbidden)

    badge = result.badge_facts
    assert badge.values.model_dump(mode="json", exclude_none=True) == {
        "networks": [{"id": 49, "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png"}],
        # A malformed path keeps the id; a non-integer id is dropped.
        "companies": [{"id": 3268}, {"id": 7429}],
    }
    assert [(group.fields, group.source) for group in badge.provenance] == [
        (("companies", "networks"), "tmdb")
    ]
    # The R1 facts envelope, which the snapshot freezes, gains nothing.
    assert not {"networks", "companies", "is_blockbuster"} & set(
        result.facts.values.model_dump(exclude_none=True)
    )
    assert not result.partial


def test_movie_blockbuster_answer_absence_and_failure():
    seen = []

    async def hit(client, tmdb_id, release_date, tmdb_key):
        seen.append((tmdb_id, release_date, tmdb_key))
        return True

    async def unknown(*_args):
        return None

    async def broken(*_args):
        raise RuntimeError("tmdb down")

    result = _enrich(_tmdb_metadata(), box_office_hook=hit)
    assert seen == [("11", "1999-03-30", "tmdb-key")]
    assert result.badge_facts.values.is_blockbuster is True
    assert result.badge_facts.values.networks is None
    assert {group.source: group.fields for group in result.badge_facts.provenance} == {
        "tmdb": ("companies",),
        "tmdb_box_office": ("is_blockbuster",),
    }
    statuses = result.provider_statuses
    for hook in (unknown, broken):
        missing = _enrich(_tmdb_metadata(), box_office_hook=hook)
        # A list that can't be had is no answer: absent, not False, and silent.
        assert missing.badge_facts.values.is_blockbuster is None
        assert missing.provider_statuses == statuses
        assert not missing.partial


def test_real_box_office_lookup_through_mock_transport(monkeypatch):
    box_office._memo.clear()
    box_office._failed_at.clear()
    box_office._locks.clear()
    state: dict[str, str] = {}
    monkeypatch.setattr(box_office, "get_app_state", state.get)
    monkeypatch.setattr(box_office, "set_app_state", state.__setitem__)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/discover/movie"):
            assert request.url.params["primary_release_year"] == "1999"
            return httpx.Response(200, json={"results": [{"id": 11}, {"id": 12}]})
        tmdb_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"revenue": 900_000_000 if tmdb_id == 11 else 0})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = replace(_runtime(_hooks(_tmdb_metadata(), box_office.is_blockbuster)), client=client)
            return await enrich(_request(_year_config()), NOW, runtime=runtime)

    assert asyncio.run(run()).badge_facts.values.is_blockbuster is True
    assert any(key.startswith("box_office_top:1999:") for key in state)


def test_no_metadata_fetch_means_no_badge_facts_and_unchanged_response():
    result = asyncio.run(
        enrich(
            _request(_year_config(), known_facts={"release_year": 1999, "genre": "Sci-Fi"}),
            NOW,
            runtime=_runtime(ProviderHooks.all(_forbidden)),
        )
    )
    assert result.badge_facts is None
    assert "badge_facts" not in result.model_dump(mode="json")


def test_artwork_only_lane_reports_no_badge_facts():
    request = _request(_year_config()).model_copy(update={"artwork_only": True})
    result = asyncio.run(
        enrich(request, NOW, runtime=_runtime(_hooks(_tmdb_metadata(), _forbidden)))
    )
    assert result.badge_facts is None
