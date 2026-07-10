import json
from pathlib import Path

from i18n import normalize_locale, resolve_locale_chain
from preset_registry import BINGECAT_PRESET_REGISTRY, get_preset, list_public_presets


def test_bingecat_presets_are_immutable_and_have_pinned_hashes():
    expected_hashes = {
        "clean-notch@1": "dc9d4a268a5786a2f5a69b15134edb86ac07e5bad3a58a772654f3c84eb658c2",
        "prestige@1": "6e758a59565ce0a944789da9065341baf89e76f75ad9ed90a395e70542f67463",
        "minimalist@1": "e755ccdea405c6f41b1aa59bf4cae66e34b5824a80c2d0017fc9c302d4895416",
    }

    assert set(BINGECAT_PRESET_REGISTRY) == set(expected_hashes)
    assert [metadata.ref for metadata in list_public_presets()] == list(expected_hashes)
    for ref, expected_hash in expected_hashes.items():
        preset = get_preset(ref)
        assert preset.ref == ref
        assert preset.config.sha256() == expected_hash
        assert preset.config.badge_display_mode == 0
        assert "access_key" not in preset.config.canonical_json()
        assert "key" not in preset.metadata().to_dict()


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
