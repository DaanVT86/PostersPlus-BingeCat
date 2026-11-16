"""Values a request controls that become cache keys or upstream calls are
bounded: every distinct one is its own TMDB lookup, metadata row, render and
stored composite."""
import os
import unittest

import main

LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "languages")


class LanguageTests(unittest.TestCase):
    def test_every_shipped_language_and_region_form_is_kept(self):
        codes = [f[:-5] for f in os.listdir(LANG_DIR) if f.endswith(".json")]
        codes += ["en", "pt-br", "es-mx", "zh-tw", "fr-fr", "EN-us", "fil", "zh-hans"]
        for code in codes:
            self.assertEqual(main._clean_language(code, "en"), code.strip().lower(), code)

    def test_free_text_falls_back(self):
        for junk in ("{language}", "en;drop", "english", "a" * 40, "en-", "../x"):
            self.assertEqual(main._clean_language(junk, "de"), "de", junk)

    def test_the_config_uses_it(self):
        cfg = main.build_request_config({"logo_language": "x" * 50, "logo_language_secondary": "pt-BR"})
        self.assertEqual(cfg.logo_language, main._cfg.DEFAULT_LOGO_LANGUAGE)
        self.assertEqual(cfg.logo_language_secondary, "pt-br")


class FloatTests(unittest.TestCase):
    def test_floats_past_three_places_share_one_render(self):
        a = main.build_request_config({"bar_height_ratio": "0.0800001"})
        b = main.build_request_config({"bar_height_ratio": "0.0800002"})
        self.assertEqual(a.bar_height_ratio, 0.08)
        self.assertEqual(main._render_config_signature(a), main._render_config_signature(b))


if __name__ == "__main__":
    unittest.main()
