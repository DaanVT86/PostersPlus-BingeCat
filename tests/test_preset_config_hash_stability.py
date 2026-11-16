"""Config hashes of published presets and custom configs survive a renderer bump.

BingeCat serves the previous renderer revision's poster while a new one is
rendered, matching on (media, config_hash, locale).  A sync that changed these
hashes (a new upstream option filled into canonical JSON) would make every
variant miss its old poster.  Values recorded on fork ``dev`` 8fa6cf9 (R1).
"""
import pytest

import v2_render
from preset_registry import get_preset, list_public_presets
from render_spec import canonicalize_config

PORTRAIT = {
    "clean-notch@4": "9fdbe43272939dd4e189d1ddfc1cda6cae1940588c82b18c2619ab052afa3db6",
    "prestige@3": "9887f7f9cde5ab44965d71c858e3c37c8450a6e05231a37dbde6f21dce3fce1c",
    "minimalist@4": "c244df47e3813bf8551e7d5d770ded33260f77dda7faef3b2cb9d3d602ac99d3",
}
LANDSCAPE = {
    "clean-notch@4": "bd7e3ea8c451134e34be2298d28a4c8a8c8e7d4391bdcf7007dfe19e445266b8",
    "prestige@3": "c88bf3615574634e7b03e249eb2a36e8c3d86ef7767b4f76dcf00e14bac98812",
    "minimalist@4": "f48e780b9baeade28cc25d4567664890a3f9ef960b7c1eb5cb9b29ee674fe251",
}
CUSTOM = {
    "05939d1af50d11745c03bae29022d1380e30b2bea1c965eca22694340824d642": {},
    "6b6b78954a15cdffd259029f23b70577a3f653d27c4dd20c9163df153481eab3": {
        "sash_mode": "notch", "sash_badge_pos": "left", "sash_badge_pad": 0.8,
        "sash_priority": "wins,festival,trending",
    },
    "482da1853d31793858aad4a0cea43a3c4565533e43db8ed82be0a5171f61c918": {
        "rating_display_mode": 2, "rating_badges": "imdb,tomatoes", "rating_badge_style": "mono",
    },
    "8bbbbfda4320a04bc18dfa5d055024d279b505c0a927879af5193c8739e176a0": {
        "rating_display_mode": 3, "minimalist_append_mode": 3, "meta_order": "year,genre,rating",
    },
    "abdbf782c083b7f3bb7be9f4addbaa338200d97a44b5141665eb4c15d58f245f": {
        "rating_display_mode": 4, "bar_append": "second_rating", "release_status_dates": True,
    },
}


def test_portrait_catalog_hashes_are_unchanged():
    catalog = {}
    for metadata in list_public_presets():
        assert get_preset(metadata.ref).config.sha256() == metadata.config_sha256
        catalog[metadata.ref] = metadata.config_sha256
    assert {ref: catalog.get(ref) for ref in PORTRAIT} == PORTRAIT


def test_landscape_catalog_hashes_are_unchanged():
    presets = v2_render.landscape_preset_catalog()["presets"]
    catalog = {p.get("ref") or p.get("preset_ref"): p["config_sha256"] for p in presets}
    assert {ref: catalog.get(ref) for ref in LANDSCAPE} == LANDSCAPE


@pytest.mark.parametrize("expected,raw", CUSTOM.items())
def test_custom_config_hashes_are_unchanged(expected, raw):
    assert canonicalize_config(raw).sha256() == expected
