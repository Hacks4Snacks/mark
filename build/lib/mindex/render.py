"""Server-side Markdown rendering for assistant responses.

Raw HTML in the source is disabled, so conversation content cannot inject
markup into the page (XSS-safe). Fenced code blocks are highlighted with
Pygments at render time.
"""
from __future__ import annotations

import functools

from markdown_it import MarkdownIt
from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

_FORMATTER = HtmlFormatter(nowrap=False, cssclass="hl")


def _highlight(code: str, lang: str | None, _attrs) -> str:
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
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "highlight": _highlight})
    md.enable(["table", "strikethrough"])
    return md


def render_markdown(text: str | None) -> str:
    if not text:
        return ""
    return _md().render(text)


@functools.lru_cache(maxsize=1)
def pygments_css() -> str:
    return HtmlFormatter(cssclass="hl").get_style_defs(".hl")
