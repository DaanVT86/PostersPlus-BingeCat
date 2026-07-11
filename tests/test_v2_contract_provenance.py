from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from integration_contract import (
    ArtworkLocator,
    EnrichmentRequest,
    FactProvenance,
    NormalizedFacts,
    NormalizedFactsEnvelope,
    SourceArtReference,
)


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
DIGEST = "a" * 64
FIXTURE = Path(__file__).parent / "fixtures" / "postersplus_v2_contract_provenance.json"


def _locator(kind: str) -> ArtworkLocator:
    path = {
        "poster": "poster.jpg",
        "backdrop": "backdrop.jpg",
        "logo": "logo.png",
    }[kind]
    return ArtworkLocator(provider="tmdb", url=f"https://image.tmdb.org/t/p/original/{path}")


def _source_art(**changes) -> dict:
    values = {
        "source_art_id": "poster-a",
        "kind": "poster",
        "role": "textless_poster",
        "policy_key": "fallback.textless",
        "sha256": DIGEST,
        "byte_size": 1024,
        "mime": "image/jpeg",
        "recipe_version": 1,
        "locator": _locator("poster").model_dump(mode="json"),
        "locale": "neutral",
        "reconstructable": True,
        "observed_at": NOW - timedelta(minutes=2),
        "checked_at": NOW,
        "expires_at": NOW + timedelta(days=30),
        "textless_verified": True,
        "verification_recipe": "ppocr.v1",
        "verified_at": NOW - timedelta(minutes=1),
        "verification_source_digest": DIGEST,
    }
    values.update(changes)
    return values


def _provenance(*fields: str, source: str = "bingecat", expires_at=None) -> FactProvenance:
    return FactProvenance(
        fields=fields,
        source=source,
        observed_at=NOW - timedelta(hours=2),
        checked_at=NOW - timedelta(hours=1),
        expires_at=expires_at or NOW + timedelta(days=7),
    )


def test_source_art_role_policy_and_ocr_provenance_are_strict() -> None:
    reference = SourceArtReference.model_validate(_source_art())
    assert reference.role == "textless_poster"
    assert reference.verification_source_digest == reference.sha256

    with pytest.raises(ValidationError, match="verification_source_digest"):
        SourceArtReference.model_validate(
            _source_art(verification_source_digest="b" * 64)
        )
    with pytest.raises(ValidationError, match="textless_poster"):
        SourceArtReference.model_validate(_source_art(textless_verified=False))
    with pytest.raises(ValidationError, match="policy_key"):
        SourceArtReference.model_validate(_source_art(policy_key="original.primary"))
    with pytest.raises(ValidationError, match="kind"):
        SourceArtReference.model_validate(
            _source_art(kind="backdrop", locator=_locator("backdrop").model_dump(mode="json"))
        )


def test_legacy_source_art_defaults_only_when_selection_is_unambiguous() -> None:
    legacy_backdrop = _source_art(
        source_art_id="backdrop-a",
        kind="backdrop",
        locator=_locator("backdrop").model_dump(mode="json"),
        textless_verified=None,
        verification_recipe=None,
        verified_at=None,
        verification_source_digest=None,
    )
    legacy_backdrop.pop("role")
    legacy_backdrop.pop("policy_key")
    migrated = SourceArtReference.model_validate(legacy_backdrop)
    assert (migrated.role, migrated.policy_key) == (
        "fallback_backdrop",
        "fallback.backdrop",
    )

    for kind in ("poster", "logo"):
        ambiguous = _source_art(
            kind=kind,
            locator=_locator(kind).model_dump(mode="json"),
            textless_verified=None,
            verification_recipe=None,
            verified_at=None,
            verification_source_digest=None,
        )
        ambiguous.pop("role")
        ambiguous.pop("policy_key")
        with pytest.raises(ValidationError, match="ambiguous legacy source art"):
            SourceArtReference.model_validate(ambiguous)


def test_logo_policy_pins_priority_and_requested_language() -> None:
    logo = SourceArtReference.model_validate(
        _source_art(
            source_art_id="logo-en",
            kind="logo",
            role="logo",
            policy_key="logo.native_original.en",
            locator=_locator("logo").model_dump(mode="json"),
            locale="en",
            mime="image/png",
            textless_verified=None,
            verification_recipe=None,
            verified_at=None,
            verification_source_digest=None,
        )
    )
    assert logo.policy_key == "logo.native_original.en"
    for policy in ("logo.unknown.en", "logo.native_original.fr", "logo.en"):
        with pytest.raises(ValidationError, match="policy_key"):
            SourceArtReference.model_validate(
                {**logo.model_dump(mode="json"), "policy_key": policy}
            )


def test_fact_envelope_requires_exact_provenance_and_filters_expired_groups() -> None:
    envelope = NormalizedFactsEnvelope(
        values=NormalizedFacts(
            genre="Sci-Fi",
            award_wins=("Oscar",),
            is_cult=False,
        ),
        provenance=(
            _provenance("genre", "is_cult"),
            _provenance(
                "award_wins",
                source="mdblist",
                expires_at=NOW - timedelta(seconds=1),
            ),
        ),
    )
    active = envelope.active_values(NOW)
    assert active.genre == "Sci-Fi"
    assert active.is_cult is False
    assert active.award_wins is None
    assert tuple(group.source for group in envelope.active_provenance(NOW)) == ("bingecat",)

    with pytest.raises(ValidationError, match="missing provenance"):
        NormalizedFactsEnvelope(
            values=NormalizedFacts(genre="Sci-Fi", is_cult=False),
            provenance=(_provenance("genre"),),
        )
    with pytest.raises(ValidationError, match="duplicate fact provenance"):
        NormalizedFactsEnvelope(
            values=NormalizedFacts(genre="Sci-Fi"),
            provenance=(_provenance("genre"), _provenance("genre", source="tmdb")),
        )
    with pytest.raises(ValidationError, match="absent fact"):
        NormalizedFactsEnvelope(
            values=NormalizedFacts(),
            provenance=(_provenance("genre"),),
        )


def test_enrichment_request_fails_closed_for_unprovenanced_known_facts() -> None:
    base = {
        "schema": "bingecat_postersplus_v2",
        "version": 1,
        "media": {"media_type": "movie", "tmdb_id": 603},
        "locales": ["en"],
        "titles_by_locale": {"en": "The Matrix"},
        "preset_refs": ["minimalist@1"],
    }
    with pytest.raises(ValidationError):
        EnrichmentRequest.model_validate({**base, "known_facts": {"genre": "Sci-Fi"}})

    request = EnrichmentRequest.model_validate(
        {
            **base,
            "known_facts": {
                "values": {"genre": "Sci-Fi"},
                "provenance": [
                    {
                        "fields": ["genre"],
                        "source": "bingecat",
                        "observed_at": "2026-07-11T10:00:00Z",
                        "checked_at": "2026-07-11T11:00:00Z",
                        "expires_at": "2026-07-18T11:00:00Z",
                    }
                ],
            },
        }
    )
    assert request.known_facts.values.genre == "Sci-Fi"
    with pytest.raises((TypeError, ValidationError)):
        request.known_facts.provenance[0].fields += ("release_year",)


def test_shared_provenance_fixture_pins_canonical_wire_bytes() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    facts = NormalizedFactsEnvelope.model_validate(fixture["facts_envelope"])
    source_art = SourceArtReference.model_validate(fixture["source_art"])
    payload = {
        "facts_envelope": facts.model_dump(mode="json", exclude_none=True),
        "source_art": source_art.model_dump(mode="json", exclude_none=True),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == fixture["canonical_sha256"]
