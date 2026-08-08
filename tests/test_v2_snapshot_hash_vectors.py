from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from integration_contract import ImmutableRenderSnapshot, MediaIdentity
from preset_registry import get_preset
from render_spec import canonicalize_config
from v2_render import canonical_snapshot_sha256, snapshot_visual_projection
from v2_render import V2_TRENDING_BROAD_FETCH_COUNT, V2_TRENDING_FETCH_COUNT


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "postersplus_v2_snapshot_hash_vectors.json"
)
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate fixture key: {key}")
        result[key] = value
    return result


def _load_fixture() -> dict:
    raw = FIXTURE.read_text(encoding="utf-8")
    document = json.loads(raw, object_pairs_hook=_unique_object)
    assert raw == json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    ) + "\n"
    return document


def test_shared_snapshot_hash_vectors_pin_every_visual_selection_rule() -> None:
    fixture = _load_fixture()
    assert fixture["schema"] == "postersplus_v2_snapshot_hash_vectors"
    assert fixture["version"] == 1
    assert fixture["canonical_json"] == {
        "allow_nan": False,
        "ensure_ascii": True,
        "separators": [",", ":"],
        "sort_keys": True,
    }

    media = MediaIdentity.model_validate(fixture["media"]["movie"])
    specs = {}
    for config_ref, value in fixture["configs"].items():
        canonical_config = value["canonical_config"]
        spec = canonicalize_config(canonical_config)
        assert json.loads(spec.canonical_json()) == canonical_config, config_ref
        if preset_ref := value.get("preset_ref"):
            preset = get_preset(preset_ref)
            assert preset.config == spec, preset_ref
            assert json.loads(preset.config.canonical_json()) == canonical_config
        specs[config_ref] = spec

    snapshots = {
        key: ImmutableRenderSnapshot.model_validate(value)
        for key, value in fixture["snapshots"].items()
    }
    hashes = {}
    vector_ids = {vector["id"] for vector in fixture["vectors"]}
    assert len(vector_ids) == len(fixture["vectors"]) == 33

    for vector in fixture["vectors"]:
        vector_id = vector["id"]
        spec = specs[vector["config_ref"]]
        snapshot = snapshots[vector["snapshot_ref"]]
        locale = vector["locale"]
        projection = snapshot_visual_projection(
            snapshot,
            media=media,
            spec=spec,
            locale=locale,
        )
        assert projection == vector["expected_projection"], vector_id
        digest = canonical_snapshot_sha256(
            snapshot,
            media=media,
            spec=spec,
            locale=locale,
        )
        assert digest == vector["expected_sha256"], vector_id
        hashes[vector_id] = digest

    for vector in fixture["vectors"]:
        if reference := vector.get("same_hash_as"):
            assert hashes[vector["id"]] == hashes[reference], vector["id"]


def test_fixed_preset_hashes_remain_frozen() -> None:
    assert {
        ref: get_preset(ref).config.sha256()
        for ref in ("clean-notch@1", "prestige@1", "minimalist@1")
    } == {
        "clean-notch@1": "d7b5d56bd01620b8860499bcc6a06597357762784dff160ffe0c8c50e01f63ee",
        "prestige@1": "6345f175f12b7df5c0bc7831e449ba5bff6f7cd956c96607006674c8b61a12a8",
        "minimalist@1": "b761456d72e8d8669b583f724e43cec753e587629d01111ba33d6af1c4faa04f",
    }


def test_v2_trending_thresholds_are_independent_of_legacy_environment(
    monkeypatch,
) -> None:
    import discovery

    monkeypatch.setattr(discovery._cfg, "TRENDING_FETCH_COUNT", 1)
    monkeypatch.setattr(discovery._cfg, "TRENDING_BROAD_FETCH_COUNT", 2)
    narrow = discovery.DiscoveryMeta(
        trending_rank=40,
        trending_fetch_count=V2_TRENDING_FETCH_COUNT,
        trending_broad_fetch_count=V2_TRENDING_BROAD_FETCH_COUNT,
    )
    broad = discovery.DiscoveryMeta(
        trending_rank=100,
        trending_fetch_count=V2_TRENDING_FETCH_COUNT,
        trending_broad_fetch_count=V2_TRENDING_BROAD_FETCH_COUNT,
    )
    assert discovery.pick_sash(narrow, ["trending"]) == ("#40 Today", "trending")
    assert discovery.pick_sash(broad, ["trending_broad"]) == (
        "#100 Today",
        "trending",
    )


def test_most_popular_rank_is_localized_and_changes_visible_snapshot_identity():
    import discovery

    spec = canonicalize_config({
        "rating_display_mode": 0,
        "show_award_sash": True,
        "sash_mode": "notch",
        "sash_priority": ["most_popular"],
        "use_original_art": True,
        "textless": True,
    })
    observed = NOW - timedelta(hours=1)
    facts_without_rank = ImmutableRenderSnapshot.model_validate({
        "evaluated_at": NOW,
        "titles_by_locale": {"en": "The Matrix", "nl": "The Matrix"},
        "facts": {"values": {}, "provenance": []},
    })
    facts_with_rank = ImmutableRenderSnapshot.model_validate({
        "evaluated_at": NOW,
        "titles_by_locale": {"en": "The Matrix", "nl": "The Matrix"},
        "facts": {
            "values": {"most_popular_rank": 4},
            "provenance": [{
                "fields": ["most_popular_rank"],
                "source": "bingecat.tmdb_most_popular",
                "observed_at": observed,
                "checked_at": observed,
                "expires_at": NOW + timedelta(days=1),
            }],
        },
    })
    without_hash = canonical_snapshot_sha256(
        facts_without_rank,
        media=MediaIdentity(media_type="movie", tmdb_id=603),
        spec=spec,
        locale="nl",
    )
    with_hash = canonical_snapshot_sha256(
        facts_with_rank,
        media=MediaIdentity(media_type="movie", tmdb_id=603),
        spec=spec,
        locale="nl",
    )
    assert with_hash != without_hash
    assert discovery.pick_sash(discovery.DiscoveryMeta(most_popular_rank=4), ["most_popular"]) == (
        "#4 Today",
        "trending",
    )
