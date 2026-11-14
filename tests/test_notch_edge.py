"""sash_badge_pos edge_left / edge_right hang the notch off a side edge, its
label turned to run along it, at sash_edge_y down the poster."""
import unittest

import numpy as np
from PIL import Image

import awards
import main


def _poster():
    return Image.new("RGBA", (500, 750), (40, 90, 140, 255))


def _changed(out, box):
    a = np.asarray(out.crop(box), dtype=np.int16)
    b = np.asarray(_poster().crop(box), dtype=np.int16)
    return int(np.abs(a - b).sum())


class EdgeNotchTests(unittest.TestCase):
    def test_hangs_off_its_edge_and_leaves_the_top_alone(self):
        for pos, near, far in (("edge_left", (0, 300, 6, 450), (494, 0, 500, 750)),
                               ("edge_right", (494, 300, 500, 450), (0, 0, 6, 750))):
            with self.subTest(pos=pos):
                out = awards.draw_award_badge(_poster(), "Oscar Winner", position=pos)
                self.assertGreater(_changed(out, near), 0)
                self.assertEqual(_changed(out, far), 0)
                self.assertEqual(_changed(out, (0, 0, 500, 60)), 0)

    def test_runs_along_the_edge(self):
        # Longer down the side than it is deep into the poster.
        out = awards.draw_award_badge(_poster(), "Oscar Winner", position="edge_left")
        diff = np.abs(np.asarray(out, dtype=np.int16) - np.asarray(_poster(), dtype=np.int16)).max(axis=2)
        ys, xs = np.nonzero(diff)
        self.assertGreater(ys.max() - ys.min(), 2 * (xs.max() - xs.min()))
        self.assertEqual(xs.min(), 0)

    def test_edge_y_moves_it(self):
        high = awards.draw_award_badge(_poster(), "Oscar Winner", position="edge_left", edge_y=0.2)
        self.assertGreater(_changed(high, (0, 130, 6, 170)), 0)
        self.assertEqual(_changed(high, (0, 500, 6, 750)), 0)

    def test_every_style(self):
        for style in ("frosted", "black", "silver", "gold"):
            with self.subTest(style=style):
                out = awards.draw_award_badge(_poster(), "#3 Today", notch_style=style,
                                              position="edge_right")
                self.assertGreater(_changed(out, (494, 300, 500, 450)), 0)

    def test_parameters(self):
        cfg = main.build_request_config({"sash_badge_pos": "edge_left", "sash_edge_y": "0.3"})
        self.assertEqual((cfg.sash_badge_pos, cfg.sash_edge_y), ("edge_left", 0.3))
        self.assertEqual(main.build_request_config({"sash_edge_y": "2"}).sash_edge_y, 0.95)
        self.assertEqual(main.build_request_config({}).sash_edge_y, 0.5)

    def test_rank_on_the_other_side_leaves_it_where_it_is(self):
        cfg = main.RequestConfig(sash_mode="notch", sash_badge_pos="edge_left", trending_sash="opposite")
        self.assertEqual(main._sash_beside_rank(cfg).sash_badge_pos, "edge_left")


if __name__ == "__main__":
    unittest.main()
