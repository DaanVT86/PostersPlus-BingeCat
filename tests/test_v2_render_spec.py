import hashlib
import json

import pytest

from render_spec import canonicalize_config, compile_requirements


def test_canonical_spec_uses_the_v2_contract_schema_and_version():
    spec = canonicalize_config({})

    assert spec.schema == "bingecat_postersplus_v2"
    assert spec.version == 1
    assert json.loads(spec.canonical_json())["schema"] == "bingecat_postersplus_v2"
    assert json.loads(spec.canonical_json())["version"] == 1


def test_canonicalize_normalizes_legacy_aliases_and_values():
    spec = canonicalize_config(
        {
            "rating_mode": "5",
            "show_award_sash": "NO",
            "top_vignette": "true",
            "top_gradient_height": "0.2500",
            "top_gradient_opacity": "128",
            "rating_text_color": "#AbC",
            "sash_priority": "trending,structural,-mini_series,wins,trending,unknown",
        }
    )

    assert spec.rating_display_mode == 5
    assert spec.show_award_sash is False
    assert spec.top_gradient == "high"
    assert spec.top_gradient_height == 0.25
    assert spec.top_gradient_opacity == pytest.approx(128 / 255)
    assert spec.rating_text_color == "#aabbcc"
    assert spec.sash_priority == ("trending", "short_film", "binge_ready", "wins")


def test_canonicalize_clamps_render_allocation_inputs():
    spec = canonicalize_config(
        {
            "rating_display_mode": 5,
            "top_gradient": "custom",
            "top_gradient_height": 99999,
            "top_gradient_opacity": -10,
            "bottom_gradient": "custom",
            "bottom_gradient_height": -9,
            "bottom_gradient_opacity": 99999,
        }
    )

    assert spec.rating_display_mode == 5
    assert spec.top_gradient_height == 1.0
    assert spec.top_gradient_opacity == 0.0
    assert spec.bottom_gradient_height == 0.0
    assert spec.bottom_gradient_opacity == 1.0


@pytest.mark.parametrize(
    ("requested", "expected"),
    (("pt-BR", "pt"), ("NL_nl", "nl"), ("de-DE", "de"), ("es-419", "es"), ("fr", "en"), ("x" * 10_000, "en")),
)
def test_logo_language_is_limited_to_supported_locales_with_english_fallback(requested, expected):
    assert canonicalize_config({"logo_language": requested}).logo_language == expected


@pytest.mark.parametrize("non_finite", ("inf", "-inf", "nan"))
def test_integer_normalizers_default_non_finite_spellings(non_finite):
    spec = canonicalize_config(
        {
            "badge_height": non_finite,
            "score_glow_alpha": non_finite,
            "top_gradient_height": non_finite,
            "bottom_gradient_opacity": non_finite,
        }
    )

    assert spec.badge_height == 20
    assert spec.score_glow_alpha == 40
    assert spec.top_gradient_height == 0.0
    assert spec.bottom_gradient_opacity == 0.0


def test_canonical_identity_ignores_unknown_and_secret_fields():
    base = canonicalize_config({"rating_display_mode": 5, "top_gradient_height": 0.4})
    noisy = canonicalize_config(
        {
            "rating_display_mode": 5,
            "top_gradient_height": 0.4,
            "unknown": "ignored",
            "tmdb_key": "secret",
            "mdblist_key": "secret",
            "access_key": "secret",
        }
    )

    assert noisy == base
    assert noisy.canonical_json() == base.canonical_json()
    assert noisy.sha256() == hashlib.sha256(noisy.canonical_json().encode()).hexdigest()
    assert "secret" not in noisy.canonical_json()


def test_requirements_are_gated_by_visible_features():
    hidden = canonicalize_config(
        {
            "rating_display_mode": 0,
            "sash_mode": "hidden",
            "use_original_art": True,
            "badge_display_mode": 0,
        }
    )
    assert compile_requirements(hidden).ratings is False
    assert compile_requirements(hidden).awards is False
    assert compile_requirements(hidden).logo is False
    assert compile_requirements(hidden).ocr is False
    assert compile_requirements(hidden).fallback_art is False
    assert compile_requirements(hidden).quality is False


def test_custom_configs_reject_out_of_scope_quality_badges():
    with pytest.raises(ValueError, match="badge_display_mode"):
        canonicalize_config({"badge_display_mode": 5})
