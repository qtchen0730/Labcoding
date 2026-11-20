"""Named application palettes and shared semantic UI tokens."""


DARK = dict(bg="#10151d", panel="#17202b", field="#1d2937", text="#edf2f8",
            muted="#a3b2c6", border="#344457", accent="#a9b9ff", ink="#12182e",
            add="#9be6aa", remove="#ffb3b8", heading="#b2c4ff",
            side="#141c26", soft="#233046", add_bg="#1b372e", remove_bg="#442c38",
            header="#151b24")
LIGHT = dict(bg="#eef0f5", panel="#ffffff", field="#f5f6fa", text="#191f30",
             muted="#566278", border="#dbe1ed", accent="#4056bf", ink="#ffffff",
             add="#146135", remove="#9f223f", heading="#344993",
             side="#f8f9fd", soft="#ecf0fe", add_bg="#e8f7ed", remove_bg="#fceef0",
             header="#ffffff")

DEFAULT_THEME = "vscode-dark"

THEMES = {
    "light": ("经典浅色", LIGHT),
    "dark": ("经典深色", DARK),
    "warm": ("暖沙米白", dict(LIGHT, bg="#eee6da", panel="#fcf8f0", field="#f3ecdf",
                            side="#f5efe4", soft="#e9dfcc", border="#d9cbb6",
                            text="#30291f", muted="#6b5d49", accent="#87602c", heading="#795025",
                            header="#fcf8f0")),
    "mint": ("薄荷绿", dict(LIGHT, bg="#e5efea", panel="#f6fbf8", field="#edf5f0",
                         side="#eef7f1", soft="#dcece2", border="#c5d9cc",
                         text="#19362a", muted="#506d5d", accent="#28664c", heading="#225d43",
                         header="#f6fbf8")),
    "sky": ("晴空蓝", dict(LIGHT, bg="#e5eef7", panel="#f7fbff", field="#edf4fb",
                        side="#eef5fc", soft="#dceaf8", border="#c5d7ea",
                        text="#203348", muted="#536b84", accent="#32669b", heading="#285b91",
                        header="#f7fbff")),
    "vscode-dark": ("Dark", dict(
        DARK, bg="#1e1e1e", panel="#1e1e1e", field="#252526", side="#181818",
        soft="#2a2d2e", border="#29292B", text="#d4d4d4", muted="#a0a0a0",
        accent="#75a7ff", ink="#101820", heading="#9dbfff", add="#8bcf9b",
        remove="#e69a9a", add_bg="#26362b", remove_bg="#3c292b", header="#181818",
        sidebar_card="#202022", sidebar_card_line="#29292B")),
    "herdr-midnight": ("Midnight", dict(
        DARK, bg="#0A0A0A", panel="#0A0A0A", field="#141414", side="#101114",
        soft="#262629", border="#17181B", text="#EEEEEE", muted="#A7ADB5",
        accent="#9B91EF", ink="#101114", heading="#AE92FF", add="#80CFA1",
        remove="#EF9191", add_bg="#1b3027", remove_bg="#302020", header="#17191D",
        sidebar_card="#141414", sidebar_card_line="#1C1D20", result_panel="#141414")),
}

# Python hues follow VS Code Dark+ roles; Markdown has its own reading hierarchy.
_DARK_CONTENT = dict(
    heading_1="#E9A566", heading_2="#C6A0E8", heading_3="#80CBC4", heading_4="#A6BCE8",
    strong="#E5D49A", emphasis="#D6A0BD", list_marker="#C1B18B", quote="#9BA9BC",
    inline_code="#DBA38F", filename="#B1C9EC", path="#78CBB9", link="#75B7F0",
    error="#F08B8B", warning="#E6CB70", success="#91D57B",
    code_keyword="#C586C0", code_string="#CE9178", code_number="#B5CEA8",
    code_constant="#4FC1FF", code_comment="#7F9D6C", code_function="#DCDCAA",
    code_class="#4EC9B0", code_parameter="#D7BA7D", code_variable="#9CDCFE",
    code_type="#70C8D7", code_builtin="#7CB5E8", code_decorator="#D4B6E8",
    code_operator="#D4D4D4", code_punctuation="#ADB5C2")
_LIGHT_CONTENT = dict(
    heading_1="#875018", heading_2="#7950A0", heading_3="#146D73", heading_4="#425B91",
    strong="#725813", emphasis="#914768", list_marker="#7A5C32", quote="#576579",
    inline_code="#93492C", filename="#435C97", path="#25683E", link="#165EAA",
    error="#A52A35", warning="#825310", success="#246742",
    code_keyword="#884A91", code_string="#93472B", code_number="#3A681E",
    code_constant="#205E98", code_comment="#506A45", code_function="#735E1D",
    code_class="#176D5A", code_parameter="#865326", code_variable="#275B80",
    code_type="#1C6474", code_builtin="#3A5A9C", code_decorator="#77508E",
    code_operator="#444B55", code_punctuation="#5B6370")

_REMOVED_THEME_MIGRATION = {
    "VS Code Dark": "vscode-dark", "Herdr Midnight": "herdr-midnight",
    "forest": "mint", "静谧墨绿": "mint",
    "plum": "herdr-midnight", "暮色紫": "herdr-midnight",
    "ocean": "vscode-dark", "深海蓝": "vscode-dark",
}


def _semantic_tokens(colors):
    """Give every palette the same semantic roles used by Tk and ttk widgets."""
    accent = colors["accent"]
    text = colors["text"]
    muted = colors["muted"]
    dark = sum(int(colors["bg"][index:index + 2], 16) for index in (1, 3, 5)) < 435
    defaults = dict(_DARK_CONTENT if dark else _LIGHT_CONTENT, secondary=muted)
    result = dict(defaults, **colors)
    def mix(first, second, ratio):
        left = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
        right = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
        values = (round(a * (1 - ratio) + b * ratio) for a, b in zip(left, right))
        return "#" + "".join(f"{value:02X}" for value in values)

    thumb = mix(result["side"], result["muted"], .28)
    thumb_hover = mix(result["side"], result["muted"], .55)
    line = mix(result["panel"], result["border"], .5)
    return dict(result,
                window=result["bg"], sidebar=result["side"], editor=result["panel"],
                container=result["field"], text_primary=text, text_secondary=muted,
                text_disabled=muted, accent_hover=accent, pressed=result["border"],
                line=line, separator=line,
                input=result["field"], selection=result["soft"],
                input_bg=result["field"], selected_bg=result["soft"],
                hover=result["soft"], active=result["border"], focus=accent,
                scroll_track=result["bg"], scrollbar=result["border"],
                scrollbar_thumb=thumb, scrollbar_hover=thumb_hover,
                code=result["field"], code_border=line,
                danger=result["remove"], danger_bg=result["remove_bg"],
                user_message=result["soft"], header=result["header"],
                sidebar_card=result.get("sidebar_card", result["side"]),
                sidebar_card_line=result.get("sidebar_card_line", result["border"]),
                result_panel=result.get("result_panel", result["field"]))


for _key, (_label, _colors) in tuple(THEMES.items()):
    THEMES[_key] = (_label, _semantic_tokens(_colors))


def migrate_default_theme(prefs):
    """Default new profiles to Dark; preserve saved choices and migrate old labels."""
    changed = False
    current = prefs.get("theme")
    mapped = _REMOVED_THEME_MIGRATION.get(current) if isinstance(current, str) else None
    if mapped:
        prefs["theme"] = mapped
        changed = True
    elif current is None:
        prefs["theme"] = DEFAULT_THEME
        changed = True
    elif not isinstance(current, str) or (
            current not in THEMES and current not in {label for label, _colors in THEMES.values()}):
        prefs["theme"] = DEFAULT_THEME
        changed = True
    revision = prefs.get("theme_defaults_version")
    if not (type(revision) is int and revision >= 11):
        prefs["theme_defaults_version"] = 11
        changed = True
    return changed


def theme_key(value):
    """Accept saved IDs and current/legacy labels; default to Dark."""
    if isinstance(value, str):
        if value in THEMES:
            return value
        for key, (label, _colors) in THEMES.items():
            if value == label:
                return key
        if value in _REMOVED_THEME_MIGRATION:
            return _REMOVED_THEME_MIGRATION[value]
    return DEFAULT_THEME
