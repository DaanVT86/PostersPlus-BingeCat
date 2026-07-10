from pathlib import Path
import unittest


class ConfiguratorPreviewLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_preview_tail_contains_console_without_label_or_recipe(self):
        self.assertIn('class="preview-console"', self.html)
        self.assertNotIn('preview-console-head', self.html)
        self.assertNotIn('System Output', self.html)
        self.assertIn('id="preview-log"', self.html)
        self.assertNotIn('class="preview-recipe"', self.html)
        self.assertNotIn("updatePreviewRecipe", self.html)

    def test_form_controls_are_square_across_browsers(self):
        self.assertRegex(
            self.html,
            r"input\[type=\"text\"\],\s*input\[type=\"number\"\],\s*(?:textarea,\s*)?select\s*\{[^}]*border-radius:\s*0;",
        )

    def test_select_focus_preserves_dropdown_arrow(self):
        self.assertIn("transition: border-color 0.15s, background-color 0.15s;", self.html)
        self.assertRegex(
            self.html,
            r"select:focus\s*\{[^}]*background-color:\s*var\(--black3\)",
        )
        self.assertNotRegex(
            self.html,
            r"select:focus\s*\{[^}]*\bbackground:\s*var\(--black3\)",
        )

    def test_system_output_matches_preview_background(self):
        self.assertRegex(
            self.html,
            r"\.preview-console\s*\{[^}]*background:\s*var\(--black2\)",
        )

    def test_preview_actions_share_the_metadata_row(self):
        self.assertIn('class="preview-meta-items"', self.html)
        self.assertIn('aria-label="Copy URL"', self.html)
        self.assertIn('aria-label="Load Preset"', self.html)
        self.assertIn('aria-label="Reset"', self.html)
        meta = self.html.index('<div class="preview-meta">')
        controls = self.html.index('<div class="preview-controls">', meta)
        actions = self.html.index('<div class="console-actions">', meta)
        self.assertLess(actions, controls)

    def test_console_reserves_two_lines(self):
        self.assertIn("-webkit-line-clamp: 2;", self.html)
        self.assertIn("white-space: normal;", self.html)
        self.assertIn("min-height: calc(2 * 1.65em + 12px);", self.html)

    def test_panels_use_the_available_desktop_height(self):
        self.assertIn("height: max(680px, calc(100vh - 144px));", self.html)
        self.assertIn("max-height: max(680px, calc(100vh - 144px));", self.html)
        self.assertIn("grid-template-rows: minmax(0, 1fr);", self.html)
        self.assertRegex(self.html, r"\.left-col\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;")
        self.assertRegex(self.html, r"\.right-col\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;")
        self.assertIn("#tab-host {\n    flex: 1;\n    min-height: 0;", self.html)
        self.assertNotIn("max-height: calc(100vh - 180px)", self.html)
        self.assertNotIn("_lockTabHeight", self.html)
        self.assertIn("if (host) host.scrollTop = 0;", self.html)

    def test_live_preview_corner_notch_is_restored(self):
        right = self.html.index("<!-- RIGHT: PREVIEW -->")
        header = self.html.index('<div class="panel-header">', right)
        self.assertIn('class="notch"', self.html[right:header])

    def test_desktop_trailing_padding_is_reduced(self):
        self.assertIn("padding: 0 28px 28px;", self.html)
        self.assertNotIn("padding: 0 28px 80px;", self.html)


if __name__ == "__main__":
    unittest.main()
