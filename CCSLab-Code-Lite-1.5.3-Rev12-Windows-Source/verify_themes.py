"""Exercise real Tk theme selection, persisted preferences and diff rendering."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from verify_support import TEST_ROOT

sys.path.insert(0, str(Path(__file__).parent / "app"))
from app import App, apply_windows_titlebar, read_json, write_json
from markdown_view import _python_highlight_spans, insert_markdown
from themes import THEMES, migrate_default_theme, theme_key


def contrast(a, b):
    def luminance(color):
        rgb = [int(color[n:n + 2], 16) / 255 for n in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return sum(x * weight for x, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    low, high = sorted((luminance(a), luminance(b)))
    return (high + 0.05) / (low + 0.05)


def settle_geometry(root):
    root.after(800, root.quit)
    root.mainloop()


class ThemeTests(unittest.TestCase):
    def test_windows_titlebar_uses_win32_and_actual_top_level_hwnd(self):
        class TkWindow:
            tk = Mock()

            def winfo_id(self):
                return 0x2222

        window = TkWindow()
        window.tk.call.return_value = "win32"
        user32 = Mock()
        user32.GetAncestor.return_value = 0x3333
        dwmapi = Mock()
        dwmapi.DwmSetWindowAttribute.return_value = 0
        with patch("app.ctypes.WinDLL", side_effect=(user32, dwmapi)):
            self.assertTrue(apply_windows_titlebar(window, THEMES["herdr-midnight"][1]))
        self.assertEqual(user32.GetAncestor.call_args.args[0].value, 0x2222)
        self.assertEqual(user32.GetAncestor.call_args.args[1], 2)
        self.assertEqual([call.args[1] for call in dwmapi.DwmSetWindowAttribute.call_args_list],
                         [20, 35, 36])

    def test_theme_header_label_and_icon_share_action_and_keep_dpi_gap(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="theme-header-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            try:
                root.geometry("950x620")
                gui.account = {"id": 1, "username": "A" * 150, "role": "admin", "status": "active"}
                gui.account_online = True
                gui.account_state_changed()
                gui.update_actions()
                root.update()
                initial = gui.theme.get()
                gui.theme_label.event_generate("<ButtonPress-1>", x=5, y=5, when="now")
                gui.theme_label.event_generate("<ButtonRelease-1>", x=5, y=5, when="now")
                root.update()
                self.assertNotEqual(gui.theme.get(), initial)
                after_label = gui.theme.get()
                gui.theme_icon.event_generate("<Button-1>", x=14, y=14, when="now")
                root.update()
                self.assertNotEqual(gui.theme.get(), after_label)
                self.assertEqual(gui.theme_icon.master, gui.theme_control)
                expected_gap = round((72 / 2.54) * float(root.tk.call("tk", "scaling")))
                actual_gap = gui.status_label.winfo_rootx() - (
                    gui.theme_icon.winfo_rootx() + gui.theme_icon.winfo_width())
                self.assertGreaterEqual(actual_gap, expected_gap - 2)
                self.assertLessEqual(actual_gap, expected_gap + 3)
                for width in (950, 1280):
                    root.geometry(f"{width}x620")
                    root.update()
                    self.assertLessEqual(gui.theme_label.winfo_rootx() + gui.theme_label.winfo_width(),
                                         gui.theme_icon.winfo_rootx())
                    self.assertLessEqual(gui.theme_icon.winfo_rootx() + gui.theme_icon.winfo_width(),
                                         gui.status_label.winfo_rootx())
                    self.assertLessEqual(gui.status_label.winfo_rootx() + gui.status_label.winfo_width(),
                                         gui.role_button.winfo_rootx())
                self.assertIn("…", gui.status.get())
            finally:
                gui.close()

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
                self.assertEqual(len(gui.theme_picker["values"]), len(THEMES))
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
                label, colors = next(reversed(THEMES.values()))
                self.assertEqual(gui.theme.get(), label)
                self.assertEqual(gui.colors["bg"], colors["bg"])
            finally:
                gui.close()

    def test_existing_removed_and_unknown_preferences(self):
        cases = (("light", "light"), ("dark", "dark"), ("mint", "mint"), ("forest", "mint"),
                 ("vscode-dark", "vscode-dark"), ("herdr-midnight", "herdr-midnight"),
                 ("VS Code Dark", "vscode-dark"), ("Herdr Midnight", "herdr-midnight"),
                 ("plum", "herdr-midnight"), ("ocean", "vscode-dark"),
                 ("静谧墨绿", "mint"), ("暮色紫", "herdr-midnight"),
                 ("深海蓝", "vscode-dark"), ("no-longer-a-theme", "vscode-dark"),
                 ({"bad": "value"}, "vscode-dark"))
        for saved, expected_key in cases:
            with self.subTest(saved=saved), tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="theme-migrate-") as folder:
                data = Path(folder)
                preferences = {"theme": saved, "theme_defaults_version": 10,
                               "chat_size": "大", "default_mode": "方案", "custom_keep": 47}
                self.assertTrue(migrate_default_theme(preferences))
                self.assertEqual(preferences["theme"], expected_key)
                self.assertEqual(preferences["theme_defaults_version"], 11)
                self.assertEqual(preferences["custom_keep"], 47)
                write_json(data / "preferences.json", {"theme": saved, "theme_defaults_version": 10,
                                                       "chat_size": "大", "default_mode": "方案",
                                                       "custom_keep": 47})
                root = tk.Tk()
                gui = App(root, data, smoke=True)
                try:
                    self.assertEqual(gui.theme.get(), THEMES[expected_key][0])
                    self.assertEqual(gui.chat_size.get(), "大")
                    self.assertEqual(gui.default_mode.get(), "方案")
                    self.assertEqual(read_json(gui.prefs_path, {})["custom_keep"], 47)
                finally:
                    gui.close()

    def test_fresh_profile_defaults_to_dark_and_renamed_choices_persist(self):
        self.assertEqual(THEMES["vscode-dark"][0], "Dark")
        self.assertEqual(THEMES["herdr-midnight"][0], "Midnight")
        for name, key in (("Dark", "vscode-dark"), ("Midnight", "herdr-midnight"),
                          ("VS Code Dark", "vscode-dark"), ("Herdr Midnight", "herdr-midnight"),
                          (None, "vscode-dark"), ("unknown", "vscode-dark")):
            self.assertEqual(theme_key(name), key)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="dark-default-") as folder:
            data = Path(folder)
            root = tk.Tk()
            gui = App(root, data, smoke=True)
            try:
                self.assertEqual(gui.theme.get(), "Dark")
                self.assertEqual(read_json(gui.prefs_path, {})["theme"], "vscode-dark")
                self.assertEqual(gui.colors["bg"], THEMES["vscode-dark"][1]["bg"])
                gui.show_page("settings")
                root.update()
                choices = tuple(gui.theme_picker["values"])
                self.assertIn("Dark", choices)
                self.assertIn("Midnight", choices)
                self.assertNotIn("VS Code Dark", choices)
                self.assertNotIn("Herdr Midnight", choices)
                gui.theme_picker.current(choices.index("Midnight"))
                gui.theme_picker.event_generate("<<ComboboxSelected>>")
                root.update()
                self.assertEqual(gui.theme.get(), "Midnight")
                self.assertEqual(read_json(gui.prefs_path, {})["theme"], "herdr-midnight")
            finally:
                gui.close()
            root = tk.Tk()
            gui = App(root, data, smoke=True)
            try:
                self.assertEqual(gui.theme.get(), "Midnight")
                self.assertEqual(gui.colors["bg"], THEMES["herdr-midnight"][1]["bg"])
            finally:
                gui.close()

    def test_diff_popup_uses_selected_palette(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="theme-diff-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            try:
                gui.theme.set(THEMES["herdr-midnight"][0])
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
                self.assertEqual(text.tag_cget("add", "foreground"), THEMES["herdr-midnight"][1]["add"])
                self.assertEqual(popup.cget("background"), THEMES["herdr-midnight"][1]["panel"])
                popup.destroy()
            finally:
                gui.close()

    def test_legacy_upgrade_preserves_theme_and_other_preferences(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="theme-upgrade-") as folder:
            data = Path(folder)
            write_json(data / "preferences.json", {"theme": "light", "strict": True,
                       "default_mode": "方案", "custom_keep": "yes"})
            root = tk.Tk()
            gui = App(root, data, smoke=True)
            try:
                self.assertEqual(gui.theme.get(), THEMES["light"][0])
                self.assertEqual(read_json(gui.prefs_path, {})["theme_defaults_version"], 11)
                self.assertTrue(gui.strict.get())
                self.assertEqual(gui.default_mode.get(), "方案")
                self.assertEqual(gui.prefs["custom_keep"], "yes")
                gui.theme.set(THEMES["dark"][0])
                gui.apply_preferences()
            finally:
                gui.close()
            root = tk.Tk()
            gui = App(root, data, smoke=True)
            try:
                self.assertEqual(gui.theme.get(), THEMES["dark"][0])
            finally:
                gui.close()

    def test_removed_themes_and_herdr_region_tokens(self):
        self.assertEqual(set(THEMES), {"light", "dark", "warm", "mint", "sky", "vscode-dark", "herdr-midnight"})
        self.assertEqual(theme_key("forest"), "mint")
        colors = THEMES["herdr-midnight"][1]
        self.assertEqual(colors["sidebar"], "#101114")
        self.assertEqual(colors["side"], "#101114")
        self.assertEqual(colors["panel"], "#0A0A0A")
        self.assertEqual(colors["bg"], "#0A0A0A")
        self.assertEqual(colors["header"], "#17191D")
        self.assertEqual(colors["result_panel"], "#141414")
        self.assertEqual(colors["field"], "#141414")
        self.assertEqual(colors["selection"], "#262629")
        self.assertEqual(colors["text"], "#EEEEEE")
        self.assertEqual(colors["muted"], "#A7ADB5")

    def test_text_contrast_for_every_theme_and_semantic_heading(self):
        for key, (_label, colors) in THEMES.items():
            for background in ("panel", "field", "side", "soft", "result_panel", "sidebar_card"):
                with self.subTest(theme=key, background=background):
                    self.assertGreaterEqual(contrast(colors["text"], colors[background]), 4.5)
                    self.assertGreaterEqual(contrast(colors["muted"], colors[background]), 4.5)
            self.assertGreaterEqual(contrast(colors["ink"], colors["accent"]), 4.5)
            for role in ("heading_1", "heading_2", "heading_3", "heading_4", "strong",
                         "emphasis", "list_marker", "quote", "error", "warning", "success",
                         "filename", "path", "inline_code", "link", "code_keyword",
                         "code_string", "code_function", "code_type", "code_number", "code_comment"):
                self.assertGreaterEqual(contrast(colors[role], colors["panel"]), 4.5,
                                        f"{key}: {role}")
            for role in ("keyword", "string", "function", "class", "parameter", "variable",
                         "builtin", "type", "number", "constant", "comment", "decorator",
                         "operator", "punctuation"):
                self.assertGreaterEqual(contrast(colors[f"code_{role}"], colors["code"]), 4.5,
                                        f"{key}: code_{role}")
            if key in {"herdr-midnight", "vscode-dark"}:
                self.assertLess(contrast(colors["line"], colors["panel"]), 1.25)
                roles = ("heading_1", "heading_2", "heading_3", "text", "strong", "emphasis",
                         "list_marker", "quote", "inline_code", "path", "link", "error", "warning", "success")
                self.assertEqual(len({colors[role] for role in roles}), len(roles))

    def test_semantic_roles_scrollbars_and_responsive_three_pane_layout(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="ide-layout-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            try:
                required = {"window", "sidebar", "editor", "container", "line", "text_primary",
                            "text_secondary", "text_disabled", "accent", "hover", "active", "focus",
                            "input_bg", "selected_bg", "scroll_track", "scrollbar_thumb", "code"}
                for key, (_label, colors) in THEMES.items():
                    with self.subTest(theme=key):
                        self.assertTrue(required.issubset(colors))
                gui.account = {"id": 7, "username": "Visual fixture", "role": "admin", "status": "active"}
                gui.account_online = True
                gui.show_page("workspace", force=True)
                root.geometry("1366x768")
                root.update()
                settle_geometry(root)
                self.assertGreaterEqual(gui.project_tree.winfo_height(), 100)
                self.assertEqual(gui.open_button.cget("text"), "打开项目")
                self.assertEqual(gui.open_button.winfo_width(), gui.remove_recent_button.winfo_width())
                self.assertEqual(gui.open_button.winfo_rootx() + gui.open_button.winfo_width(),
                                 gui.remove_recent_button.winfo_rootx() + gui.remove_recent_button.winfo_width())
                self.assertLessEqual(abs(gui.results_pane.winfo_width() - 336), 8)
                self.assertGreaterEqual(gui.workspace_view.nametowidget(
                    gui.workspace_view.panes()[1]).winfo_width(), 600)
                self.assertLessEqual(int(gui.workspace_view.cget("sashwidth")), 1)
                self.assertNotIn("Notebook.client", str(gui.style.layout("TNotebook")))
                self.assertNotIn("Treeview.field", str(gui.style.layout("Treeview")))
                for width, height in ((800, 600), (1920, 1080)):
                    root.geometry(f"{width}x{height}")
                    root.update()
                    settle_geometry(root)
                    first = gui.workspace_view.sash_coord(0)[0]
                    second = gui.workspace_view.sash_coord(1)[0]
                    actual_width = gui.workspace_view.winfo_width()
                    self.assertGreaterEqual(first, 160)
                    self.assertGreaterEqual(second - first, 300)
                    self.assertGreaterEqual(actual_width - second, 190)
                    self.assertGreaterEqual(gui.session_group.winfo_height(), 110)
                    self.assertGreaterEqual(gui.session_group.winfo_y() - (
                        gui.project_group.winfo_y() + gui.project_group.winfo_height()), 12)
            finally:
                gui.close()

    def test_result_tabs_are_borderless_and_preserve_keyboard_selection_on_theme_switch(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="borderless-tabs-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            try:
                gui.account = {"id": 7, "username": "Tabs fixture", "role": "admin", "status": "active"}
                gui.account_online = True
                gui.show_page("workspace", force=True)
                root.geometry("1366x768")
                tabs = gui.result_tabs
                tabs.select(0)
                for key in ("herdr-midnight", "vscode-dark", "light"):
                    with self.subTest(theme=key):
                        selected = tabs.select()
                        gui.theme.set(THEMES[key][0])
                        gui.apply_theme()
                        root.update()
                        self.assertEqual(tabs.select(), selected)
                        layout = str(gui.style.layout("TNotebook.Tab"))
                        self.assertNotIn("Notebook.tab", layout)
                        self.assertNotIn("Notebook.focus", layout)
                        self.assertIn("Notebook.label", layout)
                        colors = THEMES[key][1]
                        self.assertEqual(gui.style.lookup("TNotebook.Tab", "foreground", ("selected",)),
                                         colors["heading"])
                        self.assertEqual(gui.style.lookup("TNotebook.Tab", "foreground"), colors["muted"])
                        tabs.focus_force()
                        root.update()
                        tabs.event_generate("<KeyPress-Right>")
                        root.update()
                        self.assertEqual(tabs.index(tabs.select()), 1)
                        tabs.event_generate("<KeyPress-Left>")
                        root.update()
                        self.assertEqual(tabs.index(tabs.select()), 0)
            finally:
                gui.close()

    def test_overlay_scrollbar_show_drag_hide_and_target_coverage(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="overlay-bars-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            errors = []
            root.report_callback_exception = lambda *_args: errors.append(_args)
            try:
                gui.account = {"id": 7, "username": "Overlay fixture", "role": "admin", "status": "active"}
                gui.account_online = True
                gui.show_page("workspace", force=True)
                root.geometry("1366x768")
                for index in range(90):
                    gui.project_tree.insert("", "end", text=f"Project {index}")
                    gui.running_task_list.insert("end", f"Session {index}")
                    gui.change_list.insert("end", f"Changed file {index}")
                    gui.activity.tree.insert("", "end", values=(f"Run {index}", "done"))
                for target in (gui.chat, gui.input):
                    target.configure(state="normal")
                    target.insert("end", "Scrollable response line\n" * 90)
                    if target is gui.chat:
                        target.configure(state="disabled")
                root.update()
                targets = (gui.project_tree, gui.running_task_list, gui.chat, gui.input,
                           gui.change_list, gui.activity.tree)
                for target in targets:
                    with self.subTest(target=target.winfo_class()):
                        if target is gui.change_list:
                            gui.result_tabs.select(0)
                        elif target is gui.activity.tree:
                            gui.result_tabs.select(1)
                        root.update()
                        overlay = next(item for item in gui.overlay_scrollbars if item.target is target)
                        width_before = overlay.parent.winfo_width()
                        target.yview_moveto(.55)
                        root.update()
                        overlay._enter()
                        root.update()
                        self.assertTrue(overlay.canvas.winfo_ismapped())
                        self.assertEqual(overlay.parent.winfo_width(), width_before)
                        old_first = target.yview()[0]
                        overlay._press(type("Event", (), {"y": overlay.thumb_start + 2, "x": 2})())
                        overlay._drag(type("Event", (), {"y": overlay.canvas.winfo_height(),
                                                           "x": overlay.canvas.winfo_width()})())
                        overlay._release(None)
                        root.update()
                        self.assertGreaterEqual(target.yview()[0], old_first)
                        overlay._leave()
                        overlay._hide()
                        root.update()
                        self.assertFalse(overlay.canvas.winfo_ismapped())
                gui.show_page("settings", force=True, section="updates")
                notes = gui.update_settings_notes_text
                self.assertIsNotNone(notes)
                gui.update_settings_release_section.pack(fill="x", padx=15, pady=(0, 10))
                notes.configure(state="normal")
                notes.insert("end", "release note\n" * 80)
                notes.configure(state="disabled")
                root.update()
                notes_bar = next(item for item in gui.overlay_scrollbars if item.target is notes)
                notes.yview_moveto(.5)
                root.update()
                self.assertTrue(notes_bar.canvas.winfo_ismapped())
                self.assertFalse(errors, errors)
            finally:
                gui.close()

    def test_theme_switch_preserves_composer_and_renders_markdown_paths(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="ide-markdown-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            try:
                gui.input.insert("1.0", "draft remains")
                gui.chat.configure(state="normal")
                gui.chat.delete("1.0", "end")
                source = "# 中文标题\n\n## English heading\n\nChanged src/app.py and `--flag`; see docs/readme.md beside example.py.\n\n> quoted text\n\n```py\ndef run(count: int):\n    return count + 42  # note\n```"
                insert_markdown(gui.chat, source)
                rendered = gui.chat.get("1.0", "end-1c")
                self.assertIn("Changed src/app.py", rendered)
                self.assertEqual(len(gui.chat.tag_ranges("md:path")), 4)
                self.assertEqual(len(gui.chat.tag_ranges("md:filename")), 2)
                self.assertIn("md:h1", gui.chat.tag_names("1.0"))
                self.assertTrue(gui.chat.tag_ranges("md:inline_code"))
                self.assertTrue(gui.chat.tag_ranges("md:syntax:keyword"))
                self.assertTrue(gui.chat.tag_ranges("md:syntax:function"))
                self.assertTrue(gui.chat.tag_ranges("md:syntax:type"))
                self.assertTrue(gui.chat.tag_ranges("md:syntax:number"))
                self.assertTrue(gui.chat.tag_ranges("md:syntax:comment"))
                self.assertIn("def run(count: int):", rendered)
                self.assertIn("return count + 42  # note", rendered)
                before = gui.input.get("1.0", "end-1c")
                for key in ("herdr-midnight", "vscode-dark", "light"):
                    gui.theme.set(THEMES[key][0])
                    gui.apply_preferences()
                    self.assertEqual(gui.input.get("1.0", "end-1c"), before)
                    self.assertEqual(gui.chat.get("1.0", "end-1c"), rendered)
                    self.assertEqual(gui.chat.tag_cget("md:h1", "foreground"),
                                     THEMES[key][1]["heading_1"])
            finally:
                gui.close()


    def test_python_token_highlighting_preserves_exact_source(self):
        code = "def run(count: int):\n    return count + 42  # note\n"
        spans = _python_highlight_spans(code)
        rebuilt = list(code)
        for start, end, _role in spans:
            self.assertTrue(0 <= start < end <= len(code))
        roles = [(code[start:end], role) for start, end, role in spans
                 if role in {"keyword", "function", "type", "number", "comment"}]
        self.assertEqual(roles, [("def", "keyword"), ("run", "function"), ("int", "type"),
                                 ("return", "keyword"), ("42", "number"), ("# note", "comment")])
        self.assertEqual("".join(rebuilt), code)

    def test_markdown_semantics_have_visible_colors_and_preserve_text_on_switch(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="markdown-colors-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            try:
                source = ("# 一级\n\n## 二级\n\n### 三级\n\n"
                          "正文 **加粗** 与 *强调*，路径 src/main.py，行内 `value` 和 `src/app.py`，"
                          "[链接](https://example.com)。\n\n- 列表项\n\n> 引用\n\n"
                          "**错误：** 无效参数\n\nWarning: 检查配置\n\n> [!SUCCESS] 完成\n\n"
                          "正文提到 error 一词不应自动变红。")
                gui.chat.configure(state="normal")
                gui.chat.delete("1.0", "end")
                insert_markdown(gui.chat, source)
                rendered = gui.chat.get("1.0", "end-1c")
                for key in ("herdr-midnight", "vscode-dark", "light"):
                    gui.theme.set(THEMES[key][0])
                    gui.apply_theme()
                    colors = THEMES[key][1]
                    for role, color in (("h1", "heading_1"), ("h2", "heading_2"), ("h3", "heading_3"),
                                        ("body", "text"), ("strong", "strong"), ("emphasis", "emphasis"),
                                        ("list_marker", "list_marker"), ("quote", "quote"),
                                        ("inline_code", "inline_code"), ("path", "path"), ("link", "link"),
                                        ("error", "error"), ("warning", "warning"), ("success", "success")):
                        self.assertTrue(gui.chat.tag_ranges(f"md:{role}"), role)
                        self.assertEqual(gui.chat.tag_cget(f"md:{role}", "foreground"), colors[color])
                    self.assertEqual(gui.chat.get("1.0", "end-1c"), rendered)
                index = gui.chat.search("正文提到", "1.0")
                self.assertNotIn("md:error", gui.chat.tag_names(index))
                index = gui.chat.search("src/app.py", "1.0")
                self.assertIn("md:path", gui.chat.tag_names(index))
                self.assertIn("md:inline_code", gui.chat.tag_names(index))
            finally:
                gui.close()

    def test_python_roles_cover_declarations_calls_parameters_and_punctuation(self):
        code = ('@cache\nclass Worker:\n    def run(self, count: int = 2):\n'
                '        result = helper(count) + len("value")  # keep comment\n'
                '        return result if True else None\n')
        spans = _python_highlight_spans(code)
        tokens = [(code[start:end], role) for start, end, role in spans]
        expected = {("Worker", "class"), ("run", "function"), ("helper", "function"),
                    ("count", "parameter"), ("result", "variable"), ("int", "type"),
                    ("len", "builtin"), ("cache", "decorator"), ("@", "decorator"),
                    ("def", "keyword"), ('"value"', "string"), ("2", "number"),
                    ("True", "constant"), ("# keep comment", "comment"),
                    ("+", "operator"), (":", "punctuation")}
        self.assertTrue(expected <= set(tokens), expected - set(tokens))
        for key in ("herdr-midnight", "vscode-dark"):
            colors = THEMES[key][1]
            self.assertEqual(len({colors[f"code_{role}"] for role in
                                  ("keyword", "string", "comment", "function", "variable", "parameter", "class")}), 7)
            self.assertNotEqual(colors["code_function"], colors["text"])

    def test_python_unicode_parameter_offsets_partial_snippets_and_exact_reconstruction(self):
        sources = ("def 转换(数量: int):\n    return 数量 + 2\n\n数量 = 3\n",
                   "def unfinished(value: int):\n    result =\n",
                   'message = f"value={42}"\n', 'broken = "unterminated')
        for code in sources:
            with self.subTest(code=code):
                spans = _python_highlight_spans(code)
                cursor = 0
                parts = []
                for start, end, role in spans:
                    self.assertTrue(cursor <= start < end <= len(code))
                    parts.extend((code[cursor:start], code[start:end]))
                    cursor = end
                parts.append(code[cursor:])
                self.assertEqual("".join(parts), code)
        code = sources[0]
        self.assertEqual([role for start, end, role in _python_highlight_spans(code)
                          if code[start:end] == "数量"], ["parameter", "parameter", "variable"])

    def test_other_language_fences_stay_neutral_and_horizontal_scrollbar_is_overlay(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT, prefix="neutral-code-") as folder:
            root = tk.Tk()
            gui = App(root, Path(folder), smoke=True)
            try:
                gui.chat.configure(state="normal")
                gui.chat.delete("1.0", "end")
                insert_markdown(gui.chat, '```js\nconst value = "def class"; // comment\n```')
                self.assertIn('const value = "def class";', gui.chat.get("1.0", "end"))
                self.assertFalse(any(gui.chat.tag_ranges(tag) for tag in gui.chat.tag_names()
                                     if tag.startswith("md:syntax:")))
                horizontal = [bar for bar in gui.overlay_scrollbars
                              if bar.target is gui.diff and bar.orient == "horizontal"]
                self.assertEqual(len(horizontal), 1)
                self.assertEqual(horizontal[0].canvas.winfo_class(), "Canvas")
            finally:
                gui.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
