"""Named application palettes; preference IDs remain stable across translations."""

DARK = dict(bg="#10151d", panel="#17202b", field="#1d2937", text="#edf2f8",
            muted="#a3b2c6", border="#344457", accent="#a9b9ff", ink="#12182e",
            add="#9be6aa", remove="#ffb3b8", heading="#b2c4ff",
            side="#141c26", soft="#233046", add_bg="#1b372e", remove_bg="#442c38")
LIGHT = dict(bg="#eef0f5", panel="#ffffff", field="#f5f6fa", text="#191f30",
             muted="#566278", border="#dbe1ed", accent="#4056bf", ink="#ffffff",
             add="#146135", remove="#9f223f", heading="#344993",
             side="#f8f9fd", soft="#ecf0fe", add_bg="#e8f7ed", remove_bg="#fceef0")

THEMES = {
    "light": ("经典浅色", LIGHT),
    "dark": ("经典深色", DARK),
    "warm": ("暖沙米白", dict(LIGHT, bg="#eee6da", panel="#fcf8f0", field="#f3ecdf",
                            side="#f5efe4", soft="#e9dfcc", border="#d9cbb6",
                            text="#30291f", muted="#6b5d49", accent="#87602c", heading="#795025")),
    "mint": ("薄荷绿", dict(LIGHT, bg="#e5efea", panel="#f6fbf8", field="#edf5f0",
                         side="#eef7f1", soft="#dcece2", border="#c5d9cc",
                         text="#19362a", muted="#506d5d", accent="#28664c", heading="#225d43")),
    "sky": ("晴空蓝", dict(LIGHT, bg="#e5eef7", panel="#f7fbff", field="#edf4fb",
                        side="#eef5fc", soft="#dceaf8", border="#c5d7ea",
                        text="#203348", muted="#536b84", accent="#32669b", heading="#285b91")),
    "ocean": ("深海蓝", dict(DARK, bg="#0b1927", panel="#102438", field="#173047",
                          side="#0d2032", soft="#1c3a54", border="#34546e",
                          text="#e8f2fc", muted="#a0bad0", accent="#8ecbfa", ink="#102438",
                          heading="#a0d5ff")),
    "forest": ("静谧墨绿", dict(DARK, bg="#101e18", panel="#172b22", field="#20382d",
                            side="#14261d", soft="#294537", border="#3f5e4b",
                            text="#edf5ec", muted="#adc2b0", accent="#b0d5ad", ink="#15271c",
                            heading="#c3dfb9")),
    "plum": ("暮色紫", dict(DARK, bg="#1b1729", panel="#251f37", field="#302842",
                         side="#211b31", soft="#3a3050", border="#514463",
                         text="#f0ebfa", muted="#bcb0cf", accent="#d1b4f3", ink="#281d39",
                         heading="#dec6fa")),
}


def theme_key(value):
    """Accept existing light/dark IDs, current IDs and UI labels; fail to light."""
    if isinstance(value, str):
        if value in THEMES:
            return value
        for key, (label, _colors) in THEMES.items():
            if value == label:
                return key
    return "light"
