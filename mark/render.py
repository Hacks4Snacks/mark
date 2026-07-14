from __future__ import annotations

import functools
from typing import ClassVar

from markdown_it import MarkdownIt
from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.style import Style
from pygments.token import (
    Comment,
    Error,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Token,
)
from pygments.util import ClassNotFound


class MarkDarkStyle(Style):
    """Syntax colors tuned to Mark's dark palette (purple/cyan/green/amber)."""

    background_color = "transparent"
    styles: ClassVar[dict] = {
        Token: "#e8eaf0",
        Comment: "italic #6b7488",
        Comment.Preproc: "#c98bdb",
        Keyword: "#8b8cff",
        Keyword.Constant: "#ffd666",
        Keyword.Type: "#59c2ff",
        Name: "#e8eaf0",
        Name.Function: "#59c2ff",
        Name.Class: "#ffd666",
        Name.Builtin: "#ffd666",
        Name.Builtin.Pseudo: "#c98bdb",
        Name.Decorator: "#c98bdb",
        Name.Tag: "#8b8cff",
        Name.Attribute: "#59c2ff",
        Name.Constant: "#ffd666",
        Name.Variable: "#e8eaf0",
        String: "#56d6a0",
        String.Doc: "italic #6b7488",
        String.Escape: "#ffb02e",
        String.Interpol: "#ffb02e",
        Number: "#ffb02e",
        Operator: "#9aa4b6",
        Operator.Word: "#8b8cff",
        Punctuation: "#9aa4b6",
        Generic.Deleted: "#e5534b",
        Generic.Inserted: "#56d6a0",
        Generic.Emph: "italic",
        Generic.Strong: "bold",
        Generic.Heading: "bold #8b8cff",
        Generic.Subheading: "bold #59c2ff",
        Error: "#e5534b",
    }


class MarkLightStyle(Style):
    """Syntax colors tuned to Mark's light palette."""

    background_color = "transparent"
    styles: ClassVar[dict] = {
        Token: "#1b2330",
        Comment: "italic #717a8a",
        Comment.Preproc: "#8a3fbf",
        Keyword: "#5b5bf0",
        Keyword.Constant: "#b26a00",
        Keyword.Type: "#1597e6",
        Name: "#1b2330",
        Name.Function: "#1597e6",
        Name.Class: "#b26a00",
        Name.Builtin: "#b26a00",
        Name.Builtin.Pseudo: "#8a3fbf",
        Name.Decorator: "#8a3fbf",
        Name.Tag: "#5b5bf0",
        Name.Attribute: "#1597e6",
        Name.Constant: "#b26a00",
        Name.Variable: "#1b2330",
        String: "#16a06f",
        String.Doc: "italic #717a8a",
        String.Escape: "#b26a00",
        String.Interpol: "#b26a00",
        Number: "#b26a00",
        Operator: "#535c6b",
        Operator.Word: "#5b5bf0",
        Punctuation: "#535c6b",
        Generic.Deleted: "#c5403b",
        Generic.Inserted: "#16a06f",
        Generic.Emph: "italic",
        Generic.Strong: "bold",
        Generic.Heading: "bold #5b5bf0",
        Generic.Subheading: "bold #1597e6",
        Error: "#c5403b",
    }


_FORMATTER = HtmlFormatter(nowrap=False, cssclass="hl", style=MarkDarkStyle)

# Pygments emits one <span> per token, so a single very large code block can
# explode into thousands of DOM nodes. A pathological agent transcript (whole-file
# dumps, long command output, big diffs) can reach tens of thousands of spans,
# which is what makes the detail view slow to lay out, paint, and scroll. Past a
# few KB the syntax colours add little to those blocks but cost a lot, so above
# the cap we skip tokenising and let markdown-it emit a plain escaped <pre>.
_MAX_HIGHLIGHT_CHARS = 4000


def _highlight(code: str, lang: str | None, _attrs) -> str:
    if len(code) > _MAX_HIGHLIGHT_CHARS:
        return ""  # too large to tokenise cheaply; render as a plain <pre>
    try:
        lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
    except (ClassNotFound, ValueError):
        try:
            lexer = guess_lexer(code)
        except (ClassNotFound, ValueError):
            return ""  # let markdown-it escape it in a plain <pre>
    return _pyg_highlight(code, lexer, _FORMATTER)


@functools.lru_cache(maxsize=1)
def _md() -> MarkdownIt:
    md = MarkdownIt(
        "commonmark", {"html": False, "linkify": False, "highlight": _highlight}
    )
    md.enable(["table", "strikethrough"])
    return md


def render_markdown(text: str | None) -> str:
    if not text:
        return ""
    return _md().render(text)


@functools.lru_cache(maxsize=1)
def pygments_css() -> str:
    # Token CSS classes are style-independent, so we emit one scoped block per
    # theme; the active [data-theme] on <html> selects the right palette. The
    # code-block background itself is handled by styles.css (var(--code-bg)).
    dark = HtmlFormatter(cssclass="hl", style=MarkDarkStyle).get_style_defs(
        '[data-theme="dark"] .hl'
    )
    light = HtmlFormatter(cssclass="hl", style=MarkLightStyle).get_style_defs(
        '[data-theme="light"] .hl'
    )
    return f"{dark}\n{light}"
