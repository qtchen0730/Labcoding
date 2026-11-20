"""Render CommonMark in a native Tk text widget."""

from dataclasses import dataclass
import ast
import builtins
import io
import keyword
import re
import tokenize
from urllib.parse import urlsplit
import webbrowser

from markdown_it import MarkdownIt


_PARSER = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
_HEADINGS = {"h1": 19, "h2": 16, "h3": 14, "h4": 12, "h5": 11, "h6": 10}
_URL_TAG = "md:url:"
_PATH_PATTERN = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/](?:[^\\/\s<>:\"|?*]+[\\/])*[^\\/\s<>:\"|?*]+"
    r"|(?:\.\.?[\\/])?[\w.-]+(?:[\\/][\w.-]+)+"
    r"|[\w.-]+\.(?:py|pyw|js|jsx|ts|tsx|json|toml|yaml|yml|md|txt|sql|html|css|bat|ps1))"
    r"(?![\w])",
    re.IGNORECASE,
)
_BLOCKS = {"body", "list", "quote", "code", "code_label", "table", "table_head", "rule", *_HEADINGS}
_SYNTAX_ROLES = {
    "keyword": "code_keyword", "string": "code_string", "function": "code_function",
    "type": "code_type", "number": "code_number", "comment": "code_comment",
    "class": "code_class", "parameter": "code_parameter", "variable": "code_variable",
    "builtin": "code_builtin", "constant": "code_constant", "decorator": "code_decorator",
    "operator": "code_operator", "punctuation": "code_punctuation",
}
_PYTHON_TYPES = {"bool", "bytearray", "bytes", "complex", "dict", "float", "frozenset",
                 "int", "list", "memoryview", "object", "range", "set", "str", "tuple", "type"}
_NOTICE_PREFIXES = {
    "error": re.compile(r"^(?:\[!CAUTION\]|\[!ERROR\]|错误[：:]|失败[：:]|error\s*:|❌)", re.I),
    "warning": re.compile(r"^(?:\[!WARNING\]|\[!IMPORTANT\]|警告[：:]|注意[：:]|warning\s*:|⚠)", re.I),
    "success": re.compile(r"^(?:\[!SUCCESS\]|成功[：:]|通过[：:]|success\s*:|✅)", re.I),
}


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

    text.tag_configure("md:body", foreground=colors["text"], spacing1=2, spacing2=3, spacing3=7)
    text.tag_configure("md:list", spacing1=1, spacing2=3, spacing3=4)
    text.tag_configure("md:quote", foreground=colors.get("quote", colors["muted"]),
                       spacing2=3, spacing3=5, lmargin1=14, lmargin2=14)
    for heading in _HEADINGS:
        level = int(heading[1])
        text.tag_configure(f"md:{heading}", foreground=colors.get(f"heading_{min(level, 4)}", colors["text"]),
                           spacing1=10, spacing3=5)

    code_bg = colors.get("code", colors["bg"])
    text.tag_configure("md:code", background=code_bg, foreground=colors["text"], spacing2=1)
    text.tag_configure("md:code_label", background=code_bg, foreground=colors["muted"],
                       spacing1=7, spacing3=4)
    text.tag_configure("md:inline_code", background=code_bg,
                       foreground=colors.get("inline_code", colors["heading"]))
    text.tag_configure("md:strong", foreground=colors.get("strong", colors["heading"]))
    text.tag_configure("md:emphasis", foreground=colors["emphasis"])
    for role in _NOTICE_PREFIXES:
        text.tag_configure(f"md:{role}", foreground=colors[role])
    text.tag_configure("md:list_marker", foreground=colors.get("list_marker", colors["heading"]),
                       font=(font_family, 11, "bold"))
    for role, token_role in _SYNTAX_ROLES.items():
        text.tag_configure(f"md:syntax:{role}", foreground=colors.get(token_role, colors["heading"]))
    text.tag_configure("md:table", spacing1=4, spacing3=3)
    text.tag_configure("md:table_head", background=code_bg, foreground=colors["text"],
                       spacing1=6, spacing3=6)
    text.tag_configure("md:rule", foreground=colors["border"], spacing1=5, spacing3=5)
    text.tag_configure("md:link", foreground=colors.get("link", colors["heading"]), underline=True)
    text.tag_configure("md:path", foreground=colors.get("path", colors["heading"]))
    text.tag_configure("md:filename", foreground=colors.get("filename", colors["heading"]))
    text.tag_configure("md:strike", overstrike=True)

    for level in range(1, 9):
        margin = level * 16
        text.tag_configure(f"md:indent:{level}", lmargin1=margin, lmargin2=margin)
        text.tag_configure(f"md:bullet:{level}", lmargin1=margin, lmargin2=margin + 22)

    text.tag_bind("md:link", "<Enter>", lambda _event: text.configure(cursor="hand2"))
    text.tag_bind("md:link", "<Leave>", lambda _event: text.configure(cursor="xterm"))
    text.tag_bind("md:link", "<ButtonRelease-1>", lambda event: _follow_link(text, event))
    for role in ("emphasis", "strong", *_NOTICE_PREFIXES, "inline_code", "path", "filename",
                 "link", "list_marker", *_SYNTAX_ROLES):
        text.tag_raise(f"md:{'syntax:' if role in _SYNTAX_ROLES else ''}{role}")


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
                if bold:
                    tags.append("md:strong")
                if italic:
                    tags.append("md:emphasis")
                if code:
                    tags.append("md:inline_code")
                    if _PATH_PATTERN.fullmatch(value):
                        tags.append("md:path" if "/" in value or "\\" in value else "md:filename")
                if strike:
                    tags.append("md:strike")
                if link:
                    tags.extend(("md:link", _URL_TAG + link))
                if kind == "text" and value and not link:
                    cursor = 0
                    for match in _PATH_PATTERN.finditer(value):
                        self.emit(value[cursor:match.start()], role, *tags)
                        tag = "md:path" if any(separator in match.group() for separator in ("/", "\\")) else "md:filename"
                        self.emit(match.group(), role, *tags, tag)
                        cursor = match.end()
                    self.emit(value[cursor:], role, *tags)
                else:
                    self.emit(value, role, *tags)

    def indent(self, *, increment=0, bullet=False):
        level = min(8, self.quotes + len(self.lists) + increment)
        if level:
            name = "bullet" if bullet else "indent"
            return (f"md:{name}:{level}",)
        return ()

    def list_prefix(self, role, extra):
        if self.lists and self.lists[-1].prefix:
            self.emit(self.lists[-1].prefix + "\u2002", role, *extra, "md:list_marker")
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
        # Only explicit leading labels/alert markers imply status; ordinary prose does not.
        if not self.heading:
            plain = "".join(token.content for token in tokens if token.type in {"text", "code_inline"}).lstrip()
            for notice, pattern in _NOTICE_PREFIXES.items():
                if pattern.match(plain):
                    extra = (*extra, f"md:{notice}")
                    break
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
        content = content if content.endswith("\n") else content + "\n"
        base_tags = (*extra, _font("code", False, False, False))
        spans = _python_highlight_spans(content) if language.lower() in {"py", "python", "python3"} else ()
        cursor = 0
        for start, end, role in spans:
            if start > cursor:
                self.emit(content[cursor:start], "code", *base_tags)
            self.emit(content[start:end], "code", *base_tags, f"md:syntax:{role}")
            cursor = end
        self.emit(content[cursor:], "code", *base_tags)


def _python_highlight_spans(source):
    """Color Python tokens without executing code; AST adds parameter scopes when parseable."""
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def offset(position):
        row, column = position
        return min(len(source), offsets[min(max(0, row - 1), len(offsets) - 1)] + column)

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (IndentationError, SyntaxError, tokenize.TokenError):
        return ()

    parameter_positions = set()
    classes = set()
    functions = set()

    def position(node):
        # AST columns are UTF-8 byte offsets; Tk/tokenize use character offsets.
        prefix = lines[node.lineno - 1].encode("utf-8")[:node.col_offset]
        return node.lineno, len(prefix.decode("utf-8"))

    class Parameters(ast.NodeVisitor):
        def __init__(self):
            self.scopes = []

        def visit_Name(self, node):
            if any(node.id in scope for scope in reversed(self.scopes)):
                parameter_positions.add(position(node))

        def function(self, node):
            args = node.args
            arguments = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            arguments += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
            for value in (*args.defaults, *args.kw_defaults,
                          *getattr(node, "decorator_list", ()), getattr(node, "returns", None)):
                if value is not None:
                    self.visit(value)
            for argument in arguments:
                parameter_positions.add(position(argument))
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            self.scopes.append({arg.arg for arg in arguments})
            if isinstance(node, ast.Lambda):
                self.visit(node.body)
            else:
                for statement in node.body:
                    self.visit(statement)
            self.scopes.pop()

        visit_FunctionDef = function
        visit_AsyncFunctionDef = function
        visit_Lambda = function

        def visit_keyword(self, node):
            if node.arg is not None:
                parameter_positions.add(position(node))
            self.visit(node.value)

    try:
        tree = ast.parse(source)
        classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        functions = {node.name for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        Parameters().visit(tree)
    except (SyntaxError, ValueError, RecursionError):
        # Partial streamed snippets still receive lexical highlighting.
        pass

    significant = [token for token in tokens if token.type not in {
        tokenize.ENCODING, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT,
        tokenize.NL, tokenize.NEWLINE}]
    spans = []
    decorator = False
    for index, token in enumerate(significant):
        kind = token.type
        previous = significant[index - 1].string if index else ""
        following = significant[index + 1].string if index + 1 < len(significant) else ""
        if index and token.start[0] != significant[index - 1].end[0]:
            decorator = False
        role = None
        if kind == tokenize.NAME:
            if previous == "class":
                role = "class"
            elif previous == "def":
                role = "function"
            elif token.string in {"True", "False", "None"}:
                role = "constant"
            elif token.string in keyword.kwlist:
                role = "keyword"
            elif decorator:
                role = "decorator"
            elif token.start in parameter_positions:
                role = "parameter"
            elif token.string in classes:
                role = "class"
            elif token.string in functions:
                role = "function"
            elif token.string.isupper():
                role = "constant"
            elif token.string in _PYTHON_TYPES:
                role = "type"
            elif token.string in vars(builtins) and previous != ".":
                role = "builtin"
            elif following == "(":
                role = "function"
            else:
                role = "variable"
        elif kind == tokenize.STRING or tokenize.tok_name.get(kind, "").startswith("FSTRING_"):
            role = "string"
        elif kind == tokenize.NUMBER:
            role = "number"
        elif kind == tokenize.COMMENT:
            role = "comment"
        elif kind == tokenize.OP:
            if token.string == "@" and not token.line[:token.start[1]].strip():
                decorator = True
                role = "decorator"
            else:
                role = "punctuation" if token.string in {"(", ")", "[", "]", "{", "}", ":", ",", ".", ";"} else "operator"
            if token.string == "(":
                decorator = False
        start, end = offset(token.start), offset(token.end)
        if role and end > start:
            spans.append((start, end, role))
    return tuple(spans)


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
