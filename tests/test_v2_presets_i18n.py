import json
from pathlib import Path

from i18n import normalize_locale, resolve_locale_chain
from preset_registry import BINGECAT_PRESET_REGISTRY, get_preset, list_public_presets


def test_bingecat_presets_are_immutable_and_have_pinned_hashes():
    expected_hashes = {
        "clean-notch@1": "1a6f6730bf25ff39d140e48af1ba802ddcd14e84fe76481bca5216a2730960f5",
        "prestige@1": "26281fc5ac686bc7522051188083c1d16c20c5f075e4e35e270b70e174ffcde0",
        "minimalist@1": "e9017a4760e549645910ec439bebe6a78611ea85e0d93b4c306881b52b08bebe",
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
