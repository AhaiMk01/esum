"""HTML to plain text for post bodies (Discourse `cooked`, WordPress content, vBulletin posts).

Deliberately small: block elements become line breaks, quotes are prefixed with `> `, table cells
are separated by ` | `, scripts and styles are dropped, and links keep their URL when it differs
from the link text. Raw newlines are whitespace except inside `<pre>`. An image the page declares
as tiny (pixels, spacers, icons) loses its URL, so it prints as its alt text or not at all.
"""

from __future__ import annotations

import re
import secrets
from html import escape, unescape
from html.parser import HTMLParser

_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
          "pre", "hr", "section", "article", "aside", "header", "footer", "dd", "dt", "figure"}
_SKIP = {"script", "style", "noscript", "template", "svg"}
_STRUCK = {"del", "s", "strike"}
_SMILEY = re.compile(r"emoji|smil", re.I)
_SMILEY_FILE = re.compile(r"/smil(?:ey|ies)/(?:[^/?]+/)*([^/?.]+)\.\w+", re.I)
_PIXELS = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:px)?\s*", re.I)
_CSS_SIZE = re.compile(r"(?:^|;)\s*(?:width|height)\s*:", re.I)
TINY_SIDE = 48   # xgoose's number. Both sides must be under it: real screenshots can be 95x39
FENCE = "```"
QUOTE_CHAR_CAP = 200
_QUOTE_MARKER = secrets.token_hex(24)
_CONTROLS: dict[int, str | None] = dict.fromkeys(n for n in range(160) if n < 32 and n not in (9, 10) or 127 <= n <= 159)
_CONTROLS.update({0x2028: " ", 0x2029: " "})   # line and paragraph separators split lines too


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def sanitize_text(text: str) -> str:
    """Strip C0/C1 controls, keeping newlines and tabs; Unicode line separators become spaces."""
    return _ANSI.sub("", text).translate(_CONTROLS)


def one_line(text: str | None) -> str:
    """Untrusted text for one row of a list: controls stripped, all whitespace (newlines too)
    collapsed to single spaces, so it can't start a row of its own."""
    return " ".join(sanitize_text(text or "").split())


def declares_tiny(attrs: dict[str, str | None]) -> bool:
    """True when an `<img>`'s width and height attributes are both pixel sizes under TINY_SIDE. A
    missing or percent side is unknown, and CSS sizing overrides the attributes, so those are not
    tiny: `width="1" height="1" style="width:100%"` is a full-size picture."""
    sides = [_PIXELS.fullmatch(attrs.get(side) or "") for side in ("width", "height")]
    return (all(sides) and max(float(side[1]) for side in sides) < TINY_SIDE
            and not _CSS_SIZE.search(attrs.get("style") or ""))


class _Extractor(HTMLParser):
    def __init__(self, keep_links: bool, url_cap: int):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.quote = 0
        self.pre = 0
        self.fences: set[int] = set()
        self.lists: list[int | None] = []
        self.cells = 0  # cells started in the current table row
        self.keep_links = keep_links
        self.url_cap = url_cap
        self.capture: _Extractor | None = None
        self.capture_depth = 0
        self.quote_options: dict[str, str | None] = {}
        self.literal: set[int] = set()
        self.links: list[tuple[str, int]] = []  # (href, len(out) at <a>)
        # The last text link whose URL was just printed: (href, its <a> start, index of the URL
        # part). An <a> to the same page right after it continues that link (editors split one
        # mid-word).
        self.printed_link: tuple[str, int, int] | None = None

    def _newline(self) -> None:
        self.out.append("\n" + "> " * self.quote)

    def _after_bullet(self) -> bool:
        return bool(re.fullmatch(r"\s*(?:-|-?\d+\.) ",
                                 next((part for part in reversed(self.out) if part.strip()), "")))

    def _fence(self, fence: str = FENCE) -> None:
        self.fences.add(len(self.out))
        self.out.append(fence)

    def handle_starttag(self, tag, attrs):
        if self.capture is not None:
            self.capture.handle_starttag(tag, attrs)
            if tag == "blockquote":
                self.capture_depth += 1
            return
        options = dict(attrs)
        if tag == "blockquote" and options.get("data-esum-quote") == _QUOTE_MARKER:
            self.capture = _Extractor(options.get("data-links") == "true", self.url_cap)
            self.capture_depth = 1
            self.quote_options = options
            return
        if tag in _SKIP:
            self.skip += 1
        elif tag == "blockquote":
            self.quote += 1
            self._newline()
        elif tag in ("td", "th"):
            if self.cells:
                self.out.append(" | ")
            self.cells += 1
        elif tag in ("ol", "ul"):
            number = None
            if tag == "ol":
                try:
                    number = int(dict(attrs).get("start") or "1")
                except ValueError:
                    number = 1
            self.lists.append(number)
            self._newline()
        elif tag == "li":
            self._newline()
            indent = max(0, len(self.lists) - 1) * 2
            number = self.lists[-1] if self.lists else None
            self.out.append(" " * indent + (f"{number}. " if number is not None else "- "))
            if number is not None:
                self.lists[-1] = number + 1
        elif tag == "p" and self._after_bullet():   # `<li><p>`: the text stays on the bullet's line
            pass
        elif tag in _BLOCK:
            if tag == "pre":
                self.pre += 1
            elif tag in ("tr", "table"):
                self.cells = 0
            self._newline()
            if tag == "pre" and self.pre == 1:
                self._fence()
                self._newline()
        elif tag in _STRUCK:
            self.out.append("~~")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            prior_href, start, url_at = self.printed_link or ("", 0, -1)
            if href and href == prior_href and url_at == len(self.out) - 1:
                # the URL prints once, after the joined label; its part also holds a trailing space
                if self.out.pop().endswith(" "):
                    self.out.append(" ")
                self.links.append((href, start))
            else:
                self.links.append((href, len(self.out)))
            self.printed_link = None
        elif tag == "img":
            found = dict(attrs)
            alt, src = (found.get("alt") or "").strip(), found.get("src") or ""
            src = f"https:{src}" if src.startswith("//") else src
            smiley = _SMILEY_FILE.search(src) if "smilieid" in found or not alt else None
            keep = src.startswith("http") and not _SMILEY.search(found.get("class") or "") and not declares_tiny(found)
            src = src if keep else ""
            if self.skip:
                pass
            elif smiley:
                self.out.append(f":{smiley.group(1)}:")
            elif alt or src:
                self.out.append(f"[image: {' '.join(part for part in (alt, src) if part)}]")

    def handle_startendtag(self, tag, attrs):
        if self.capture is not None:
            self.capture.handle_startendtag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        if tag in _SKIP or tag in ("blockquote", "pre"):  # `<svg/>`: nothing follows to close it
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.capture is not None:
            if tag == "blockquote":
                self.capture_depth -= 1
            if self.capture_depth:
                self.capture.handle_endtag(tag)
            else:
                text = _quote_text(self.capture, self.quote_options)
                self.out.append("\n")
                self.literal.add(len(self.out))
                self.out.append("\n".join("> " * (self.quote + 1) + line for line in text.split("\n")))
                self._newline()
                self.capture = None
            return
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "blockquote":
            self.quote = max(0, self.quote - 1)
            self._newline()
        elif tag in _BLOCK:
            if tag == "pre" and self.pre:
                self.pre -= 1
                if not self.pre:
                    start = max(self.fences)
                    code = "".join(self.out[start + 1:])
                    fence = FENCE
                    if re.search(r"(?m)^(?:> )*[ \t]*`{3,}", code):
                        size = max(len(run[0]) for run in re.finditer(r"`+", code)) + 1
                        fence = "`" * size
                    self.out[start] = fence
                    self._newline()
                    self._fence(fence)
            elif tag in ("ol", "ul") and self.lists:
                self.lists.pop()
            elif tag in ("tr", "table"):
                self.cells = 0
            self._newline()
        elif tag in _STRUCK:
            self.out.append("~~")
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            label = "".join(self.out[start:]).strip()
            if self.keep_links and href.startswith(("http://", "https://")) and href not in label:
                # `<a>gift card </a>to`: the label's trailing space belongs after the URL
                last = self.out[-1] if len(self.out) > start else ""
                trail = " " if not self.pre and last.endswith(" ") and not last.startswith("\n") else ""
                if trail:
                    self.out[-1] = last.rstrip(" ")
                self.out.append(f" ({short_url(href, self.url_cap)}){trail}")
                # only a label ending in a word can be half of a word or phrase split in two
                if trail or label[-1:].isalnum():
                    self.printed_link = (href, start, len(self.out) - 1)

    def handle_data(self, data):
        if self.capture is not None:
            self.capture.handle_data(data)
            return
        if self.skip:
            return
        if self.pre:  # a quoted code block keeps its quote marker on every line
            self.out.append(data.replace("\n", "\n" + "> " * self.quote))
        else:
            self.out.append(data.replace("\r", " ").replace("\n", " "))


def squeeze(text: str) -> str:
    """Collapse prose whitespace, keeping list indentation and code inside matching backtick fences."""
    out: list[str] = []
    fence: str | None = None
    # Keep a final empty code line: an unclosed fence must survive another pass too.
    for line in re.split(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]", text):
        content = line.lstrip("> \t").rstrip()
        opening = re.match(r"`{3,}", content)
        is_fence = content == fence if fence else opening is not None
        if is_fence:
            fence = None if fence else opening[0]
            while fence is None and out and not out[-1].strip("> "):   # a block's trailing newline isn't code
                out.pop()
            out.append(line.rstrip())
        elif fence:
            out.append(line)
        elif (single := " ".join(line.split())) and single.strip("> "):
            if bullet := re.match(r"((?:> )*)( *)(?:- |-?\d+\. )", line):
                prefix, indent = bullet[1], bullet[2]
                single = prefix + indent + single[len(prefix):]
            out.append(single)
    return "\n".join(out)


def short_url(href: str, cap: int) -> str:
    """A link target cut to about `cap` characters. A long address without a query stays whole (up to
    twice the cap) so a long article slug is still followable; a long query, usually tracking, is cut.
    A cut target ends with `…`."""
    if len(href) <= cap:
        return href
    base = href.split("#", 1)[0]
    if len(base) <= cap:
        return base
    page, query, _ = base.partition("?")
    if len(page) <= 2 * cap and (not query or len(page) >= cap):
        return page + ("?…" if query else "")
    return base[: cap - 1] + "…"


def html_to_body(html: str, *, keep_links: bool = True, url_cap: int = 120) -> str:
    """Post body: keep block lines, code whitespace, quotes, strike marks and list numbers."""
    parser = _Extractor(keep_links, url_cap)
    parser.feed(html or "")
    parser.close()
    return _finish(parser)


def html_to_text(html: str, *, keep_links: bool = True, url_cap: int = 120) -> str:
    """FORMAT 1 compatibility entry point for html_to_body."""
    return html_to_body(html, keep_links=keep_links, url_cap=url_cap)


def html_to_label(html: str, *, keep_links: bool, url_cap: int = 120) -> str:
    """Inline label: collapse body whitespace to one safe line; choose link URLs explicitly.

    Fence, quote, strike and list markers and image descriptions remain visible, but code
    indentation, blank lines and list nesting collapse. Image URLs follow the body rules,
    independently of keep_links, which controls anchor destinations only.
    """
    return one_line(html_to_body(html, keep_links=keep_links, url_cap=url_cap))


def _finish(parser: _Extractor) -> str:
    if parser.capture is not None:
        parser.capture_depth = 1
        parser.handle_endtag("blockquote")
    fences: set[int] = set()
    literal: set[int] = set()
    line = 0
    for n, part in enumerate(parser.out):
        if n in parser.fences:
            fences.add(line)
        if n in parser.literal:
            literal.update(range(line, line + part.count("\n") + 1))
        line += part.count("\n")
    lines = "".join(parser.out).splitlines()
    pre = False
    for n, line in enumerate(lines):
        if n in fences:
            pre = not pre
        elif not pre and n not in literal:
            lines[n] = _escape_fences(line)
    return squeeze("\n".join(lines))


def _escape_fences(text: str) -> str:
    """Keep plain text from opening Markdown code fences, including after quote prefixes."""
    return re.sub(r"^((?:> )*[ \t]*)(?=`{3})", r"\1\\", text, flags=re.M)


def _quote_text(parser: _Extractor, options: dict[str, str | None]) -> str:
    text = _finish(parser)
    cap = int(options["data-cap"]) if options.get("data-cap") else None
    cut = cap is not None and len(text) > cap
    if cut:
        text = text[:cap].rstrip()
    fence = None
    for line in text.splitlines():
        content = line.lstrip("> ")
        if fence is None and re.fullmatch(r"`{3,}", content):
            fence = content
        elif content == fence:
            fence = None
    if cut:
        # A suffix on a bare fence would turn it into a new, unclosed opening fence.
        if re.fullmatch(r"(?:> )*`{3,}", text.split("\n")[-1]):
            text += "\n"
        hint = _escape_fences(options.get("data-hint") or "")
        text += "…" + (f" [cut at {cap} chars{hint}]" if hint else "")
    if fence:
        text += "\n" + fence
    head = _escape_fences(options.get("data-head") or "")
    if head:
        text = head + ((":\n" if text.startswith("```") else ": ") + text if text else "")
    return text


def text_of(fragment: str | None) -> str:
    """Strip tags, decode entities and collapse whitespace for a one-line HTML field."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def clip(text: str, cap: int | None, hint: str = "") -> str:
    """Keep the first cap characters, followed by an ellipsis and optional source hint."""
    if cap is None or len(text) <= cap:
        return text
    return text[:cap].rstrip() + (f"… [cut at {cap} chars{hint}]" if hint else "…")


def quote_html(author: str, reference: str | int | None, body_html: str, *,
               cap: int | None = QUOTE_CHAR_CAP, hint: str = "", keep_links: bool = False) -> str:
    """HTML for html_to_text: one capped quote, preserving the body's code and line structure.

    Author is plain text (including any @); reference is an optional displayed post/link label.
    None disables the cap. A hint names where the complete quoted post can be read.
    """
    if cap is not None and cap <= 0:
        raise ValueError("quote cap must be positive or None")
    head = " ".join(str(part) for part in (author, reference) if part is not None and part != "")
    attrs = {"data-esum-quote": _QUOTE_MARKER, "data-head": head, "data-cap": str(cap) if cap is not None else "",
             "data-hint": hint, "data-links": str(keep_links).lower()}
    opening = " ".join(f'{key}="{escape(value, quote=True)}"' for key, value in attrs.items())
    return f"<blockquote {opening}>{body_html}</blockquote>"


def _element_bounds(html: str, start: int, tag: str) -> tuple[int, int]:
    depth = 0
    # Quoted > characters belong to attributes, not to the opening tag's boundary.
    tokens = re.compile(rf"<(/?){re.escape(tag)}(?=[\s/>])(?:=\s*(?:\"[^\"<]*\"|'[^'<]*')|[^<>])*>", re.I)
    for match in tokens.finditer(html, start):
        if match.group(1):
            depth -= 1
        elif not match[0].rstrip().endswith("/>"):
            depth += 1
        if depth == 0:
            return match.start(), match.end()
    return len(html), len(html)


def element_end(html: str, start: int, tag: str = "div") -> int:
    """Index after the balanced element at start; unclosed elements extend to EOF."""
    return _element_bounds(html, start, tag)[1]


def element_inner(html: str, opening: str, tag: str = "div", *, start: int = 0) -> str | None:
    """Inner HTML of the first element matching opening (a regex), or None if absent."""
    match = re.compile(opening, re.I).search(html, start)
    if match is None:
        return None
    return html[match.end():_element_bounds(html, match.start(), tag)[0]]


def remove_elements(html: str, opening: str, tag: str = "div", repl: str = "") -> str:
    """Replace every balanced matching element; never search replacement text again."""
    pattern = re.compile(opening, re.I)
    out, pos = [], 0
    while match := pattern.search(html, pos):
        end = element_end(html, match.start(), tag)
        out.extend((html[pos:match.start()], repl))
        pos = end
    return "".join([*out, html[pos:]])


def inner_div(html: str, start: int) -> str:
    """HTML inside the div at start (FORMAT 1 compatibility)."""
    return element_inner(html, r"<div\b[^>]*>", start=start) or ""
