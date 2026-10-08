from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import escape
import logging
import re
import threading
import time
from typing import Iterable
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import httpx

from character_memory.remote_media import ensure_public_http_url, _sniff_image_mime
from character_memory.rss_content import article_url, parse_article_content
from character_memory.time_utils import epoch_us, parse_datetime


logger = logging.getLogger("character_memory.rss")


DEFAULT_RSS_SOURCES = (
    ("阮一峰的网络日志", "https://www.ruanyifeng.com/blog/atom.xml"),
    ("AIHOT 日报", "https://aihot.news/feed/daily.xml"),
    ("hex2077.dev", "https://hex2077.dev/rss-zh-CN.xml"),
    ("OpenAI News", "https://openai.com/news/rss.xml"),
)

# Fixed UTC+08 avoids requiring an IANA timezone database on Windows.
RSS_TIMEZONE = timezone(timedelta(hours=8))
RSS_CATEGORIES = (
    {"id": "ai", "label": "AI", "keywords": ["AI", "人工智能", "GPT", "大模型", "机器学习", "机器人", "OpenAI"]},
    {"id": "technology", "label": "技术", "keywords": ["技术", "科技", "计算机", "芯片", "开源", "网络", "数据库"]},
    {"id": "development", "label": "开发", "keywords": ["开发", "编程", "代码", "程序", "API", "React", "Python", "Coding", "工程"]},
    {"id": "product", "label": "产品", "keywords": ["产品", "应用", "设计", "工具", "体验", "发布", "上线"]},
)


def rss_today(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(RSS_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def _is_ascii_word_char(char: str) -> bool:
    return char.isascii() and char.isalnum()


def keyword_matches(title: str, keyword: str) -> bool:
    """Whether a title carries a keyword, not a fragment of a longer word.

    Chinese keywords have no word boundary to anchor to, so they keep matching
    as substrings ("技术" is meant to hit "技术文章"). Latin keywords are a
    different matter: plain substring matching made ``ai`` hit "**ai**l" and
    "tr**ai**n", and ``API`` hit "r**api**d", so those must sit on a boundary.
    """
    haystack = str(title or "").lower()
    needle = str(keyword or "").strip().lower()
    if not needle:
        return False
    if not needle.isascii():
        return needle in haystack
    start = haystack.find(needle)
    while start != -1:
        before = haystack[start - 1] if start > 0 else ""
        end = start + len(needle)
        after = haystack[end] if end < len(haystack) else ""
        if not _is_ascii_word_char(before) and not _is_ascii_word_char(after):
            return True
        start = haystack.find(needle, start + 1)
    return False


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for child in list(node):
        if _local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


def _all_children(node: ET.Element, name: str) -> list[ET.Element]:
    target = name.lower()
    return [child for child in list(node) if _local_name(child.tag) == target]


def _xml_markup(node: ET.Element) -> str:
    tag = _local_name(node.tag)
    attrs = "".join(f' {key}="{escape(value, quote=True)}"' for key, value in node.attrib.items() if not key.startswith("{"))
    inner = escape(node.text or "") + "".join(_xml_markup(child) + escape(child.tail or "") for child in node)
    return f"<{tag}{attrs}>{inner}</{tag}>"


def _content_markup(node: ET.Element, names: set[str], base_url: str) -> tuple[str, str]:
    for child in node:
        if _local_name(child.tag) in names and not child.tag.startswith("{http://search.yahoo.com/mrss/}"):
            base = urljoin(base_url, child.attrib.get("{http://www.w3.org/XML/1998/namespace}base", ""))
            if len(child):
                return "".join(_xml_markup(element) for element in child), base
            return child.text or "", base
    return "", base_url


def _parse_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ParsedFeedItem:
    key: str
    title: str
    summary: str
    content_text: str
    url: str
    image_url: str
    published_at: datetime | None
    content_html: str = ""


@dataclass(frozen=True)
class ParsedFeed:
    title: str
    site_url: str
    items: list[ParsedFeedItem]


def parse_feed(xml_text: str, *, feed_url: str = "") -> ParsedFeed:
    # ElementTree may expand entities before the 4 MiB fetch limit can help.
    # DTDs are unnecessary for RSS/Atom; refuse them before parsing untrusted XML.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, flags=re.IGNORECASE):
        raise ValueError("RSS XML 禁止 DOCTYPE/ENTITY 声明")
    root = ET.fromstring(xml_text)
    root_base = urljoin(feed_url, root.attrib.get("{http://www.w3.org/XML/1998/namespace}base", ""))
    root_name = _local_name(root.tag)
    if root_name == "rss":
        channel = next((item for item in list(root) if _local_name(item.tag) == "channel"), root)
        feed_title = _child_text(channel, {"title"})
        site_url = _child_text(channel, {"link"})
        entries = _all_children(channel, "item")
    elif root_name == "feed":
        channel = root
        feed_title = _child_text(channel, {"title"})
        site_url = ""
        for link in _all_children(channel, "link"):
            href = str(link.attrib.get("href") or "").strip()
            rel = str(link.attrib.get("rel") or "alternate").strip().lower()
            if href and rel in {"", "alternate"}:
                site_url = href
                break
        entries = _all_children(channel, "entry")
    else:
        raise ValueError("unsupported RSS/Atom document")

    items: list[ParsedFeedItem] = []
    for entry in entries[:200]:
        entry_base = urljoin(root_base, entry.attrib.get("{http://www.w3.org/XML/1998/namespace}base", ""))
        title = _child_text(entry, {"title"}) or "无标题"
        guid = _child_text(entry, {"guid", "id"})
        link = _child_text(entry, {"link"})
        if not link:
            for link_node in _all_children(entry, "link"):
                href = str(link_node.attrib.get("href") or "").strip()
                rel = str(link_node.attrib.get("rel") or "alternate").strip().lower()
                if href and rel in {"", "alternate"}:
                    link = href
                    break
        link = article_url(link, entry_base) if link else ""
        summary_html, summary_base = _content_markup(entry, {"description", "summary"}, link or entry_base)
        raw_content, content_base = _content_markup(entry, {"encoded", "content"}, link or entry_base)
        summary_text, _, summary_image = parse_article_content(summary_html, base_url=summary_base)
        content_text, content_html, content_image = parse_article_content(
            raw_content or summary_html, base_url=content_base if raw_content else summary_base,
        )
        published = _parse_time(_child_text(entry, {"pubdate", "published", "updated", "date"}))
        image = content_image or summary_image or ""
        if image:
            image = urljoin(link or feed_url, image)
            if urlparse(image).scheme not in {"http", "https"}:
                image = ""
        identity = guid or link or f"{title}|{published.isoformat() if published else ''}|{summary_text[:240]}"
        key = sha256(identity.encode("utf-8", errors="ignore")).hexdigest()
        items.append(
            ParsedFeedItem(
                key=key,
                title=title[:500],
                summary=(summary_text or content_text)[:1200],
                content_text=(content_text or summary_text)[:12000],
                url=link[:2000],
                image_url=image[:2000],
                published_at=published,
                content_html=content_html,
            )
        )
    return ParsedFeed(title=feed_title[:240], site_url=article_url(site_url, feed_url), items=items)


class RssSubscriptionChanged(ValueError):
    pass


class RssRepository:
    MIGRATION = "rss/001-sources-and-items"

    def __init__(self, store, *, seed_defaults: bool = False) -> None:
        self.store = store
        # Category filtering has to happen in SQL to keep paging correct, and
        # the boundary rule below is not expressible in plain SQLite.
        self.store.conn.create_function("rss_keyword_match", 2, keyword_matches, deterministic=True)
        self.store.apply_schema_migration(self.MIGRATION, self._create_schema)
        self.store.apply_schema_migration("rss/002-rich-content", self._add_rich_content)
        self.store.apply_schema_migration("rss/003-subscription-lifecycle", self._add_subscription_lifecycle)
        if seed_defaults:
            self.seed_defaults()

    def _add_rich_content(self) -> None:
        self.store._ensure_column_locked("rss_items", "content_html", "TEXT NOT NULL DEFAULT ''")

    def _add_subscription_lifecycle(self) -> None:
        self.store._ensure_column_locked("rss_sources", "cancelled_at", "TEXT")
        self.store._ensure_column_locked("rss_sources", "subscription_generation", "INTEGER NOT NULL DEFAULT 0")

    def _create_schema(self) -> None:
        self.store.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rss_sources(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                feed_url TEXT NOT NULL UNIQUE,
                site_url TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                fetch_interval_minutes REAL NOT NULL DEFAULT 60,
                last_fetch_at TEXT,
                last_fetch_at_epoch INTEGER,
                last_success_at TEXT,
                last_success_at_epoch INTEGER,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                created_at_epoch INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rss_sources_enabled
            ON rss_sources(enabled,last_fetch_at_epoch,id);

            CREATE TABLE IF NOT EXISTS rss_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                content_text TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                published_at_epoch INTEGER,
                fetched_at TEXT NOT NULL,
                fetched_at_epoch INTEGER NOT NULL,
                UNIQUE(source_id,item_key),
                FOREIGN KEY(source_id) REFERENCES rss_sources(id)
            );
            CREATE INDEX IF NOT EXISTS idx_rss_items_feed
            ON rss_items(COALESCE(published_at_epoch,fetched_at_epoch) DESC,id DESC);
            CREATE INDEX IF NOT EXISTS idx_rss_items_source
            ON rss_items(source_id,COALESCE(published_at_epoch,fetched_at_epoch) DESC,id DESC);
            """
        )

    def seed_defaults(self) -> None:
        now = datetime.now().astimezone()
        with self.store.transaction():
            for name, feed_url in DEFAULT_RSS_SOURCES:
                self.store.conn.execute(
                    "INSERT OR IGNORE INTO rss_sources(name,feed_url,enabled,fetch_interval_minutes,created_at,created_at_epoch) "
                    "VALUES(?,?,?,?,?,?)",
                    (name, feed_url, 1, 60.0, now.isoformat(), epoch_us(now)),
                )

    @staticmethod
    def _source(row) -> dict:
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "feed_url": str(row["feed_url"]),
            "site_url": str(row["site_url"] or ""),
            "enabled": bool(row["enabled"]),
            "cancelled_at": row["cancelled_at"],
            "subscription_generation": int(row["subscription_generation"]),
            "fetch_interval_minutes": float(row["fetch_interval_minutes"]),
            "last_fetch_at": row["last_fetch_at"],
            "last_success_at": row["last_success_at"],
            "last_error": str(row["last_error"] or ""),
        }

    @staticmethod
    def _item(row) -> dict:
        return {
            "id": int(row["id"]),
            "source_id": int(row["source_id"]),
            "source_name": str(row["source_name"]),
            "title": str(row["title"]),
            "summary": str(row["summary"] or ""),
            "content_text": str(row["content_text"] or ""),
            "content_html": str(row["content_html"] or ""),
            "url": str(row["url"] or ""),
            "image_url": str(row["image_url"] or ""),
            "published_at": row["published_at"],
            "fetched_at": row["fetched_at"],
        }

    def list_sources(self, *, include_cancelled: bool = False) -> list[dict]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM rss_items i WHERE i.source_id=s.id) AS item_count "
                "FROM rss_sources s " + ("" if include_cancelled else "WHERE s.cancelled_at IS NULL ") + "ORDER BY s.id"
            ).fetchall()
        result = []
        for row in rows:
            item = self._source(row)
            item["item_count"] = int(row["item_count"] or 0)
            result.append(item)
        return result

    def get_source(self, source_id: int) -> dict | None:
        with self.store._lock:
            row = self.store.conn.execute("SELECT * FROM rss_sources WHERE id=?", (int(source_id),)).fetchone()
        return self._source(row) if row is not None else None

    def create_source(self, feed_url: str, *, name: str = "", interval_minutes: float = 60.0) -> dict:
        normalized = str(feed_url or "").strip()
        if not normalized:
            raise ValueError("RSS URL 不能为空")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("RSS URL 必须是 http(s) 地址")
        fallback_name = str(name or "").strip() or parsed.netloc
        now = datetime.now().astimezone()
        try:
            with self.store.transaction():
                existing = self.store.conn.execute("SELECT id,cancelled_at FROM rss_sources WHERE feed_url=?", (normalized[:2000],)).fetchone()
                if existing is not None:
                    if existing["cancelled_at"] is None:
                        raise ValueError("这个 RSS 已经订阅")
                    return self.restore_source(int(existing["id"]))
                cur = self.store.conn.execute(
                    "INSERT INTO rss_sources(name,feed_url,enabled,fetch_interval_minutes,created_at,created_at_epoch) "
                    "VALUES(?,?,?,?,?,?)",
                    (fallback_name[:240], normalized[:2000], 1, max(10.0, float(interval_minutes)), now.isoformat(), epoch_us(now)),
                )
                source_id = int(cur.lastrowid)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ValueError("这个 RSS 已经订阅") from exc
            raise
        return self.get_source(source_id)

    def set_enabled(self, source_id: int, enabled: bool) -> dict | None:
        with self.store.transaction():
            source = self.get_source(source_id)
            if source is not None and source["cancelled_at"]:
                raise ValueError("该来源已取消订阅，请先恢复订阅")
            self.store.conn.execute("UPDATE rss_sources SET enabled=? WHERE id=?", (int(bool(enabled)), int(source_id)))
        return self.get_source(source_id)

    def cancel_source(self, source_id: int) -> dict | None:
        with self.store.transaction():
            self.store.conn.execute(
                "UPDATE rss_sources SET cancelled_at=?,enabled=0,subscription_generation=subscription_generation+1 "
                "WHERE id=? AND cancelled_at IS NULL",
                (datetime.now().astimezone().isoformat(), int(source_id)),
            )
        return self.get_source(source_id)

    def restore_source(self, source_id: int) -> dict | None:
        with self.store.transaction():
            self.store.conn.execute(
                "UPDATE rss_sources SET cancelled_at=NULL,enabled=1,subscription_generation=subscription_generation+1 "
                "WHERE id=? AND cancelled_at IS NOT NULL", (int(source_id),),
            )
        return self.get_source(source_id)

    def mark_fetch(self, source_id: int, *, now: datetime, error: str = "", title: str = "", site_url: str = "", expected_generation: int | None = None) -> None:
        success = not error
        with self.store.transaction():
            if expected_generation is not None:
                self._check_subscription(source_id, expected_generation)
            self.store.conn.execute(
                "UPDATE rss_sources SET "
                "name=CASE WHEN ?<>'' THEN ? ELSE name END,"
                "site_url=CASE WHEN ?<>'' THEN ? ELSE site_url END,"
                "last_fetch_at=?,last_fetch_at_epoch=?,"
                "last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,"
                "last_success_at_epoch=CASE WHEN ? THEN ? ELSE last_success_at_epoch END,"
                "last_error=? WHERE id=?",
                (
                    title, title, site_url, site_url,
                    now.isoformat(), epoch_us(now),
                    int(success), now.isoformat(), int(success), epoch_us(now),
                    error[:1200], int(source_id),
                ),
            )

    def _check_subscription(self, source_id: int, generation: int) -> None:
        source = self.get_source(source_id)
        if source is None or source["cancelled_at"] or source["subscription_generation"] != generation:
            raise RssSubscriptionChanged("订阅状态已改变，本次抓取结果已丢弃")

    def upsert_items(self, source_id: int, items: list[ParsedFeedItem], *, fetched_at: datetime, expected_generation: int | None = None) -> int:
        inserted = 0
        with self.store.transaction():
            if expected_generation is not None:
                self._check_subscription(source_id, expected_generation)
            for item in items:
                rich_html = parse_article_content(item.content_html, base_url=item.url)[1] if item.content_html else ""
                existing = self.store.conn.execute(
                    "SELECT id FROM rss_items WHERE source_id=? AND item_key=?", (int(source_id), item.key),
                ).fetchone()
                if existing is not None:
                    self.store.conn.execute(
                        # Keep the original publication/sort key stable across content repairs.
                        "UPDATE rss_items SET title=?,summary=?,content_text=?,content_html=?,url=?,image_url=? WHERE id=?",
                        (item.title, item.summary, item.content_text, rich_html, item.url, item.image_url, existing["id"]),
                    )
                    continue
                cur = self.store.conn.execute(
                    "INSERT OR IGNORE INTO rss_items("
                    "source_id,item_key,title,summary,content_text,url,image_url,published_at,published_at_epoch,fetched_at,fetched_at_epoch"
                    ",content_html) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(source_id), item.key, item.title, item.summary, item.content_text,
                        item.url, item.image_url,
                        item.published_at.isoformat() if item.published_at else None,
                        epoch_us(item.published_at) if item.published_at else None,
                        fetched_at.isoformat(), epoch_us(fetched_at), rich_html,
                    ),
                )
                inserted += max(0, int(cur.rowcount or 0))
        return inserted

    def list_items(
        self, *, source_id: int | None = None, limit: int = 60,
        before_id: int | None = None, period: str = "all", q: str = "",
        category: str | None = None, now: datetime | None = None,
    ) -> list[dict]:
        # Routes request one extra item to determine has_more (public max: 100).
        limit = max(1, min(101, int(limit)))
        where = ["1=1"]
        args: list = []
        if period not in {"all", "today"}:
            raise ValueError("period 必须是 all 或 today")
        if period == "today":
            start = rss_today(now)
            where.append("i.published_at_epoch>=? AND i.published_at_epoch<?")
            args.extend([epoch_us(start), epoch_us(start + timedelta(days=1))])
        query = str(q or "").strip()
        if len(query) > 200:
            raise ValueError("标题关键词最多 200 字")
        if query:
            where.append("instr(lower(i.title), lower(?))>0")
            args.append(query)
        if category:
            definition = next((item for item in RSS_CATEGORIES if item["id"] == category), None)
            if definition is None:
                raise ValueError("未知文章类型")
            where.append("(" + " OR ".join("rss_keyword_match(i.title, ?)=1" for _ in definition["keywords"]) + ")")
            args.extend(definition["keywords"])
        if source_id is not None:
            where.append("i.source_id=?")
            args.append(int(source_id))
        if before_id is not None:
            with self.store._lock:
                anchor = self.store.conn.execute(
                    "SELECT COALESCE(published_at_epoch,fetched_at_epoch) AS sort_epoch FROM rss_items WHERE id=?",
                    (int(before_id),),
                ).fetchone()
            if anchor is None:
                raise ValueError("分页游标对应的文章不存在")
            where.append("(COALESCE(i.published_at_epoch,i.fetched_at_epoch),i.id)<(?,?)")
            args.extend([anchor["sort_epoch"], int(before_id)])
        sql = (
            "SELECT i.*,s.name AS source_name FROM rss_items i JOIN rss_sources s ON s.id=i.source_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(i.published_at_epoch,i.fetched_at_epoch) DESC,i.id DESC LIMIT ?"
        )
        args.append(limit)
        with self.store._lock:
            rows = self.store.conn.execute(sql, args).fetchall()
        return [self._item(row) for row in rows]

    def get_item(self, item_id: int) -> dict | None:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT i.*,s.name AS source_name FROM rss_items i JOIN rss_sources s ON s.id=i.source_id WHERE i.id=?",
                (int(item_id),),
            ).fetchone()
        return self._item(row) if row is not None else None

    def due_sources(self, now: datetime) -> list[dict]:
        result = []
        for source in self.list_sources():
            if not source["enabled"]:
                continue
            raw = source.get("last_fetch_at")
            if not raw:
                result.append(source)
                continue
            try:
                last = parse_datetime(raw)
            except Exception:
                result.append(source)
                continue
            if last + timedelta(minutes=float(source["fetch_interval_minutes"])) <= now:
                result.append(source)
        return result


class RssService:
    def __init__(
        self,
        repository: RssRepository,
        *,
        timeout_seconds: float = 15.0,
        total_timeout_seconds: float = 20.0,
        max_response_bytes: int = 4 * 1024 * 1024,
        client: httpx.Client | None = None,
    ) -> None:
        self.repository = repository
        # A per-operation timeout cannot bound a whole fetch: a peer that sends
        # one byte just inside the read window keeps every operation "on time"
        # forever. The deadline below covers one fetch including its redirects.
        self.total_timeout_seconds = float(total_timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False, headers={"User-Agent": "character-memory-rss/1.0"})
        self._owns_client = client is None
        # close() must not interrupt an in-flight fetch or allow it to access a
        # SQLite store that the application is about to close.
        self._lifecycle_lock = threading.RLock()
        self._closing = False
        self._closed = False
        self._active_fetches = 0

    def _close_if_idle_locked(self) -> None:
        if self._closing and not self._active_fetches and not self._closed:
            self._closed = True
            if self._owns_client:
                self.client.close()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._closing = True
            self._close_if_idle_locked()

    def _fetch_text(self, url: str) -> str:
        body, encoding = self._fetch_payload(url)
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    def fetch_image(self, url: str) -> tuple[bytes, str]:
        body, _ = self._fetch_payload(url)
        mime = _sniff_image_mime(body)
        if not mime and len(body) >= 16 and body[4:8] == b"ftyp" and any(brand in body[8:32] for brand in (b"avif", b"avis")):
            mime = "image/avif"
        if not mime:
            raise ValueError("RSS 图片不是支持的 PNG/JPEG/GIF/WebP/AVIF 格式")
        return body, mime

    def _fetch_payload(self, url: str) -> tuple[bytes, str]:
        with self._lifecycle_lock:
            if self._closing:
                raise RuntimeError("RSS 服务已停止")
            self._active_fetches += 1
        try:
            return self._fetch_payload_active(url)
        finally:
            with self._lifecycle_lock:
                self._active_fetches -= 1
                self._close_if_idle_locked()

    def _fetch_payload_active(self, url: str) -> tuple[bytes, str]:
        deadline = time.monotonic() + self.total_timeout_seconds
        current = url
        for _ in range(4):
            ensure_public_http_url(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RSS 抓取超过总时限")
            # Streamed, so an endless or oversized body is cut off at the byte
            # budget instead of being buffered whole before the size check.
            with self.client.stream("GET", current, timeout=remaining) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = response.headers.get("location")
                    if not target:
                        raise RuntimeError("RSS 重定向缺少 Location")
                    current = urljoin(current, target)
                    continue
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_response_bytes:
                        raise ValueError("RSS 响应超过 4 MiB")
                    if time.monotonic() > deadline:
                        raise TimeoutError("RSS 抓取超过总时限")
                encoding = response.charset_encoding or "utf-8"
                return bytes(body), encoding
        raise RuntimeError("RSS 重定向次数过多")

    def refresh_source(self, source_id: int) -> dict:
        stopped = {"ok": False, "source_id": source_id, "inserted": 0, "seen": 0, "error": "RSS 服务已停止"}
        with self._lifecycle_lock:
            if self._closing:
                return stopped
            source = self.repository.get_source(source_id)
        if source is None:
            raise KeyError("RSS source not found")
        if source["cancelled_at"]:
            return {"ok": False, "source_id": source_id, "inserted": 0, "seen": 0, "error": "该来源已取消订阅"}
        generation = source["subscription_generation"]
        now = datetime.now().astimezone()
        try:
            document = parse_feed(self._fetch_text(source["feed_url"]), feed_url=source["feed_url"])
            # Serialize the final DB write with shutdown. A fetch may finish
            # after shutdown, but it must never touch the closed SQLite store.
            with self._lifecycle_lock:
                if self._closing:
                    return stopped
                inserted = self.repository.upsert_items(source_id, document.items, fetched_at=now, expected_generation=generation)
                self.repository.mark_fetch(
                    source_id, now=now, title=document.title or source["name"],
                    site_url=document.site_url, expected_generation=generation,
                )
            return {"ok": True, "source_id": source_id, "inserted": inserted, "seen": len(document.items)}
        except Exception as exc:
            with self._lifecycle_lock:
                if self._closing:
                    return stopped
                try:
                    self.repository.mark_fetch(source_id, now=now, error=str(exc), expected_generation=generation)
                except RssSubscriptionChanged:
                    pass
            logger.warning("rss.refresh failed source=%s url=%s error=%s", source_id, source["feed_url"], exc)
            return {"ok": False, "source_id": source_id, "inserted": 0, "seen": 0, "error": str(exc)}

    def refresh_due(self) -> list[dict]:
        now = datetime.now().astimezone()
        with self._lifecycle_lock:
            if self._closing:
                return []
            due = self.repository.due_sources(now)
        return [self.refresh_source(source["id"]) for source in due]


class RssScheduler:
    def __init__(self, service: RssService, *, poll_seconds: float = 60.0, enabled: bool = True) -> None:
        self.service = service
        self.poll_seconds = max(10.0, float(poll_seconds))
        self.enabled = bool(enabled)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="character-rss-sources", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Do not make application startup pay for four external requests. The
        # first automatic refresh happens after one poll; manual refresh remains immediate.
        while not self._stop.wait(self.poll_seconds):
            try:
                self.service.refresh_due()
            except Exception:
                logger.exception("rss.scheduler loop_error")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.service.close()
