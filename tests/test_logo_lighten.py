"""ensure_light_logo lightens a logo's black ink for a dark poster, keeps its
coloured parts, and leaves logos whose black is structure (outline, card)
alone.  sash_chip_y moves the side chip down."""
import unittest

import numpy as np
from PIL import Image, ImageDraw

import awards
import main
from tmdb import composite_logo, ensure_light_logo

BLACK = (12, 10, 14, 255)
RED = (220, 30, 30, 255)
YELLOW = (250, 220, 40, 255)
WHITE = (255, 255, 255, 255)


def _canvas():
    return Image.new("RGBA", (240, 80), (0, 0, 0, 0))


def _bars(img, fill, xs=range(10, 230, 30)):
    d = ImageDraw.Draw(img)
    for x in xs:
        d.rectangle((x, 10, x + 14, 70), fill=fill)
    return img


def _px(img, xy):
    return tuple(img.getpixel(xy))


class EnsureLightLogoTests(unittest.TestCase):
    def test_black_ink_turns_white(self):
        out = ensure_light_logo(_bars(_canvas(), BLACK))
        r, g, b, a = _px(out, (15, 40))
        self.assertGreater(min(r, g, b), 235)
        self.assertEqual(a, 255)
        self.assertEqual(_px(out, (5, 5))[3], 0)

    def test_tinted_near_black_still_lightens(self):
        # The old HSV saturation gate read a faint tint on black as colour.
        out = ensure_light_logo(_bars(_canvas(), (30, 12, 12, 255)))
        self.assertGreater(min(_px(out, (15, 40))[:3]), 200)

    def test_dense_black_wordmark_lightens(self):
        # Letters filling most of their box are not a card.
        img = _canvas()
        ImageDraw.Draw(img).rectangle((10, 10, 229, 69), fill=BLACK)
        d = ImageDraw.Draw(img)
        for x in range(40, 229, 40):
            d.rectangle((x, 10, x + 3, 69), fill=(0, 0, 0, 0))
        self.assertGreater(min(_px(ensure_light_logo(img), (20, 40))[:3]), 235)

    def test_coloured_accent_is_kept(self):
        img = _bars(_canvas(), BLACK, xs=range(40, 230, 30))
        ImageDraw.Draw(img).rectangle((5, 10, 19, 70), fill=RED)
        out = ensure_light_logo(img)
        self.assertEqual(_px(out, (12, 40)), RED)
        self.assertGreater(min(_px(out, (45, 40))[:3]), 235)

    def test_white_text_on_black_card_untouched(self):
        img = _canvas()
        ImageDraw.Draw(img).rectangle((0, 0, 239, 79), fill=BLACK)
        _bars(img, WHITE)
        out = ensure_light_logo(img)
        np.testing.assert_array_equal(np.asarray(out), np.asarray(img))

    def test_black_outline_round_colour_untouched(self):
        img = _canvas()
        d = ImageDraw.Draw(img)
        for x in range(10, 230, 45):
            d.rectangle((x, 8, x + 36, 72), fill=BLACK)
            d.rectangle((x + 6, 14, x + 30, 66), fill=YELLOW)
        out = ensure_light_logo(img)
        np.testing.assert_array_equal(np.asarray(out), np.asarray(img))

    def test_coloured_logo_untouched(self):
        img = _bars(_canvas(), (30, 60, 220, 255))
        self.assertIs(ensure_light_logo(img), img)

    def test_composite_only_lightens_on_dark_ground(self):
        logo = _bars(_canvas(), BLACK)
        for ground, light in (((20, 20, 24, 255), True), ((230, 230, 230, 255), False)):
            with self.subTest(ground=ground):
                poster = Image.new("RGBA", (500, 750), ground)
                composite_logo(poster, logo)
                ink = np.asarray(poster.convert("L"))
                if light:
                    self.assertGreater(int(ink.max()), 200)
                else:
                    self.assertLess(int(ink.min()), 40)


class SashChipYTests(unittest.TestCase):
    def test_parse_default_and_clamp(self):
        self.assertEqual(main.build_request_config({}).sash_chip_y, 0.0)
        self.assertEqual(main.build_request_config({"sash_chip_y": "0.05"}).sash_chip_y, 0.05)
        self.assertEqual(main.build_request_config({"sash_chip_y": "1"}).sash_chip_y, 0.15)
        self.assertEqual(main.build_request_config({"sash_chip_y": "-1"}).sash_chip_y, -0.02)

    def _chip_top(self, **kw):
        poster = Image.new("RGBA", (500, 750), (40, 90, 140, 255))
        out = awards.draw_award_badge(poster.copy(), "#12 Today", position="left", **kw)
        diff = np.abs(np.asarray(out, dtype=np.int16) - np.asarray(poster, dtype=np.int16)).sum(axis=2)
        return int(np.flatnonzero(diff[:, :150].any(axis=1))[0])

    def test_chip_moves_down(self):
        self.assertAlmostEqual(self._chip_top(chip_offset=0.1) - self._chip_top(), 75, delta=2)

    def test_centre_notch_ignores_offset(self):
        poster = Image.new("RGBA", (500, 750), (40, 90, 140, 255))
        a = awards.draw_award_badge(poster.copy(), "Oscar Winner")
        b = awards.draw_award_badge(poster.copy(), "Oscar Winner", chip_offset=0.1)
        np.testing.assert_array_equal(np.asarray(a), np.asarray(b))


class SashChipXTests(unittest.TestCase):
    def test_parse_default_and_clamp(self):
        self.assertEqual(main.build_request_config({}).sash_chip_x, 0.0)
        self.assertEqual(main.build_request_config({"sash_chip_x": "0.1"}).sash_chip_x, 0.1)
        self.assertEqual(main.build_request_config({"sash_chip_x": "1"}).sash_chip_x, 0.25)
        self.assertEqual(main.build_request_config({"sash_chip_x": "-1"}).sash_chip_x, -0.045)

    def test_default_keeps_cache_signature(self):
        cfg = main.build_request_config({})
        self.assertNotIn("sash_chip_x", main._render_config_signature(cfg))
        moved = main.build_request_config({"sash_chip_x": "0.1"})
        self.assertIn("sash_chip_x", main._render_config_signature(moved))

    def _chip_cols(self, position, **kw):
        poster = Image.new("RGBA", (500, 750), (40, 90, 140, 255))
        out = awards.draw_award_badge(poster.copy(), "#12 Today", position=position, **kw)
        diff = np.abs(np.asarray(out, dtype=np.int16) - np.asarray(poster, dtype=np.int16)).sum(axis=2)
        cols = np.flatnonzero(diff.any(axis=0))
        return int(cols[0]), int(cols[-1])

    def test_chip_moves_in_from_its_corner(self):
        for position, sign in (("left", 1), ("right", -1)):
            with self.subTest(position=position):
                base, moved = self._chip_cols(position), self._chip_cols(position, chip_offset_x=0.1)
                self.assertAlmostEqual(moved[0] - base[0], 50 * sign, delta=2)
                self.assertAlmostEqual(moved[1] - base[1], 50 * sign, delta=2)

    def test_never_past_the_edge(self):
        left, _ = self._chip_cols("left", chip_offset_x=-0.045)
        self.assertGreaterEqual(left, 0)
        _, right = self._chip_cols("right", chip_offset_x=-0.045)
        self.assertLessEqual(right, 499)

    def test_centre_notch_ignores_offset(self):
        poster = Image.new("RGBA", (500, 750), (40, 90, 140, 255))
        a = awards.draw_award_badge(poster.copy(), "Oscar Winner")
        b = awards.draw_award_badge(poster.copy(), "Oscar Winner", chip_offset_x=0.1)
        np.testing.assert_array_equal(np.asarray(a), np.asarray(b))


if __name__ == "__main__":
    unittest.main()
