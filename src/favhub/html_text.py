"""Deterministic HTML → plain-text rendering for saved rich content.

A small, dependency-free converter for the clean tag subset Zhihu emits
(``p a b code pre ol ul li div span img figure br`` plus headings and
blockquotes defensively). Images never enter the text body — their URLs are
collected separately so callers can render a media section or attach OCR
later. Zhihu's ``link.zhihu.com/?target=…`` redirect wrappers are unwrapped
to the real destination.
"""

from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

__all__ = ["render_text"]

_BLOCK_TAGS = frozenset(
    {"p", "div", "figure", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "ul"}
)


def _unwrap_link(href: str) -> str:
    parsed = urlparse(href)
    if parsed.netloc == "link.zhihu.com":
        target = parse_qs(parsed.query).get("target")
        if target:
            return unquote(target[0])
    return href


class _Renderer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.images: list[str] = []
        self._current: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._in_pre = False
        self._list_stack: list[tuple[str, int]] = []

    # -- helpers ---------------------------------------------------------
    def _flush(self) -> None:
        text = "".join(self._current)
        if not self._in_pre:
            lines = [" ".join(line.split()) for line in text.split("\n")]
            text = "\n".join(line for line in lines if line)
        if text.strip():
            self.blocks.append(text.rstrip())
        self._current = []

    def _append(self, text: str) -> None:
        if self._href is not None:
            self._link_text.append(text)
        else:
            self._current.append(text)

    # -- parser hooks ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "img":
            source = attributes.get("data-actualsrc") or attributes.get("src") or ""
            if source.startswith("http") and source not in self.images:
                self.images.append(source)
            return
        if tag == "br":
            self._current.append("\n")
            return
        if tag == "pre":
            self._flush()
            self._in_pre = True
            return
        if tag == "a":
            self._href = _unwrap_link(attributes.get("href") or "")
            self._link_text = []
            return
        if tag in ("ol", "ul"):
            self._flush()
            self._list_stack.append((tag, 0))
            return
        if tag == "li":
            self._flush()
            if self._list_stack:
                kind, count = self._list_stack[-1]
                count += 1
                self._list_stack[-1] = (kind, count)
                self._current.append(f"{count}. " if kind == "ol" else "- ")
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            text = "".join(self._current).strip("\n")
            self._current = []
            self._in_pre = False
            if text.strip():
                self.blocks.append(f"```\n{text}\n```")
            return
        if tag == "a":
            text = " ".join("".join(self._link_text).split())
            href = self._href or ""
            self._href = None
            if text and href.startswith("http") and text != href:
                self._current.append(f"{text} ({href})")
            elif text:
                self._current.append(text)
            elif href.startswith("http"):
                self._current.append(href)
            return
        if tag in ("ol", "ul"):
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
            return
        if tag == "li" or tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        self._append(data)


def render_text(html: str) -> tuple[str, tuple[str, ...]]:
    """Render HTML into readable plain text plus the collected image URLs."""
    renderer = _Renderer()
    renderer.feed(html)
    renderer.close()
    renderer._flush()

    blocks: list[str] = []
    list_run: list[str] = []
    for block in renderer.blocks:
        if block.startswith(("- ", "1. ")) or (
            list_run and block[:1].isdigit() and ". " in block[:5]
        ):
            list_run.append(block)
            continue
        if list_run:
            blocks.append("\n".join(list_run))
            list_run = []
        blocks.append(block)
    if list_run:
        blocks.append("\n".join(list_run))
    return "\n\n".join(blocks).strip(), tuple(renderer.images)
