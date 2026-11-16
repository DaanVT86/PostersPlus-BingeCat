import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

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


if __name__ == "__main__":
    unittest.main()
