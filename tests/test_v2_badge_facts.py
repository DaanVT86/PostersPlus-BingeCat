"""R2: graphic-badge and Blockbuster facts, from enrich requirements to pixels."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

import box_office
import graphic_badges
import main
import v2_render as render
from integration_contract import (
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    ImmutableRenderSnapshot,
    MediaIdentity,
    NormalizedFactsEnvelope,
    RenderInputBundle,
)
from render_spec import canonicalize_config
from source_art import SourceArtStore
from test_v2_enrich_art import NOW, _art_only_config, _request, _runtime, _tmdb_metadata
from test_v2_render_endpoint import _canonical_config, _install_art
from v2_enrich import ProviderHooks, _badge_facts, enrich

HBO = {"id": 49, "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png"}
MARVEL = {"id": 420, "logo_path": "/hUzeosd33nzE5MCNsZxCGEKTXaQ.png"}
A24 = {"id": 41077, "logo_path": "/1ZXsGaFPgrgS6ZZGS37AqD5uU12.png"}
NETFLIX_STUDIO = {"id": 178464, "logo_path": None}
NETFLIX = {"id": 213, "logo_path": "/wwemzKWzjKYJFfCeiB57q3r4Bcm.png"}
GROUPS = {"badge_display_mode": 7, "badge_group2": "tl:2:network,studio", "badge_group3": "br:1:cinema"}


@pytest.fixture(autouse=True)
def _logo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(graphic_badges, "LOGO_DIR", str(tmp_path / "company_logos"))
    graphic_badges._logo_mark.cache_clear()
    yield
    graphic_badges._logo_mark.cache_clear()


def _write_logo(logo: graphic_badges.Logo) -> None:
    path = Path(graphic_badges._logo_file(logo))
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (240, 80), (0, 0, 0, 0))
    image.paste((240, 240, 245, 255), (10, 10, 230, 70))
    image.save(path, format="PNG")


def _group(fields, source="tmdb", expires_at=None):
    return {
        "fields": sorted(fields),
        "source": source,
        "observed_at": NOW - timedelta(hours=2),
        "checked_at": NOW - timedelta(hours=1),
        "expires_at": expires_at or NOW + timedelta(days=7),
    }


def _envelope(*groups) -> NormalizedFactsEnvelope:
    """``groups``: (values, source[, expires_at]) per provenance group."""
    return NormalizedFactsEnvelope.model_validate({
        "values": {name: value for values, *_ in groups for name, value in values.items()},
        "provenance": [_group(values, *rest) for values, *rest in groups],
    })


# --- enrich ----------------------------------------------------------------------


async def _forbidden(*_args, **_kwargs):
    raise AssertionError("unneeded provider")


def _hooks(metadata, calls, *, release=None, box_office_hit=None, network_path=None, write_logos=True):
    async def tmdb(*_args, **_kwargs):
        calls.append("tmdb")
        return metadata

    async def fetch_release(*_args, **_kwargs):
        calls.append("release")
        return release

    async def top_grossing(_client, tmdb_id, release_date, _key):
        calls.append(("box_office", tmdb_id, release_date))
        return box_office_hit

    async def network_logo(_client, network_id, _key):
        calls.append(("network", network_id))
        return network_path

    async def ensure(_client, logo):
        calls.append(("logo", logo))
        if write_logos:
            _write_logo(logo)

    return ProviderHooks(
        resolve_identity=_forbidden,
        fetch_ratings=_forbidden,
        fetch_tmdb=tmdb,
        fetch_trending=_forbidden,
        fetch_release=fetch_release if release else _forbidden,
        fetch_tvdb=_forbidden,
        materialize_art=_forbidden,
        fetch_box_office=top_grossing if box_office_hit is not None else _forbidden,
        fetch_network_logo=network_logo if network_path else _forbidden,
        ensure_badge_logo=ensure,
    )


def _enrich(config, hooks, *, media_type="movie", known_facts=None):
    request = _request(config, media_type=media_type, known_facts=known_facts)
    return asyncio.run(enrich(request, NOW, runtime=_runtime(hooks)))


def _series_metadata():
    return replace(
        _tmdb_metadata(), media_type="series", tmdb_status="In Production",
        networks=(HBO,), companies=(MARVEL, {"id": "bad", "logo_path": "/x.png"}),
    )


def test_badge_slots_require_tmdb_badge_facts_release_status_and_logos():
    calls = []
    result = _enrich(
        {**_art_only_config(), **GROUPS},
        _hooks(_series_metadata(), calls, release="Production"),
        media_type="series",
    )
    facts = result.facts
    assert facts.values.model_dump(mode="json", include={"networks", "companies", "release_status"}) == {
        "networks": [HBO], "companies": [MARVEL], "release_status": "production",
    }
    sources = {name: group.source for group in facts.provenance for name in group.fields}
    assert sources["networks"] == sources["companies"] == "tmdb"
    network, studio = graphic_badges.make_logo("network", 49, HBO["logo_path"]), \
        graphic_badges.make_logo("company", 420, MARVEL["logo_path"])
    assert calls == ["tmdb", "release", ("logo", network), ("logo", studio)]
    assert not any("badge_logo" in status.missing_fields for status in result.provider_statuses)
    assert "badge_facts" not in result.model_dump(mode="json")


def test_a_logo_the_cache_cannot_get_is_reported_missing():
    calls = []
    result = _enrich(
        {**_art_only_config(), **GROUPS},
        _hooks(_series_metadata(), calls, release="Production", write_logos=False),
        media_type="series",
    )
    missing = [s for s in result.provider_statuses if "badge_logo" in s.missing_fields]
    assert [(s.provider, s.status) for s in missing] == [("source_art", "missing")]


def test_an_expired_badge_group_triggers_the_tmdb_fetch_and_is_replaced_whole():
    config = {**_art_only_config(), "badge_display_mode": 7, "badge_group2": "tl:2:network,studio"}
    stale = _envelope(({"networks": [{"id": 1, "logo_path": "/old.png"}], "companies": [MARVEL]},
                       "tmdb", NOW - timedelta(minutes=1))).model_dump(mode="json")
    calls = []
    result = _enrich(config, _hooks(_series_metadata(), calls), media_type="series", known_facts=stale)
    assert calls[0] == "tmdb"
    assert result.facts.values.model_dump(mode="json")["networks"] == [HBO]
    assert all(group.expires_at > NOW for group in result.facts.provenance)

    # Fresh known badge facts: no TMDB call; their logos are still ensured.
    fresh = _envelope(({"networks": [HBO], "companies": [MARVEL]},)).model_dump(mode="json")
    calls = []
    result = _enrich(config, _hooks(None, calls), media_type="series", known_facts=fresh)
    assert [call for call in calls if call == "tmdb"] == []
    assert [call[0] for call in calls] == ["logo", "logo"]


def test_film_streamer_network_and_blockbuster_are_enrich_facts():
    metadata = replace(_tmdb_metadata(), companies=(NETFLIX_STUDIO, A24))
    config = {**_art_only_config(), "badge_display_mode": 7, "badge_group2": "tl:2:network,studio",
              "show_award_sash": True, "sash_mode": "sash", "sash_priority": ["blockbuster"]}
    calls = []
    result = _enrich(config, _hooks(metadata, calls, box_office_hit=True,
                                    network_path=NETFLIX["logo_path"]))
    values = result.facts.values
    assert values.streamer_network.model_dump() == NETFLIX
    assert values.is_blockbuster is True and values.networks is None
    sources = {name: group.source for group in result.facts.provenance for name in group.fields}
    assert {name: sources[name] for name in ("companies", "streamer_network", "is_blockbuster")} == {
        "companies": "tmdb", "streamer_network": "tmdb", "is_blockbuster": "tmdb_box_office"}
    assert ("network", 213) in calls and ("box_office", "11", "1999-03-30") in calls
    assert ("logo", graphic_badges.make_logo("network", 213, NETFLIX["logo_path"])) in calls

    # Known companies naming a streamer studio but no streamer network: refetch.
    known = _envelope(({"companies": [NETFLIX_STUDIO], "is_blockbuster": False},)).model_dump(mode="json")
    calls = []
    result = _enrich(config, _hooks(metadata, calls, network_path=NETFLIX["logo_path"]),
                     known_facts=known)
    assert calls[0] == "tmdb" and ("network", 213) in calls
    assert result.facts.values.streamer_network.id == 213
    assert result.facts.values.is_blockbuster is False   # still fresh: kept, not re-asked


def test_specs_without_badges_fetch_and_report_no_badge_facts():
    metadata = replace(_tmdb_metadata(), companies=(NETFLIX_STUDIO, A24))
    calls = []
    config = {**_art_only_config(), "rating_display_mode": 1, "accent_bar_append_mode": 0}
    result = _enrich(config, _hooks(metadata, calls))
    assert calls == ["tmdb"]
    assert not {"networks", "companies", "streamer_network", "is_blockbuster"} & set(
        result.facts.values.model_dump(exclude_none=True))


def test_offprod_producer_helper_returns_a_facts_envelope():
    seen = []

    async def network_logo(_client, network_id, _key):
        seen.append(network_id)
        return NETFLIX["logo_path"]

    async def hit(*_args):
        return True

    runtime = SimpleNamespace(client=None, tmdb_key="k",
                              hooks=SimpleNamespace(fetch_box_office=hit, fetch_network_logo=network_logo))
    metadata = replace(_tmdb_metadata(), companies=(NETFLIX_STUDIO, A24))
    envelope = asyncio.run(_badge_facts(metadata, runtime, NOW))
    assert seen == [213]
    assert envelope.model_dump(mode="json", exclude_none=True)["values"] == {
        # exclude_none drops a null logo_path inside an entry too.
        "companies": [{"id": 178464}, A24], "streamer_network": NETFLIX, "is_blockbuster": True,
    }
    assert [(group.fields, group.source) for group in envelope.provenance] == [
        (("companies", "streamer_network"), "tmdb"), (("is_blockbuster",), "tmdb_box_office"),
    ]


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

    config = {**_art_only_config(), "show_award_sash": True, "sash_mode": "sash",
              "sash_priority": ["blockbuster"]}
    hooks = replace(_hooks(_tmdb_metadata(), []), fetch_box_office=box_office.is_blockbuster)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich(_request(config), NOW, runtime=replace(_runtime(hooks), client=client))

    assert asyncio.run(run()).facts.values.is_blockbuster is True
    assert any(key.startswith("box_office_top:1999:") for key in state)


# --- render ----------------------------------------------------------------------


def _bundle(config: dict, facts: NormalizedFactsEnvelope, *, media_type="movie", source_art=()):
    spec = canonicalize_config(config)
    media = MediaIdentity(media_type=media_type, tmdb_id=603)
    snapshot = ImmutableRenderSnapshot(
        evaluated_at=NOW, titles_by_locale={"en": "The Matrix"}, facts=facts, source_art=source_art,
    )
    return RenderInputBundle(
        schema=CONTRACT_SCHEMA, version=CONTRACT_VERSION, media=media, locale="en",
        canonical_config=config, config_sha256=spec.sha256(),
        snapshot_sha256=render.canonical_snapshot_sha256(snapshot, media=media, spec=spec, locale="en"),
        snapshot=snapshot,
    )


def _captured(monkeypatch, bundle) -> dict:
    seen = {}

    def build_poster(image, *_args, **kwargs):
        seen.update(kwargs)
        return image

    monkeypatch.setattr(main, "build_poster", build_poster)
    render.render(bundle)
    return seen


def _upcoming(window, days=10, source="media_data.next_air"):
    return ({"upcoming_release_date": (NOW + timedelta(days=days)).date().isoformat(),
             "upcoming_release_window": window}, source)


def test_render_projects_series_badges_from_facts(monkeypatch):
    config = _canonical_config(**GROUPS)
    facts = _envelope(({"networks": [HBO], "companies": [MARVEL], "release_status": "production"},),
                      _upcoming("airing"))
    network = graphic_badges.make_logo("network", 49, HBO["logo_path"])
    studio = graphic_badges.make_logo("company", 420, MARVEL["logo_path"])
    for logo in (network, studio):
        _write_logo(logo)
    seen = _captured(monkeypatch, _bundle(config, facts, media_type="series"))
    assert seen["badge_logos"] == (network, studio)
    assert seen["cinema_run"] == graphic_badges.CinemaRun("Production", (NOW + timedelta(days=10)).date())
    # The cinema badge's status and date reach neither the sash nor the greyscale.
    assert seen["discovery_meta"].release_status is None and "sash_date_label" not in seen


def test_render_projects_film_streamer_cinema_and_blockbuster(monkeypatch):
    config = _canonical_config(**GROUPS, show_award_sash=True, sash_mode="sash",
                               sash_priority=["blockbuster", "cult"])
    values = {"companies": [NETFLIX_STUDIO, A24], "streamer_network": NETFLIX, "release_status": "cinema"}
    facts = _envelope((values,), ({"is_blockbuster": True}, "tmdb_box_office"),
                      _upcoming("streaming", days=-1, source="media_data.mdblist_digital"))
    network = graphic_badges.make_logo("network", 213, NETFLIX["logo_path"])
    studio = graphic_badges.make_logo("company", 41077, A24["logo_path"])
    for logo in (network, studio):
        _write_logo(logo)
    seen = _captured(monkeypatch, _bundle(config, facts))
    assert seen["badge_logos"] == (network, studio)
    # Yesterday's home date is past: the disc shows the popcorn, no date.
    assert seen["cinema_run"] == graphic_badges.CinemaRun("Cinema", None)
    assert seen["discovery_meta"].is_blockbuster is True

    # A streamer network for another company list is no answer: no network logo.
    stale = _envelope(({**values, "streamer_network": {"id": 2552, "logo_path": "/x.png"}},))
    seen = _captured(monkeypatch, _bundle(config, stale))
    assert seen["badge_logos"] == (None, studio)


def test_a_needed_logo_missing_from_the_cache_is_a_conflict():
    config = _canonical_config(**GROUPS)
    facts = _envelope(({"networks": [HBO], "companies": [MARVEL]},))
    with pytest.raises(render.RenderConflict) as raised:
        render.render(_bundle(config, facts, media_type="series"))
    assert raised.value.code == "missing_badge_logo"


def test_stale_badge_facts_fail_freshness():
    config = _canonical_config(**GROUPS)
    facts = _envelope(({"companies": [MARVEL]}, "tmdb", NOW - timedelta(seconds=1)))
    with pytest.raises(render.RenderConflict) as raised:
        render.render(_bundle(config, facts))
    assert raised.value.code == "stale_fact_provenance"


def test_badges_draw_pixels(tmp_path):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = (_install_art(store),)
    config = _canonical_config(use_original_art=True, hide_genre=True, **GROUPS,
                               show_award_sash=True, sash_mode="sash", sash_priority=["blockbuster"])
    for logo in (graphic_badges.make_logo("network", 213, NETFLIX["logo_path"]),
                 graphic_badges.make_logo("company", 41077, A24["logo_path"])):
        _write_logo(logo)

    def pixels(*groups):
        return render.render(_bundle(config, _envelope(*groups), source_art=art), source_store=store)[0]

    bare = pixels(({"genre": "Drama"},))
    logos = pixels(({"companies": [NETFLIX_STUDIO, A24], "streamer_network": NETFLIX},))
    cinema = pixels(({"release_status": "cinema"},))
    blockbuster = pixels(({"is_blockbuster": True}, "tmdb_box_office"))
    assert len({bare, logos, cinema, blockbuster}) == 4
    assert pixels(({"is_blockbuster": False}, "tmdb_box_office")) == bare


def test_snapshot_hash_ignores_badge_facts_for_specs_that_do_not_draw_them():
    media = MediaIdentity(media_type="movie", tmdb_id=603)
    badge_values = {"companies": [NETFLIX_STUDIO, A24], "streamer_network": NETFLIX}
    with_badges = _envelope(({"genre": "Drama", "release_status": "cinema"}, "bingecat"),
                            (badge_values,), ({"is_blockbuster": True}, "tmdb_box_office"))
    without = _envelope(({"genre": "Drama", "release_status": "cinema"}, "bingecat"))

    def digest(config, facts):
        snapshot = ImmutableRenderSnapshot(evaluated_at=NOW, titles_by_locale={"en": "The Matrix"},
                                           facts=facts)
        return render.canonical_snapshot_sha256(
            snapshot, media=media, spec=canonicalize_config(config), locale="en")

    plain = _canonical_config(show_award_sash=True, sash_mode="sash", sash_priority=["cinema", "cult"],
                              badge_display_mode=3)
    assert digest(plain, with_badges) == digest(plain, without)
    for drawn in (_canonical_config(**GROUPS),
                  _canonical_config(show_award_sash=True, sash_mode="sash", sash_priority=["blockbuster"])):
        assert digest(drawn, with_badges) != digest(drawn, without)


# --- logo cache ------------------------------------------------------------------


def _png_bytes() -> bytes:
    import io

    buffer = io.BytesIO()
    image = Image.new("RGBA", (240, 80), (0, 0, 0, 0))
    image.paste((240, 240, 245, 255), (10, 10, 230, 70))
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _Client:
    def __init__(self, payload: bytes, barrier=None):
        self.payload, self.barrier, self.urls = payload, barrier, []

    async def get(self, url, timeout=None):
        self.urls.append(url)
        if self.barrier is not None:
            await asyncio.to_thread(self.barrier.wait)
        return SimpleNamespace(content=self.payload, raise_for_status=lambda: None)


def test_concurrent_logo_writers_each_publish_a_whole_file(monkeypatch):
    import os
    import threading

    payload, writers = _png_bytes(), 8
    logo = graphic_badges.make_logo("company", 420, MARVEL["logo_path"])
    client = _Client(payload, threading.Barrier(writers))
    published, real_replace = [], os.replace

    def replace_spy(src, dst):
        published.append((src, os.path.getsize(src)))
        real_replace(src, dst)

    monkeypatch.setattr(graphic_badges.os, "replace", replace_spy)
    threads = [threading.Thread(target=lambda: asyncio.run(graphic_badges.ensure_logo(client, logo)))
               for _ in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(published) == writers
    assert len({src for src, _ in published}) == writers           # one temp file per writer
    assert {size for _, size in published} == {len(payload)}       # never a partial publish
    assert Path(graphic_badges._logo_file(logo)).read_bytes() == payload
    assert [p.name for p in Path(graphic_badges.LOGO_DIR).iterdir()] == [
        Path(graphic_badges._logo_file(logo)).name]


def test_an_unreadable_cached_logo_is_a_conflict_and_is_repaired():
    config = _canonical_config(**GROUPS)
    facts = _envelope(({"companies": [MARVEL]},))
    logo = graphic_badges.make_logo("company", 420, MARVEL["logo_path"])
    path = Path(graphic_badges._logo_file(logo))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")                                          # a zero-byte publish
    with pytest.raises(render.RenderConflict) as raised:
        render.render(_bundle(config, facts))
    assert raised.value.code == "missing_badge_logo"
    assert graphic_badges._logo_mark(logo, 22) is None
    asyncio.run(graphic_badges.ensure_logo(_Client(_png_bytes()), logo))
    assert graphic_badges.logo_file_ok(logo)
    # The miss above was not memoised: the repaired logo is drawn at once.
    assert graphic_badges._logo_mark(logo, 22) is not None


def test_svg_logo_cache_name_carries_the_rasteriser_fingerprint(monkeypatch):
    import importlib.metadata

    svg = graphic_badges.make_logo("network", 213, "/netflix.svg")
    png = graphic_badges.make_logo("company", 420, MARVEL["logo_path"])
    graphic_badges.svg_raster_fingerprint.cache_clear()
    before = (graphic_badges._logo_file(svg), graphic_badges._logo_file(png))
    assert f".svg-{graphic_badges.svg_raster_fingerprint()}.png" in before[0]
    real_version = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version",
                        lambda dist: "0.0-test" if dist == "CairoSVG" else real_version(dist))
    graphic_badges.svg_raster_fingerprint.cache_clear()
    try:
        after = (graphic_badges._logo_file(svg), graphic_badges._logo_file(png))
    finally:
        monkeypatch.undo()
        graphic_badges.svg_raster_fingerprint.cache_clear()
    assert after[0] != before[0]                                   # another rasteriser, another file
    assert after[1] == before[1]                                   # raster logos: immutable by path
