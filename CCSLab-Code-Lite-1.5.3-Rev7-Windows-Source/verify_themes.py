"""Exercise real Tk theme selection, persisted preferences and diff rendering."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest

from verify_support import TEST_ROOT

sys.path.insert(0, str(Path(__file__).parent / "app"))
from app import App, read_json, write_json
from themes import THEMES


def contrast(a, b):
    def luminance(color):
        rgb = [int(color[n:n + 2], 16) / 255 for n in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return sum(x * weight for x, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    low, high = sorted((luminance(a), luminance(b)))
    return (high + 0.05) / (low + 0.05)


class ThemeTests(unittest.TestCase):
    def test_select_all_themes_and_restart(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="themes-") as folder:
            data = Path(folder)
            root = tk.Tk()
            gui = App(root, data, smoke=True)
            errors = []
            root.report_callback_exception = lambda *_args: errors.append(_args)
            try:
                gui.show_page("settings")
                root.update()
                self.assertEqual(len(gui.theme_picker["values"]), 8)
                for index, (key, (label, colors)) in enumerate(THEMES.items()):
                    with self.subTest(theme=key):
                        gui.theme_picker.current(index)
                        gui.theme_picker.event_generate("<<ComboboxSelected>>")
                        root.update()
                        self.assertEqual(gui.theme.get(), label)
                        self.assertEqual(read_json(gui.prefs_path, {})["theme"], key)
                        self.assertEqual(root.cget("background"), colors["bg"])
                        self.assertEqual(gui.chat.cget("background"), colors["panel"])
                        self.assertEqual(gui.diff.tag_cget("add", "foreground"), colors["add"])
                self.assertFalse(errors, errors)
            finally:
                gui.close()
            root = tk.Tk()
            gui = App(root, data, smoke=True)
            try:
                self.assertEqual(gui.theme.get(), "暮色紫")
                self.assertEqual(gui.colors["bg"], THEMES["plum"][1]["bg"])
            finally:
                gui.close()

    def test_existing_and_unknown_preferences(self):
        for saved, expected in (("light", "经典浅色"), ("dark", "经典深色"),
                                ("no-longer-a-theme", "经典浅色"), ({"bad": "value"}, "经典浅色")):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="theme-migrate-") as folder:
                data = Path(folder)
                write_json(data / "preferences.json", {"theme": saved, "chat_size": "大", "default_mode": "方案"})
                root = tk.Tk()
                gui = App(root, data, smoke=True)
                try:
                    self.assertEqual(gui.theme.get(), expected)
                    self.assertEqual(gui.chat_size.get(), "大")
                    self.assertEqual(gui.default_mode.get(), "方案")
                finally:
                    gui.close()

    def test_diff_popup_uses_selected_palette(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="theme-diff-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            try:
                gui.theme.set("静谧墨绿")
                self.assertTrue(gui.apply_preferences())
                gui.diff_text = "--- a/example.py\n+++ b/example.py\n-old\n+new\n"
                gui.expand_diff()
                root.update()
                popup = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)
                text = next(w for w in descendants(popup) if isinstance(w, tk.Text))
                self.assertEqual(text.tag_cget("add", "foreground"), THEMES["forest"][1]["add"])
                self.assertEqual(popup.cget("background"), THEMES["forest"][1]["panel"])
                popup.destroy()
            finally:
                gui.close()

    def test_text_contrast_for_every_palette(self):
        for key, (_label, colors) in THEMES.items():
            for background in ("panel", "field", "side", "soft"):
                with self.subTest(theme=key, background=background):
                    self.assertGreaterEqual(contrast(colors["text"], colors[background]), 4.5)
                    self.assertGreaterEqual(contrast(colors["muted"], colors[background]), 4.5)
            self.assertGreaterEqual(contrast(colors["ink"], colors["accent"]), 4.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
