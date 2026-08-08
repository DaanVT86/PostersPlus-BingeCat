import json
from pathlib import Path

import pytest

from i18n import normalize_locale, resolve_locale_chain
from preset_registry import BINGECAT_PRESET_REGISTRY, get_preset, list_public_presets
from render_spec import canonicalize_config


def test_bingecat_presets_are_immutable_and_have_pinned_hashes():
    expected_hashes = {
        "clean-notch@1": "d7b5d56bd01620b8860499bcc6a06597357762784dff160ffe0c8c50e01f63ee",
        "prestige@1": "6345f175f12b7df5c0bc7831e449ba5bff6f7cd956c96607006674c8b61a12a8",
        "minimalist@1": "b761456d72e8d8669b583f724e43cec753e587629d01111ba33d6af1c4faa04f",
        "clean-notch@2": "c2f6e00eb4d09b0d55135499a72e89f3dce91f449b4eec91a3143223ecc22664",
        "minimalist@2": "65c724c240e1b297115454d7c48d3b145e79e8634d7ee172338f41384805d2b4",
        "clean-notch@3": "4f27590eecd267fb4f8e1e9ba692ac1e4f50c46cf81542321a3870bfd4577656",
        "prestige@2": "980a4e5f7feabd8c5e5044319a38deafc9f9e9dbd8dc2215d785417f35b0b56b",
        "minimalist@3": "cdb4a6967d738fe632ef7fc3fe4961c1b1e27b64f4067ec72062cf96d8dcce7b",
    }

    assert set(BINGECAT_PRESET_REGISTRY) == set(expected_hashes)
    assert [metadata.ref for metadata in list_public_presets()] == [
        "clean-notch@3",
        "prestige@2",
        "minimalist@3",
    ]
    for ref, expected_hash in expected_hashes.items():
        preset = get_preset(ref)
        assert preset.ref == ref
        assert preset.config.sha256() == expected_hash
        assert preset.config.badge_display_mode == 0
        assert "access_key" not in preset.config.canonical_json()
        assert "key" not in preset.metadata().to_dict()


def test_validated_preset_registry_cannot_be_replaced_or_mutated():
    clean_notch = get_preset("clean-notch@1")

    with pytest.raises(TypeError):
        BINGECAT_PRESET_REGISTRY["clean-notch@1"] = get_preset("prestige@1")

    assert BINGECAT_PRESET_REGISTRY["clean-notch@1"] is clean_notch
    assert get_preset("clean-notch@1") is clean_notch


def test_minimalist_preset_matches_the_supplied_v1_visual_config():
    config = get_preset("minimalist@1").config

    assert config.rating_display_mode == 3
    assert config.score_color_mode == 2
    assert config.minimalist_append_mode == 3
    assert config.sash_length_ratio == 1.20
    assert config.sash_height_ratio == 0.135
    assert config.sash_priority == (
        "wins", "gg_wins", "festival", "pic_noms", "gg_noms", "studio",
        "director", "cast", "trending", "new_season", "returning", "premiere",
        "just_added", "season_finale", "cult", "foreign", "new_release",
        "metacritic", "true_story", "short_film", "mini_series", "binge_ready",
        "trending_broad",
    )
    assert config.sash_exclusions == (
        "cinema", "streaming", "physical", "production", "ended", "cancelled", "airing",
    )
    assert config.release_status_cinema_only is False
    assert config.badge_display_mode == 0
    assert "access_key" not in config.canonical_json()


def test_fixed_presets_keep_their_source_visuals_with_quality_disabled():
    clean_notch = get_preset("clean-notch@1").config
    prestige = get_preset("prestige@1").config

    assert clean_notch.rating_display_mode == 5  # legacy Clean Notch mode 2 → Dual Clean
    assert clean_notch.sash_mode == "notch"
    assert clean_notch.badge_display_mode == 0
    assert prestige.rating_display_mode == 1  # legacy Prestige rating bar
    assert prestige.accent_bar_font_size_ratio == 0.08
    assert prestige.badge_display_mode == 0


def test_latest_presets_apply_bingecat_layout_policy():
    clean_notch = get_preset("clean-notch@2").config
    minimalist = get_preset("minimalist@2").config
    expected_movie_weights = dict(get_preset("prestige@1").config.movie_weights)
    expected_tv_weights = dict(get_preset("prestige@1").config.tv_weights)

    assert clean_notch.bottom_gradient == "medium"
    assert clean_notch.rating_display_mode == 2
    assert clean_notch.numeric_score_font_size_ratio == 0.08
    assert clean_notch.score_out_of_10 is True
    assert clean_notch.logo_max_w_ratio == 0.80
    assert clean_notch.logo_bottom_ratio == 0.19
    assert clean_notch.logo_bottom_anchor is True
    assert clean_notch.sash_mode == "notch"
    assert clean_notch.sash_badge_size_w == 0.50
    assert clean_notch.sash_badge_size_h == 1.30
    assert clean_notch.sash_badge_font_ratio == 0.55
    assert clean_notch.sash_badge_frost_opacity == 1.0
    assert minimalist.minimalist_mode_font_size_ratio == 0.065
    assert minimalist.show_award_sash is False
    assert minimalist.sash_mode == "hidden"

    for config in (clean_notch, minimalist):
        assert dict(config.movie_weights) == expected_movie_weights
        assert dict(config.tv_weights) == expected_tv_weights
        assert config.fallback_to_imdb is True


def test_latest_presets_put_most_popular_first_and_legacy_refs_remain_stable():
    for ref in ("clean-notch@3", "prestige@2", "minimalist@3"):
        assert get_preset(ref).config.sash_priority[0] == "most_popular"
    assert get_preset("clean-notch@2").config.sash_priority[0] == "wins"
    assert get_preset("prestige@1").config.sash_priority[0] == "wins"
    assert get_preset("minimalist@2").config.sash_mode == "hidden"


def test_custom_palette_is_canonical_only_while_visually_active():
    inactive = canonicalize_config(
        {"score_color_mode": 2, "score_custom_palette": "80:ABCDEF,0:111111"}
    )
    active = canonicalize_config(
        {"score_color_mode": 3, "score_custom_palette": "80:ABCDEF,0:111111"}
    )
    assert inactive.score_custom_palette is None
    assert "score_custom_palette" not in inactive.canonical_json()
    assert active.score_custom_palette == "0:#111111,80:#abcdef"
    assert '"score_custom_palette":"0:#111111,80:#abcdef"' in active.canonical_json()


def test_primary_client_is_reduced_to_visual_edge_insets():
    desktop = canonicalize_config({"primary_client": "stremio_desktop_web"})
    explicit = canonicalize_config(
        {
            "primary_client": "stremio_desktop_web",
            "bar_bottom_inset": 0.02,
            "sash_badge_inset": -0.01,
        }
    )
    assert desktop.bar_bottom_inset == 0.007
    assert desktop.sash_badge_inset == 0.004
    assert explicit.bar_bottom_inset == 0.02
    assert explicit.sash_badge_inset == -0.01
    assert "primary_client" not in desktop.canonical_json()


def test_locale_normalization_and_deterministic_fallback_chain():
    assert normalize_locale("pt-BR") == "pt"
    assert normalize_locale("NL-nl") == "nl"
    assert normalize_locale("de_DE") == "de"
    assert normalize_locale("es-419") == "es"
    assert normalize_locale("en-US") == "en"
    assert normalize_locale("??") == "en"
    assert resolve_locale_chain("de-DE", {"en", "pt", "nl", "de", "es"}) == ["de", "en"]
    assert resolve_locale_chain("fr-CA", {"en", "pt"}) == ["en"]


def test_five_supported_locale_files_have_complete_english_key_parity():
    languages_dir = Path(__file__).parents[1] / "languages"
    english = json.loads((languages_dir / "en.json").read_text(encoding="utf-8"))
    for code in ("en", "pt", "nl", "de", "es"):
        translation = json.loads((languages_dir / f"{code}.json").read_text(encoding="utf-8"))
        assert translation["code"] == code
        for section in ("genreLabels", "sashLabels"):
            assert set(translation[section]) == set(english[section])
            assert all(translation[section][key] for key in english[section])
