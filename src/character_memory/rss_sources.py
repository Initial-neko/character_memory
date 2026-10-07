from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import logging
import threading
from typing import Iterable
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import httpx

from character_memory.remote_media import ensure_public_http_url
from character_memory.time_utils import epoch_us, parse_datetime


logger = logging.getLogger("character_memory.rss")


DEFAULT_RSS_SOURCES = (
    ("阮一峰的网络日志", "https://www.ruanyifeng.com/blog/atom.xml"),
    ("AIHOT 日报", "https://aihot.news/feed/daily.xml"),
    ("hex2077.dev", "https://hex2077.dev/rss-zh-CN.xml"),
    ("OpenAI News", "https://openai.com/news/rss.xml"),
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.first_image: str | None = None

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "img" or self.first_image:
            return
        for key, value in attrs:
            if key.lower() == "src" and value:
                self.first_image = str(value).strip()
                break


def _plain_text(value: str, *, limit: int = 12000) -> tuple[str, str | None]:
    parser = _TextExtractor()
    try:
        parser.feed(value or "")
        parser.close()
    except Exception:
        pass
    text = unescape(" ".join(parser.parts)).strip()
    return text[:limit], parser.first_image


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


@dataclass(frozen=True)
class ParsedFeed:
    title: str
    site_url: str
    items: list[ParsedFeedItem]


def parse_feed(xml_text: str, *, feed_url: str = "") -> ParsedFeed:
    root = ET.fromstring(xml_text)
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
        link = urljoin(feed_url, link) if link else ""
        summary_html = _child_text(entry, {"description", "summary"})
        content_html = _child_text(entry, {"encoded", "content"}) or summary_html
        summary_text, summary_image = _plain_text(summary_html, limit=1200)
        content_text, content_image = _plain_text(content_html, limit=12000)
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
            )
        )
    return ParsedFeed(title=feed_title[:240], site_url=urljoin(feed_url, site_url)[:2000], items=items)


class RssRepository:
    MIGRATION = "rss/001-sources-and-items"

    def __init__(self, store, *, seed_defaults: bool = True) -> None:
        self.store = store
        self.store.apply_schema_migration(self.MIGRATION, self._create_schema)
        if seed_defaults:
            self.seed_defaults()

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
            "url": str(row["url"] or ""),
            "image_url": str(row["image_url"] or ""),
            "published_at": row["published_at"],
            "fetched_at": row["fetched_at"],
        }

    def list_sources(self) -> list[dict]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM rss_items i WHERE i.source_id=s.id) AS item_count "
                "FROM rss_sources s ORDER BY s.id"
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
            self.store.conn.execute("UPDATE rss_sources SET enabled=? WHERE id=?", (int(bool(enabled)), int(source_id)))
        return self.get_source(source_id)

    def mark_fetch(self, source_id: int, *, now: datetime, error: str = "", title: str = "", site_url: str = "") -> None:
        success = not error
        with self.store.transaction():
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

    def upsert_items(self, source_id: int, items: list[ParsedFeedItem], *, fetched_at: datetime) -> int:
        inserted = 0
        with self.store.transaction():
            for item in items:
                cur = self.store.conn.execute(
                    "INSERT OR IGNORE INTO rss_items("
                    "source_id,item_key,title,summary,content_text,url,image_url,published_at,published_at_epoch,fetched_at,fetched_at_epoch"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(source_id), item.key, item.title, item.summary, item.content_text,
                        item.url, item.image_url,
                        item.published_at.isoformat() if item.published_at else None,
                        epoch_us(item.published_at) if item.published_at else None,
                        fetched_at.isoformat(), epoch_us(fetched_at),
                    ),
                )
                inserted += max(0, int(cur.rowcount or 0))
        return inserted

    def list_items(self, *, source_id: int | None = None, limit: int = 60, before_id: int | None = None) -> list[dict]:
        limit = max(1, min(100, int(limit)))
        where = ["1=1"]
        args: list = []
        if source_id is not None:
            where.append("i.source_id=?")
            args.append(int(source_id))
        if before_id is not None:
            where.append("i.id<?")
            args.append(int(before_id))
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
    def __init__(self, repository: RssRepository, *, timeout_seconds: float = 15.0, client: httpx.Client | None = None) -> None:
        self.repository = repository
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False, headers={"User-Agent": "character-memory-rss/1.0"})
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _fetch_text(self, url: str) -> str:
        current = url
        for _ in range(4):
            ensure_public_http_url(current)
            response = self.client.get(current)
            if response.status_code in {301, 302, 303, 307, 308}:
                target = response.headers.get("location")
                if not target:
                    raise RuntimeError("RSS 重定向缺少 Location")
                current = urljoin(current, target)
                continue
            response.raise_for_status()
            if len(response.content) > 4 * 1024 * 1024:
                raise ValueError("RSS 响应超过 4 MiB")
            return response.text
        raise RuntimeError("RSS 重定向次数过多")

    def refresh_source(self, source_id: int) -> dict:
        source = self.repository.get_source(source_id)
        if source is None:
            raise KeyError("RSS source not found")
        now = datetime.now().astimezone()
        try:
            document = parse_feed(self._fetch_text(source["feed_url"]), feed_url=source["feed_url"])
            inserted = self.repository.upsert_items(source_id, document.items, fetched_at=now)
            self.repository.mark_fetch(
                source_id,
                now=now,
                title=document.title or source["name"],
                site_url=document.site_url,
            )
            return {"ok": True, "source_id": source_id, "inserted": inserted, "seen": len(document.items)}
        except Exception as exc:
            self.repository.mark_fetch(source_id, now=now, error=str(exc))
            logger.warning("rss.refresh failed source=%s url=%s error=%s", source_id, source["feed_url"], exc)
            return {"ok": False, "source_id": source_id, "inserted": 0, "seen": 0, "error": str(exc)}

    def refresh_due(self) -> list[dict]:
        now = datetime.now().astimezone()
        return [self.refresh_source(source["id"]) for source in self.repository.due_sources(now)]


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
