"""The tinted bottom band ("fog"): which colour it picks, and when it stays out."""
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import main
from discovery import DiscoveryMeta
from main import RequestConfig, build_poster


def _two_tone_art() -> Image.Image:
    # Teal over most of the frame, a vivid orange strip at the bottom.
    im = Image.new("RGBA", (500, 750), (30, 110, 120, 255))
    im.paste((230, 120, 20, 255), (0, 560, 500, 750))
    return im


class GreyscaleSkipsFogTests(unittest.TestCase):
    """Art we greyscaled ourselves has no colour left to take, so every such
    poster fell to the same fallback overlay; those keep the plain black band."""

    def _render(self, **kwargs):
        cfg = RequestConfig(vignette_poster_color_bottom=True, **kwargs)
        with mock.patch.object(main, "_vignette_tint_band",
                               wraps=main._vignette_tint_band) as spy:
            build_poster(_two_tone_art(), 87, "Drama", cfg, release_year="2019")
        return spy.called

    def test_colour_poster_is_tinted(self):
        self.assertTrue(self._render())

    def test_cinema_greyscale_is_not_tinted(self):
        cfg = RequestConfig(vignette_poster_color_bottom=True, cinema_greyscale=True)
        meta = DiscoveryMeta()
        meta.release_status = "Cinema"
        with mock.patch.object(main, "_vignette_tint_band",
                               wraps=main._vignette_tint_band) as spy:
            build_poster(_two_tone_art(), 87, "Drama", cfg, release_year="2019",
                         discovery_meta=meta)
        self.assertFalse(spy.called)

    def test_no_quality_greyscale_is_not_tinted(self):
        self.assertFalse(self._render(greyscale_no_quality=True, wait_for_quality=True))


class FogPickTests(unittest.TestCase):
    def test_local_takes_the_covered_colour(self):
        art = _two_tone_art().convert("RGB")
        box = (0, 500, 500, 750)
        whole, *_ = main._fog_pick(art, box, local=False, want_ramp=False)
        near,  *_ = main._fog_pick(art, box, local=True,  want_ramp=False)
        self.assertGreater(whole[2], whole[0])      # teal: blue over red
        self.assertGreater(near[0], near[2])        # orange: red over blue

    def test_colourless_art_falls_back_to_blue(self):
        grey = Image.new("RGB", (500, 750), (90, 90, 90))
        rgb, conf, second, _ = main._fog_pick(grey, (0, 400, 500, 750), True, True)
        self.assertEqual(conf, 1.0)
        self.assertIsNone(second)
        self.assertGreater(rgb[2], rgb[0])
        self.assertGreater(rgb[2], rgb[1])

    def test_faces_are_kept_out_of_the_vote(self):
        # Skin-coloured patch on neutral art: with the patch marked as a face
        # there is no colour left, so the pick falls back rather than going pink.
        art = Image.new("RGB", (500, 750), (40, 40, 40))
        art.paste((205, 150, 120), (150, 150, 350, 450))
        face = [(150.0, 150.0, 200.0, 300.0)]
        rgb, *_ = main._fog_pick(art, (0, 400, 500, 750), True, False, face)
        self.assertGreater(rgb[2], rgb[0])


class ColourStyleTests(unittest.TestCase):
    def test_reference_paints_the_colour_as_is(self):
        art = Image.new("RGB", (500, 400), (252, 238, 133))
        field = main._vignette_tint_band(
            art, (0, 0, 500, 400), (252.0, 238.0, 133.0), 1.0, 2.5, 1.0,
            lightness=1.3, style="reference")
        r, g, b = np.asarray(field, dtype=int)[-1].mean(axis=0)
        self.assertAlmostEqual(r, 252, delta=3)
        self.assertAlmostEqual(g, 238, delta=3)
        self.assertAlmostEqual(b, 133, delta=3)

    def test_shade_mode_darkens(self):
        art = Image.new("RGB", (500, 400), (252, 238, 133))
        field = main._vignette_tint_band(
            art, (0, 0, 500, 400), (252.0, 238.0, 133.0), 1.0, 2.5, 1.0, lightness=1.3)
        self.assertLess(np.asarray(field, dtype=int).mean(), 150)

    def test_muted_is_dark_and_calm(self):
        # Vivid orange and pale yellow both land at nearly one dark depth, with
        # little colour — the "muted" style's whole point.
        for rgb in ((254.0, 144.0, 4.0), (252.0, 238.0, 133.0), (30.0, 111.0, 205.0)):
            art = Image.new("RGB", (500, 400), tuple(int(c) for c in rgb))
            field = main._vignette_tint_band(
                art, (0, 0, 500, 400), rgb, 1.0, 2.5, 1.0, lightness=1.3, style="muted")
            lab = main._srgb_to_oklab(np.asarray(field, dtype=np.float32)[-1].mean(axis=0))
            with self.subTest(rgb=rgb):
                self.assertTrue(main._FOG_MUTED_L_MIN - 0.01 <= lab[0] <= main._FOG_MUTED_L_MAX + 0.01)
                self.assertLessEqual(float(np.hypot(lab[1], lab[2])), main._FOG_MUTED_C_MAX + 0.01)

    def test_style_is_parsed(self):
        for style in ("shade", "muted", "reference"):
            self.assertEqual(main.build_request_config(
                {"vignette_color_style": style}).vignette_color_style, style)
        self.assertEqual(main.build_request_config({}).vignette_color_style, "shade")
        self.assertEqual(main.build_request_config(
            {"vignette_color_style": "loud"}).vignette_color_style, "shade")
        # Split per shape like the other vignette settings.
        self.assertIn("vignette_color_style", main._LANDSCAPE_SPLIT_PARAMS)


if __name__ == "__main__":
    unittest.main()
