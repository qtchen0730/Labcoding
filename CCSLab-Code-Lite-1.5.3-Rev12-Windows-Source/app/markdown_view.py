"""Render CommonMark in a native Tk text widget."""

from dataclasses import dataclass
from urllib.parse import urlsplit
import webbrowser

from markdown_it import MarkdownIt


_PARSER = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
_HEADINGS = {"h1": 19, "h2": 16, "h3": 14, "h4": 12, "h5": 11, "h6": 10}
_URL_TAG = "md:url:"
_BLOCKS = {"body", "list", "quote", "code", "code_label", "table", "table_head", "rule", *_HEADINGS}


def configure_markdown(text, colors, font_family="Microsoft YaHei UI", zoom=0):
    """Configure rich-text fonts and colors for an active theme."""
    for role in _BLOCKS:
        for bold in (False, True):
            for italic in (False, True):
                for code in (False, True):
                    size = _HEADINGS.get(role, 10 if role == "code_label" else 11) + zoom
                    font = (
                        "Consolas" if code or role == "code" else font_family,
                        size,
                        "bold" if bold or role in _HEADINGS or role in {"code_label", "table_head"} else "normal",
                        "italic" if italic else "roman",
                    )
                    text.tag_configure(_font(role, bold, italic, code), font=font)

    text.tag_configure("md:body", spacing1=2, spacing2=3, spacing3=7)
    text.tag_configure("md:list", spacing1=1, spacing2=3, spacing3=4)
    text.tag_configure("md:quote", foreground=colors["muted"], spacing2=3, spacing3=5)
    for heading in _HEADINGS:
        text.tag_configure(f"md:{heading}", foreground=colors["text"], spacing1=10, spacing3=5)

    code_bg = colors.get("code", colors["bg"])
    text.tag_configure("md:code", background=code_bg, foreground=colors["text"], spacing2=1)
    text.tag_configure("md:code_label", background=code_bg, foreground=colors["muted"],
                       spacing1=7, spacing3=4)
    text.tag_configure("md:inline_code", background=code_bg, foreground=colors["heading"])
    text.tag_configure("md:table", spacing1=4, spacing3=3)
    text.tag_configure("md:table_head", background=code_bg, foreground=colors["text"],
                       spacing1=6, spacing3=6)
    text.tag_configure("md:rule", foreground=colors["border"], spacing1=5, spacing3=5)
    text.tag_configure("md:link", foreground=colors["heading"], underline=True)
    text.tag_configure("md:strike", overstrike=True)

    for level in range(1, 9):
        margin = level * 16
        text.tag_configure(f"md:indent:{level}", lmargin1=margin, lmargin2=margin)
        text.tag_configure(f"md:bullet:{level}", lmargin1=margin, lmargin2=margin + 22)

    text.tag_bind("md:link", "<Enter>", lambda _event: text.configure(cursor="hand2"))
    text.tag_bind("md:link", "<Leave>", lambda _event: text.configure(cursor="xterm"))
    text.tag_bind("md:link", "<ButtonRelease-1>", lambda event: _follow_link(text, event))


def _font(role, bold, italic, code):
    return f"md:font:{role}:{int(bold)}{int(italic)}{int(code)}"


def _safe_url(value):
    if not value or len(value) > 2048 or any(ord(character) < 32 for character in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {"https", "http"} and url.hostname and url.username is None and url.password is None:
            return value
    except ValueError:
        pass
    return None


def _follow_link(text, event):
    if text.tag_ranges("sel"):
        return
    index = text.index(f"@{event.x},{event.y}")
    for tag in text.tag_names(index):
        if tag.startswith(_URL_TAG):
            url = _safe_url(tag[len(_URL_TAG):])
            if url:
                try:
                    webbrowser.open_new_tab(url)
                except (OSError, webbrowser.Error):
                    pass
            return


@dataclass
class _List:
    counter: int | None
    prefix: str = ""

    def next_item(self):
        if self.counter is None:
            self.prefix = "•"
        else:
            self.prefix = f"{self.counter}."
            self.counter += 1


class _Layout:
    def __init__(self):
        self.runs = []
        self.lists = []
        self.quotes = 0
        self.heading = None
        self.table_role = None
        self.table_columns = 0

    def emit(self, value, role, *tags):
        if value:
            styles = (f"md:{role}", *tags)
            if self.runs and self.runs[-1][1] == styles:
                previous, _ = self.runs[-1]
                self.runs[-1] = (previous + value, styles)
            else:
                self.runs.append((value, styles))

    def content(self, tokens, role, extra=(), first_skip=0):
        bold, italic, strike, link = 0, 0, 0, None
        for index, token in enumerate(tokens):
            kind = token.type
            if kind.startswith("strong_"):
                bold += token.nesting
            elif kind.startswith("em_"):
                italic += token.nesting
            elif kind.startswith("s_"):
                strike += token.nesting
            elif kind == "link_open":
                link = _safe_url(token.attrGet("href"))
            elif kind == "link_close":
                link = None
            else:
                code = kind == "code_inline"
                if kind in {"softbreak", "hardbreak"}:
                    value = "\n" if kind == "hardbreak" else " "
                elif kind == "image":
                    alt = "".join(child.content for child in token.children or []
                                  if child.type in {"text", "code_inline"})
                    value = f"［图片：{alt}］" if alt else "［图片］"
                else:
                    value = token.content
                if index == 0 and first_skip:
                    value = value[first_skip:]
                tags = [*extra, _font(role, bool(bold), bool(italic), code)]
                if code:
                    tags.append("md:inline_code")
                if strike:
                    tags.append("md:strike")
                if link:
                    tags.extend(("md:link", _URL_TAG + link))
                self.emit(value, role, *tags)

    def indent(self, *, increment=0, bullet=False):
        level = min(8, self.quotes + len(self.lists) + increment)
        if level:
            name = "bullet" if bullet else "indent"
            return (f"md:{name}:{level}",)
        return ()

    def list_prefix(self, role, extra):
        if self.lists and self.lists[-1].prefix:
            self.emit(self.lists[-1].prefix + "\u2002", role, *extra)
            self.lists[-1].prefix = ""

    def token(self, token):
        kind = token.type
        if kind == "heading_open":
            self.heading = token.tag
        elif kind == "heading_close":
            self.heading = None
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            start = int(token.attrGet("start") or 1) if kind == "ordered_list_open" else None
            self.lists.append(_List(start))
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            self.lists.pop()
        elif kind == "list_item_open":
            self.lists[-1].next_item()
        elif kind == "blockquote_open":
            self.quotes += 1
        elif kind == "blockquote_close":
            self.quotes -= 1
        elif kind == "thead_open":
            self.table_role = "table_head"
        elif kind == "tbody_open":
            self.table_role = "table"
        elif kind == "table_close":
            self.table_role = None
        elif kind == "tr_open":
            self.table_columns = 0
        elif kind in {"th_open", "td_open"}:
            if self.table_columns:
                self.emit("  │  ", self.table_role, *self.indent())
            self.table_columns += 1
        elif kind == "tr_close":
            self.emit("\n", self.table_role, *self.indent())
        elif kind == "inline":
            self.inline_block(token.children or [])
        elif kind in {"fence", "code_block"}:
            self.code_block(token)
        elif kind == "hr":
            self.emit("────────────────────\n", "rule", *self.indent())

    def inline_block(self, tokens):
        if self.table_role:
            self.content(tokens, self.table_role, self.indent())
            return
        role = self.heading or ("list" if self.lists else "quote" if self.quotes else "body")
        bullet = bool(self.lists and self.lists[-1].prefix)
        extra = self.indent(increment=bool(self.lists and not bullet), bullet=bullet)
        skip = 0
        if bullet and self.lists[-1].counter is None and tokens and tokens[0].type == "text":
            marker = tokens[0].content[:4].lower()
            if marker in {"[ ] ", "[x] "}:
                self.lists[-1].prefix = "☐" if marker == "[ ] " else "☑"
                skip = len(marker)
        self.list_prefix(role, extra)
        self.content(tokens, role, extra, first_skip=skip)
        self.emit("\n", role, *extra)

    def code_block(self, token):
        pending_item = bool(self.lists and self.lists[-1].prefix)
        if pending_item:
            self.list_prefix("list", self.indent(bullet=True))
            self.emit("\n", "list", *self.indent())
        extra = self.indent(increment=1)
        language = (token.info.split(maxsplit=1) or ["代码"])[0][:36]
        self.emit(language + "\n", "code_label", *extra, _font("code_label", False, False, False))
        content = token.content
        self.emit(content if content.endswith("\n") else content + "\n", "code",
                  *extra, _font("code", False, False, False))


def insert_markdown(text, source):
    """Append rendered Markdown to an editable Text, without altering history."""
    for tag in text.tag_names():
        if tag.startswith(_URL_TAG) and not text.tag_ranges(tag):
            text.tag_delete(tag)
    layout = _Layout()
    for token in _PARSER.parse(source):
        layout.token(token)
    if layout.runs:
        args = [item for run in layout.runs for item in run]
        text.insert("end", *args)
