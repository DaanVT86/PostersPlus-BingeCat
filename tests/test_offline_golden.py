from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from integration_contract import FactProvenance, NormalizedFactsEnvelope, ProviderRating
from offline_golden import (
    GoldenFixtureError,
    _parity_projection,
    export_fixture,
    load_cases,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _write_image(path: Path, kind: str, color: tuple[int, ...]) -> None:
    if kind == "poster":
        image = Image.new("RGB", (900, 1350), color)
        image.save(path, format="JPEG", quality=90, optimize=False, progressive=False, subsampling=0)
    elif kind == "backdrop":
        image = Image.new("RGB", (1600, 900), color)
        image.save(path, format="JPEG", quality=90, optimize=False, progressive=False, subsampling=0)
    else:
        image = Image.new("RGBA", (700, 180), color)
        image.save(path, format="PNG", optimize=False, compress_level=9)


def _fixture(root: Path) -> None:
    case = root / "title-1"
    case.mkdir(parents=True)
    candidates = []
    for index, color in enumerate(((10, 20, 30), (20, 30, 40), (30, 40, 50))):
        filename = f"poster-{index}.jpg"
        _write_image(case / filename, "poster", color)
        candidates.append(
            {
                "kind": "poster",
                "path": filename,
                "locale": "en" if index == 0 else "neutral",
                "locator": {
                    "provider": "tmdb",
                    "url": f"https://image.tmdb.org/t/p/w500/poster-{index}.jpg",
                },
                "vote_count": index + 1,
                "vote_average": float(index + 1),
            }
        )
    for index, color in enumerate(((40, 50, 60), (50, 60, 70))):
        filename = f"backdrop-{index}.jpg"
        _write_image(case / filename, "backdrop", color)
        candidates.append(
            {
                "kind": "backdrop",
                "path": filename,
                "locale": "neutral",
                "locator": {
                    "provider": "tmdb",
                    "url": f"https://image.tmdb.org/t/p/w1280/backdrop-{index}.jpg",
                },
                "vote_count": index + 1,
                "vote_average": float(index + 1),
            }
        )
    filename = "logo.png"
    _write_image(case / filename, "logo", (0, 0, 0, 0))
    candidates.append(
        {
            "kind": "logo",
            "path": filename,
            "locale": "en",
            "locator": {
                "provider": "tmdb",
                "url": "https://image.tmdb.org/t/p/original/logo.png",
            },
            "vote_count": 1,
            "vote_average": 1.0,
        }
    )
    (case / "inputs.json").write_text(
        json.dumps(
            {
                "id": "title-1",
                "media_id": 101,
                "tmdb_kind": "movie",
                "tmdb_id": 11,
                "title": "Example Title",
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )


def _production_facts(path: Path) -> None:
    facts = NormalizedFactsEnvelope(
        values={"genre": "Sci-Fi"},
        provenance=(
            FactProvenance(
                fields=("genre",),
                source="bingecat",
                observed_at=NOW - timedelta(days=1),
                checked_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(days=7),
            ),
        ),
    )
    rating = ProviderRating(
        provider="imdb",
        score=8.0,
        scale=10.0,
        normalized_score=80.0,
        vote_count=123,
        source="bingecat",
        observed_at=NOW - timedelta(days=1),
        checked_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=7),
    )
    row = {
        "media_id": 101,
        "payload_json": {
            "evaluated_at": NOW.isoformat(),
            "facts": facts.model_dump(mode="json"),
            "ratings": [rating.model_dump(mode="json")],
            "source_art": [],
            "titles_by_locale": {"en": "Example Title."},
        },
        "source_art_json": [],
        "content_hash": "a" * 64,
        "renderer_revision": "b" * 64,
    }
    path.write_text(json.dumps([row]), encoding="utf-8")


def test_offline_golden_uses_real_selection_normalization_and_render_bytes(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _fixture(fixtures)
    artifact_dir = tmp_path / "out-arm"
    detector = lambda image, **_kwargs: False

    first = export_fixture(
        fixtures,
        artifact_dir=artifact_dir,
        require_real_ocr=False,
        detector=detector,
    )
    second = export_fixture(
        fixtures,
        require_real_ocr=False,
        detector=detector,
    )

    assert len(load_cases(fixtures)) == 1
    case = first["cases"][0]
    assert first["presets"] == ["clean-notch@4", "prestige@3", "minimalist@4"]
    assert case["calls"] == {"materialize": 3, "tmdb": 1}
    assert [item["kind"] for item in case["materialization_order"]] == [
        "poster",
        "backdrop",
        "logo",
    ]
    assert len(case["normalized_artifacts"]) == 3
    assert len(case["renders"]) == 3
    assert all(len(item["content_sha256"]) == 64 for item in case["renders"])
    assert all(
        (artifact_dir / item["evidence_file"]).is_file()
        for item in case["normalized_artifacts"] + case["renders"]
    )
    assert not list(fixtures.rglob("*.sqlite"))
    assert not list(artifact_dir.rglob("*.sqlite"))
    assert _parity_projection(first) == _parity_projection(second)


def test_offline_golden_uses_strict_production_facts_overlay_and_root_manifest(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _fixture(fixtures)
    case = fixtures / "title-1"
    manifest = json.loads((case / "inputs.json").read_text(encoding="utf-8"))
    for candidate in manifest["candidates"]:
        candidate["path"] = f"title-1/{candidate['path']}"
    (fixtures / "inputs.json").write_text(json.dumps([manifest]), encoding="utf-8")
    (case / "inputs.json").unlink()
    facts_path = fixtures / "production-facts.json"
    _production_facts(facts_path)

    fixture = export_fixture(
        fixtures,
        production_facts=facts_path,
        require_real_ocr=False,
        detector=lambda image, **_kwargs: False,
    )

    record = fixture["cases"][0]
    assert record["media_id"] == 101
    assert record["title"] == "Example Title."
    assert record["media"]["tmdb_id"] == 11
    assert record["evaluated_at"] == NOW.isoformat()
    assert record["enrich"]["facts"] == json.loads(
        facts_path.read_text(encoding="utf-8")
    )[0]["payload_json"]["facts"]
    assert len(record["enrich"]["ratings"]) == 1
    assert fixture["production_facts_sha256"]


def test_media_id_is_required_for_production_join_but_optional_for_legacy_fixture(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _fixture(fixtures)
    case_inputs = fixtures / "title-1" / "inputs.json"
    payload = json.loads(case_inputs.read_text(encoding="utf-8"))
    payload.pop("media_id")
    case_inputs.write_text(json.dumps(payload), encoding="utf-8")
    assert load_cases(fixtures)[0].media_id == payload["tmdb_id"]

    facts_path = fixtures / "production-facts.json"
    _production_facts(facts_path)
    with pytest.raises(
        GoldenFixtureError,
        match="media_id is required when production facts are supplied",
    ):
        load_cases(fixtures, production_facts=facts_path)
