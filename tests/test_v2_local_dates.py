from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import i18n
from integration_contract import FactProvenance, NormalizedFacts, NormalizedFactsEnvelope
from render_spec import canonicalize_config, compile_requirements
from source_art import SourceArtStore
from test_v2_handoff import configured_handoff, _start, _csrf
import v2_render
from test_v2_render_endpoint import NOW, _bundle, _canonical_config, _facts, _install_art, _snapshot


def dated_snapshot(*, window="airing", status="airing", day="2026-07-12", expires=None, **changes):
    facts = _facts({"release_status": status})
    values = {
        "release_status": status,
        "upcoming_release_date": day,
        "upcoming_release_window": window,
    }
    provenance = facts.provenance + (
        FactProvenance(
            fields=("upcoming_release_date", "upcoming_release_window"),
            source="media_data.next_air" if window == "airing" else "media_data.mdblist_digital",
            observed_at=NOW - timedelta(hours=2),
            checked_at=NOW - timedelta(hours=1),
            expires_at=expires or NOW + timedelta(days=7),
        ),
    )
    return _snapshot(
        facts=NormalizedFactsEnvelope(
            values=NormalizedFacts.model_validate(values), provenance=provenance
        ),
        **changes,
    )


def projection(snapshot, **changes):
    cfg = _canonical_config(
        show_award_sash=True,
        sash_mode="sash",
        sash_priority=["airing", "cinema", "streaming", "production"],
        cinema_greyscale=False,
        **changes,
    )
    bundle = _bundle(cfg, snapshot)
    return v2_render.snapshot_visual_projection(
        snapshot, media=bundle.media, spec=canonicalize_config(cfg), locale="en"
    )


def test_flags_omit_false_and_preserve_fixed_configs_and_requirements():
    old = canonicalize_config({})
    assert "airing_dates" not in json.loads(old.canonical_json())
    assert "release_status_dates" not in json.loads(old.canonical_json())
    assert (
        old.canonical_json()
        == canonicalize_config(
            {"airing_dates": False, "release_status_dates": False}
        ).canonical_json()
    )
    for flag in ("airing_dates", "release_status_dates"):
        enabled = canonicalize_config({flag: True})
        assert json.loads(enabled.canonical_json())[flag] is True
        assert compile_requirements(old) == compile_requirements(enabled)


@pytest.mark.parametrize(
    ("window", "status", "day", "flag", "shown"),
    [
        ("airing", "airing", "2026-07-11", "airing_dates", True),
        ("airing", "airing", "2026-07-10", "airing_dates", False),
        ("airing", "airing", "2026-07-12", "release_status_dates", False),
        ("streaming", "cinema", "2026-07-12", "release_status_dates", True),
        ("streaming", "production", "2026-07-12", "release_status_dates", True),
        ("streaming", "streaming", "2026-07-12", "release_status_dates", False),
        ("streaming", "cinema", "2026-07-11", "release_status_dates", False),
        ("streaming", "airing", "2026-07-12", "release_status_dates", False),
        ("cinema", "production", "2026-07-12", "release_status_dates", True),
        ("cinema", "streaming", "2026-07-12", "release_status_dates", False),
    ],
)
def test_only_matching_future_window_is_a_visual_input(window, status, day, flag, shown):
    value = projection(dated_snapshot(window=window, status=status, day=day), **{flag: True})
    assert ("upcoming_release_date" in value["facts"]["values"]) is shown


def test_expired_date_group_falls_back_without_expiring_status():
    value = projection(dated_snapshot(expires=NOW - timedelta(minutes=1)), airing_dates=True)
    assert value["facts"]["values"] == {"release_status": "airing"}


@pytest.mark.parametrize(
    ("lang", "prefixes", "calendar"),
    [
        ("en", ("Cinema", "Streams", "Airs"), "07/12/2026"),
        ("nl", ("Bioscoop", "Streaming", "Uitzending"), "12-07-2026"),
        ("de", ("Kino", "Streaming", "Ausstrahlung"), "12.07.2026"),
        ("pt", ("Cinema", "Streaming", "Exibição"), "12/07/2026"),
        ("es", ("Cine", "Streaming", "Emisión"), "12/07/2026"),
    ],
)
def test_fixed_calendar_labels_use_five_translations(lang, prefixes, calendar):
    i18n.load_languages()
    for window, prefix in zip(("Cinema", "Streaming", "Airing"), prefixes):
        assert i18n.translate_sash(f"{window} · 2026-07-12", lang) == f"{prefix} · {calendar}"


def test_date_render_changes_webp_but_optouts_remain_byte_identical(tmp_path, monkeypatch):
    store = SourceArtStore(tmp_path / "source", tmp_path / "ledger.sqlite")
    art = _install_art(store)
    first = dated_snapshot(source_art=(art,))
    second = dated_snapshot(day="2026-07-13", source_art=(art,))
    cfg = _canonical_config(
        use_original_art=True,
        show_award_sash=True,
        sash_mode="sash",
        sash_priority=["airing"],
        cinema_greyscale=False,
    )
    old_bytes, _ = v2_render.render(_bundle(cfg, first), source_store=store)
    same_bytes, _ = v2_render.render(_bundle(cfg, second), source_store=store)
    assert old_bytes == same_bytes
    cfg["airing_dates"] = True
    seen = []
    engine = v2_render._composition_engine()
    original = engine.build_poster

    def capture(*args, **kwargs):
        seen.append(kwargs.get("sash_date_label"))
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "build_poster", capture)
    dated_bytes, _ = v2_render.render(_bundle(cfg, first), source_store=store)
    next_bytes, _ = v2_render.render(_bundle(cfg, second), source_store=store)
    assert seen == ["Airing · 2026-07-12", "Airing · 2026-07-13"]
    assert dated_bytes != old_bytes and next_bytes != dated_bytes


def test_strict_contract_rejects_partial_or_split_date_pair():
    with pytest.raises(ValueError, match="coherent"):
        NormalizedFactsEnvelope(values=NormalizedFacts(upcoming_release_date="2026-07-12"))
    value = dated_snapshot().facts.model_dump(mode="json")
    group = value["provenance"].pop()
    value["provenance"].extend(
        {**group, "fields": [name]} for name in ("upcoming_release_date", "upcoming_release_window")
    )
    with pytest.raises(ValueError, match="coherent"):
        NormalizedFactsEnvelope.model_validate(value)


@pytest.mark.parametrize("enabled", [False, True])
def test_existing_configurator_controls_save_date_optins(configured_handoff, enabled):
    client, callback, _store = configured_handoff
    assert _start(client).status_code == 303
    page = client.get("/bingecat/configurator")
    for name in ("release_status_dates", "airing_dates"):
        assert f'name="{name}"' in page.text
    form = {"csrf_token": _csrf(page.text), "badge_display_mode": "0"}
    if enabled:
        form.update(release_status_dates="true", airing_dates="true")
    saved = client.post("/bingecat/configurator/save", data=form, follow_redirects=False)
    assert saved.status_code == 303
    canonical = callback.save_calls[0]["canonical_config"]
    for name in ("release_status_dates", "airing_dates"):
        assert (canonical.get(name) is True) is enabled
        if not enabled:
            assert name not in canonical


def test_shared_local_date_hash_vectors():
    from integration_contract import ImmutableRenderSnapshot, MediaIdentity

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/postersplus_v2_local_date_vectors.json").read_text()
    )
    media = MediaIdentity.model_validate(fixture["media"])
    for vector in fixture["vectors"]:
        spec = canonicalize_config({**fixture["canonical_config"], **vector["config_overrides"]})
        snapshot = ImmutableRenderSnapshot.model_validate(
            fixture["snapshots"][vector["snapshot_ref"]]
        )
        assert (
            v2_render.snapshot_visual_projection(
                snapshot, media=media, spec=spec, locale=vector["locale"]
            )
            == vector["expected_projection"]
        ), vector["id"]
        assert (
            v2_render.canonical_snapshot_sha256(
                snapshot, media=media, spec=spec, locale=vector["locale"]
            )
            == vector["expected_sha256"]
        ), vector["id"]
