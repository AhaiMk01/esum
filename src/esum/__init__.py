"""ESUM: tools for coding agents to read websites and make a few reversible, logged account actions.

The public module API (module format v1, see skills/esum-site-author/references/module-format.md):

    from esum import FORMAT, Tier, Transport, engine, site, action, Write, Result, Listing, Next, Post, Gap

Names load on first use, so `esum --version` answers before the HTTP stack is imported (D-43).
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

_EXPORTS = {
    "Context": ".core.context", "Options": ".core.context",
    "NoSuchItem": ".core.errors", "SiteRefused": ".core.errors",
    "ArgumentError": ".core.errors", "HttpError": ".core.errors", "LoginNeeded": ".core.errors", "ModuleFormatError": ".core.errors",
    "Write": ".core.gate",
    "Credential": ".transport",
    "FORMAT": ".core.model", "Engine": ".core.model", "Site": ".core.model", "Tier": ".core.model",
    "Transport": ".core.model", "action": ".core.model", "engine": ".core.model", "site": ".core.model",
    "command_line": ".core.runner", "other_site_command": ".core.runner",
    "Result": ".core.output", "Listing": ".core.output", "Next": ".core.output",
    "PageRead": ".core.paging", "read_pages": ".core.paging",
    "Mode": ".core.paging", "chunked": ".core.paging", "select_numbers": ".core.paging", "to_spans": ".core.paging",
    "Gap": ".core.posts", "Post": ".core.posts", "format_post": ".core.posts", "parse_date": ".core.posts",
    "parse_since": ".core.posts", "subtree": ".core.posts", "largest_subthreads": ".core.posts",
    "reply_counts": ".core.posts", "is_top_level": ".core.posts",
    "render_posts": ".core.posts", "render_thread": ".core.posts", "with_gaps": ".core.posts",
    "ThreadView": ".core.thread_view", "thread_view": ".core.thread_view",
    "read_spans": ".core.thread_view", "before_span": ".core.thread_view",
    "QUOTE_CHAR_CAP": ".core.text", "quote_html": ".core.text",
    "element_end": ".core.text", "element_inner": ".core.text", "remove_elements": ".core.text",
    "text_of": ".core.text", "clip": ".core.text",
    "html_to_body": ".core.text", "html_to_label": ".core.text", "html_to_text": ".core.text", "inner_div": ".core.text", "one_line": ".core.text",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'esum' has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


if TYPE_CHECKING:   # the same names, eagerly, for type checkers
    from .core.context import Context, Options  # noqa: F401
    from .core.errors import NoSuchItem, SiteRefused, ArgumentError, HttpError, LoginNeeded, ModuleFormatError  # noqa: F401
    from .core.gate import Write  # noqa: F401
    from .transport import Credential  # noqa: F401
    from .core.model import FORMAT, Engine, Site, Tier, Transport, action, engine, site  # noqa: F401
    from .core.output import Listing, Next, Result  # noqa: F401
    from .core.runner import command_line  # noqa: F401
    from .core.paging import PageRead, read_pages, Mode, chunked, select_numbers, to_spans  # noqa: F401
    from .core.posts import Gap, Post, format_post, parse_date, parse_since, render_posts, subtree, largest_subthreads, reply_counts, is_top_level, render_thread, with_gaps  # noqa: F401
    from .core.text import QUOTE_CHAR_CAP, html_to_body, html_to_label, html_to_text, inner_div, one_line, element_end, element_inner, remove_elements, quote_html, text_of, clip  # noqa: F401
    from .core.thread_view import ThreadView, before_span, read_spans, thread_view  # noqa: F401
