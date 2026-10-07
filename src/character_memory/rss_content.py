"""Preserve readable Feed markup through a small, explicit HTML allowlist."""
from html import escape, unescape
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlparse


ALLOWED_TAGS = frozenset({
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr",
    "ul", "ol", "li", "blockquote", "pre", "code", "strong", "b", "em", "i",
    "a", "img", "figure", "figcaption", "table", "thead", "tbody", "tr", "th", "td",
})
VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "source", "embed", "wbr"})
DROP_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript", "head"})
BLOCK_TAGS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figure", "figcaption", "table"})


def article_url(value: str, base_url: str) -> str:
    value = unescape(str(value or "")).strip()
    if not value:
        return ""
    resolved = urljoin(base_url, value)
    parsed = urlparse(resolved)
    return resolved[:2000] if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username else ""


class _SafeArticle(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.dropped: list[str] = []
        self.first_image = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.dropped:
            if tag not in VOID_TAGS:
                self.dropped.append(tag)
            return
        if tag in DROP_TAGS:
            if tag not in VOID_TAGS:
                self.dropped.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return
        attributes = dict(attrs)
        safe_attrs = ""
        if tag == "img":
            # Lazy feeds often use data-src alongside a placeholder src.
            src = article_url(attributes.get("data-src") or attributes.get("src"), self.base_url)
            if not src:
                return
            alt = str(attributes.get("alt") or "")[:500]
            safe_attrs = f' src="{escape(src, quote=True)}" alt="{escape(alt, quote=True)}" loading="lazy" referrerpolicy="no-referrer"'
            if not self.first_image:
                self.first_image = src
        elif tag == "a":
            href = article_url(attributes.get("href"), self.base_url)
            if href:
                safe_attrs = f' href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
        self.parts.append(f"<{tag}{safe_attrs}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.dropped:
            if tag in self.dropped:
                del self.dropped[self.dropped.index(tag):]
            return
        if tag in self.stack:
            while self.stack:
                opened = self.stack.pop()
                self.parts.append(f"</{opened}>")
                if opened == tag:
                    break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.dropped:
            self.parts.append(escape(data))

    def finish(self):
        self.close()
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts)


class _ReadableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.pre = False

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self.parts.append("\n\n")
        elif tag in {"li", "br", "tr"}:
            self.parts.append("\n")
        if tag == "pre":
            self.pre = True

    def handle_endtag(self, tag):
        if tag in BLOCK_TAGS:
            self.parts.append("\n\n")
        if tag == "pre":
            self.pre = False

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))


def article_image_urls(value: str) -> set[str]:
    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "img":
                src = dict(attrs).get("src")
                if src:
                    urls.add(src)
    urls: set[str] = set()
    parser = Images()
    parser.feed(value or "")
    return urls


def parse_article_content(value: str, *, base_url: str = "") -> tuple[str, str, str]:
    raw = str(value or "")[:128_000]
    # Plain-text feeds may already contain meaningful line breaks.
    if "<" not in raw:
        raw = "<p>" + escape(raw).replace("\n", "<br>") + "</p>" if raw else ""
    safe = _SafeArticle(base_url)
    safe.feed(raw)
    html = safe.finish()
    reader = _ReadableText()
    reader.feed(html)
    reader.close()
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", "".join(reader.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:12000], html, safe.first_image
