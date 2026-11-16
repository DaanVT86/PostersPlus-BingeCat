"""Hebrew labels: the label font choice, fallback by script, and bidi order."""
import os
import unittest

import numpy as np
from PIL import Image

import awards
import fonts
import main
from i18n import load_languages, translate_sash, visual
from main import RequestConfig, build_poster, build_request_config

INTER = os.path.join(fonts.FONTS_DIR, "Inter-Bold.ttf")
RUBIK = os.path.join(fonts.FONTS_DIR, "Rubik-Bold.ttf")


def _art(size=(500, 750)):
    return Image.new("RGBA", size, (16, 16, 24, 255))


class VisualOrderTests(unittest.TestCase):
    def test_left_to_right_text_is_untouched(self):
        for text in ("", "Drama · 2019 ★ 87", "Première", "Ταινία"):
            self.assertEqual(visual(text), text)

    def test_hebrew_is_drawn_right_to_left_with_numbers_kept_forwards(self):
        self.assertEqual(visual("מקום #3 היום"), "םויה #3 םוקמ")
        # The line is a right-to-left paragraph: the genre that starts it is
        # drawn at the right, the score it ends with at the left.
        self.assertEqual(visual("דרמה · 2019 ★ 87"), "87 ★ 2019 · המרד")

    def test_latin_names_inside_hebrew_stay_forwards(self):
        self.assertEqual(visual("A24 • מועמד לאוסקר"), "רקסואל דמעומ • A24")


class LabelFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def test_the_chosen_font_draws_a_language_it_has(self):
        self.assertEqual(fonts.resolve_label_font("inter", "fr"), INTER)
        self.assertEqual(fonts.resolve_label_font("rubik", "fr"), RUBIK)
        self.assertEqual(fonts.resolve_label_font("rubik", "en"), RUBIK)
        self.assertEqual(fonts.resolve_label_font("rubik", None), RUBIK)

    def test_a_font_without_the_language_gives_way(self):
        self.assertEqual(fonts.resolve_label_font("inter", "he"), RUBIK)
        self.assertEqual(fonts.resolve_label_font("inter", "he-IL"), RUBIK)
        self.assertEqual(fonts.resolve_label_font("rubik", "el"), INTER)
        self.assertEqual(fonts.resolve_label_font("rubik", "vi"), INTER)

    def test_an_unknown_choice_is_the_default(self):
        self.assertEqual(fonts.resolve_label_font("comic-sans", "en"), INTER)

    def test_every_label_font_has_the_symbols_the_labels_draw(self):
        for choice, name in fonts.LABEL_FONTS.items():
            with self.subTest(choice):
                self.assertTrue(fonts.covers(os.path.join(fonts.FONTS_DIR, name), "★•·…—–"))

    def test_a_title_the_genre_font_cannot_draw_takes_a_label_font(self):
        bebas = os.path.join(fonts.FONTS_DIR, "BebasNeue-Bold.ttf")
        self.assertEqual(fonts.font_for_text(bebas, "Heat"), bebas)
        self.assertEqual(fonts.font_for_text(bebas, "שעת האפס"), RUBIK)

    def test_the_scope_sets_and_restores_the_font(self):
        self.assertEqual(fonts.label_path(), INTER)
        with fonts.label_font_scope("inter", "he"):
            self.assertEqual(fonts.label_path(), RUBIK)
            self.assertEqual(awards._notch_font(30).path, RUBIK)
        self.assertEqual(fonts.label_path(), INTER)

    def test_cached_notch_parts_are_kept_per_font(self):
        with fonts.label_font_scope("inter", "en"):
            inter = awards._notch_label_layer_1x("Oscar Winner", 45, 3, 150, 30, (255, 255, 255, 245))
        with fonts.label_font_scope("rubik", "en"):
            rubik = awards._notch_label_layer_1x("Oscar Winner", 45, 3, 150, 30, (255, 255, 255, 245))
        self.assertFalse(np.array_equal(np.asarray(inter), np.asarray(rubik)))


class RequestConfigTests(unittest.TestCase):
    def test_label_font_parses_and_ignores_unknown_values(self):
        self.assertEqual(build_request_config({}).label_font, "inter")
        self.assertEqual(build_request_config({"label_font": "Rubik"}).label_font, "rubik")
        self.assertEqual(build_request_config({"label_font": "papyrus"}).label_font, "inter")

    def test_the_default_font_leaves_cache_keys_alone(self):
        sig = main._render_config_signature(build_request_config({}))
        self.assertNotIn("label_font", sig)
        self.assertIn("label_font", main._render_config_signature(
            build_request_config({"label_font": "rubik"})))

    def test_hebrew_posters_cached_before_now_re_render(self):
        rev = next(r for r in main._RENDER_REVISIONS if r.rev == 17)
        self.assertTrue(rev.applies(build_request_config({"logo_language": "he"})))
        self.assertTrue(rev.applies(build_request_config({"logo_language": "he-il"})))
        self.assertFalse(rev.applies(build_request_config({"logo_language": "en"})))


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def _render(self, **kwargs):
        cfg = RequestConfig(top_gradient="off", bottom_gradient="off", **kwargs)
        return np.array(build_poster(_art(), 87, "Drama", cfg, release_year="2019"))

    def test_the_font_choice_changes_the_label(self):
        for mode in (1, 2, 3, 4):
            with self.subTest(mode=mode):
                self.assertFalse(np.array_equal(
                    self._render(rating_display_mode=mode, label_font="inter"),
                    self._render(rating_display_mode=mode, label_font="rubik")))

    def test_hebrew_draws_the_same_whichever_font_is_chosen(self):
        for mode in (1, 2, 3, 4):
            with self.subTest(mode=mode):
                self.assertTrue(np.array_equal(
                    self._render(rating_display_mode=mode, logo_language="he", label_font="inter"),
                    self._render(rating_display_mode=mode, logo_language="he", label_font="rubik")))

    def test_hebrew_sash_labels_translate(self):
        self.assertEqual(translate_sash("Oscar Winner", "he"), "זוכה אוסקר")


if __name__ == "__main__":
    unittest.main()
