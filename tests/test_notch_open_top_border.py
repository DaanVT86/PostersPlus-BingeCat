import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import awards


class NotchOpenTopBorderTests(unittest.TestCase):
    def _assert_open_top_trim(self, use_cairo: bool):
        poster = Image.new("RGBA", (500, 750), (80, 80, 80, 255))
        with patch.object(awards, "_HAS_CAIRO", use_cairo):
            rendered = awards.draw_award_badge(
                poster, "CULT CLASSIC", notch_style="gold"
            )

        pixels = np.asarray(rendered)
        gold = (
            (pixels[:, :, 0] > 140)
            & (pixels[:, :, 1] > 100)
            & (pixels[:, :, 2] < 100)
        )
        top_y = int(poster.height * 0.007)
        top_trim_x = np.where(gold[top_y])[0]

        self.assertGreater(len(top_trim_x), 0)
        self.assertFalse(gold[top_y, poster.width // 2])
        self.assertLess(top_trim_x.min(), poster.width // 2)
        self.assertGreater(top_trim_x.max(), poster.width // 2)
        self.assertTrue(gold[top_y + int(poster.height * 0.07)].any())

    def test_pil_notch_trim_has_no_top_border(self):
        self._assert_open_top_trim(use_cairo=False)

    @unittest.skipUnless(awards._HAS_CAIRO, "pycairo not installed")
    def test_cairo_notch_trim_has_no_top_border(self):
        self._assert_open_top_trim(use_cairo=True)

    def test_cached_masks_are_bounded_immutable_and_keyed_by_all_geometry(self):
        awards._notch_mask_bytes.cache_clear()
        for width, height, radius in ((210, 168, 51), (211, 168, 51), (210, 169, 51), (210, 168, 52)):
            reference = Image.new("L", (width, height), 0)
            ImageDraw.Draw(reference).rounded_rectangle(
                [(0, 0), (width - 1, height - 1)], radius=radius, fill=255,
                corners=(False, False, True, True),
            )
            payload = awards._notch_mask_bytes(width, height, radius)
            self.assertIsInstance(payload, bytes)
            self.assertEqual(payload, reference.tobytes())
            owned_image = Image.frombytes("L", (width, height), payload)
            owned_image.paste(0, (0, 0, width, height))
            self.assertEqual(awards._notch_mask_bytes(width, height, radius), reference.tobytes())
        self.assertEqual(awards._notch_mask_bytes.cache_info().hits, 4)
        for width in range(220, 260):
            awards._notch_mask_bytes(width, 168, 51)
        self.assertEqual(awards._notch_mask_bytes.cache_info().currsize, 32)

    def test_warm_notch_masks_do_not_retain_poster_pixels_or_mutate_inputs(self):
        awards._notch_mask_bytes.cache_clear()
        for style in ("frosted", "black"):
            for color in ((10, 45, 180, 255), (220, 145, 20, 255)):
                poster = Image.new("RGBA", (500, 750), color)
                original = poster.tobytes()
                rendered = awards.draw_award_badge(poster, "Été ★", notch_style=style)
                again = awards.draw_award_badge(poster, "Été ★", notch_style=style)
                self.assertEqual(rendered.tobytes(), again.tobytes())
                self.assertEqual(poster.tobytes(), original)
        self.assertGreater(awards._notch_mask_bytes.cache_info().hits, 0)


if __name__ == "__main__":
    unittest.main()
