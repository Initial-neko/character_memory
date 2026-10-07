from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from character_memory.rss_sources import (
    ParsedFeedItem,
    RssRepository,
    RssService,
    keyword_matches,
)
from character_memory.rss_web import attach_rss_routes
from character_memory.storage.sqlite import SQLiteStore


@pytest.fixture
def rss(tmp_path):
    store = SQLiteStore(tmp_path / "queries.db")
    repository = RssRepository(store)
    source = repository.create_source("https://example.com/rss", name="测试源")
    # Deliberately insert out of publication order, including both midnight edges.
    records = [
        ("older", "旧文章 AI", "2026-10-06T15:59:59+00:00"),
        ("newest", "GPT API 开发指南", "2026-10-07T15:59:59+00:00"),
        ("start", "机器人产品 100%_完成", "2026-10-06T16:00:00+00:00"),
        ("tomorrow", "明天技术", "2026-10-07T16:00:00+00:00"),
        ("unknown", "时间未知", None),
        ("middle", "React Native 开发", "2026-10-07T08:00:00+00:00"),
    ]
    fetched = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
    for key, title, published in records:
        repository.upsert_items(source["id"], [ParsedFeedItem(
            key=key, title=title, summary="摘要里有 ONLYSUMMARY",
            content_text="Feed 内容", url=f"https://example.com/{key}",
            image_url="", published_at=datetime.fromisoformat(published) if published else None,
        )], fetched_at=fetched)
    service = RssService(repository)
    app = FastAPI()
    attach_rss_routes(app, Path(__file__).parents[1] / "src/character_memory/web", repository, service)
    with TestClient(app) as client:
        yield repository, client
    service.close()
    store.close()


def test_today_uses_publication_in_beijing_not_fetch_time(rss):
    repo, _ = rss
    items = repo.list_items(period="today", now=datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
    assert [i["title"] for i in items] == ["GPT API 开发指南", "React Native 开发", "机器人产品 100%_完成"]


def test_title_search_is_literal_case_insensitive_and_not_summary(rss):
    repo, _ = rss
    assert [i["title"] for i in repo.list_items(q="gpt api")] == ["GPT API 开发指南"]
    assert [i["title"] for i in repo.list_items(q="%_")] == ["机器人产品 100%_完成"]
    assert repo.list_items(q="ONLYSUMMARY") == []
    assert repo.list_items(q="' OR 1=1 --") == []


def test_category_and_search_are_combined(rss):
    repo, _ = rss
    assert [i["title"] for i in repo.list_items(category="development", q="react")] == ["React Native 开发"]
    assert repo.list_items(category="product", q="React") == []


def test_pagination_uses_sort_key_not_insertion_id(rss):
    repo, _ = rss
    expected = ["明天技术", "GPT API 开发指南", "时间未知", "React Native 开发", "机器人产品 100%_完成", "旧文章 AI"]
    collected = []
    cursor = None
    while True:
        page = repo.list_items(limit=2, before_id=cursor)
        if not page:
            break
        collected.extend(i["title"] for i in page)
        cursor = page[-1]["id"]
    assert collected == expected


def test_http_query_pagination_and_category_discovery(rss):
    _, client = rss
    response = client.get("/v1/rss/items", params={"q": "react", "category": "development"})
    assert response.status_code == 200
    assert [i["title"] for i in response.json()["items"]] == ["React Native 开发"]
    categories = client.get("/v1/rss/categories").json()["categories"]
    assert {c["id"] for c in categories} == {"ai", "technology", "development", "product"}
    assert all(c["keywords"] for c in categories)
    first = client.get("/v1/rss/items?limit=2").json()
    assert first["has_more"] is True
    second = client.get("/v1/rss/items", params={"limit": 2, "before_id": first["next_before_id"]}).json()
    assert [i["title"] for i in second["items"]] == ["时间未知", "React Native 开发"]


@pytest.mark.parametrize("params", [
    {"period": "week"}, {"category": "unknown"}, {"limit": 101},
    {"before_id": 0}, {"before_id": 9999}, {"q": "x" * 201},
])
def test_invalid_queries_are_rejected_instead_of_silently_ignored(rss, params):
    _, client = rss
    assert client.get("/v1/rss/items", params=params).status_code in {400, 422}


def test_today_http_exposes_effective_date_and_timezone(rss):
    _, client = rss
    payload = client.get("/v1/rss/items?period=today").json()
    assert payload["query"]["period"] == "today"
    assert payload["query"]["timezone"] == "UTC+08:00"
    assert len(payload["query"]["date"]) == 10
    assert all(i["published_at"] is not None for i in payload["items"])


def test_latin_category_keywords_need_a_word_boundary():
    # Substring matching made these three hit AI/development by accident.
    assert keyword_matches("Email 推送设计", "AI") is False
    assert keyword_matches("Train 时刻表", "AI") is False
    assert keyword_matches("Rapid growth 报告", "API") is False
    assert keyword_matches("AI 新品发布", "AI") is True
    assert keyword_matches("GPT API 开发指南", "API") is True
    # A hyphen is a boundary, not a word character.
    assert keyword_matches("React-Native 教程", "React") is True
    # Chinese has no word boundary to anchor to, so it stays substring.
    assert keyword_matches("技术文章 04", "技术") is True


def test_category_filter_applies_the_boundary_rule_through_sql(tmp_path):
    store = SQLiteStore(tmp_path / "boundary.db")
    now = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
    try:
        repository = RssRepository(store)
        source = repository.create_source("https://example.com/rss", name="边界源")
        for key, title in (("email", "Email 推送设计"), ("train", "Train 时刻表"), ("ai", "AI 新品发布")):
            repository.upsert_items(source["id"], [ParsedFeedItem(
                key=key, title=title, summary="", content_text="", url="", image_url="", published_at=now,
            )], fetched_at=now)
        assert [i["title"] for i in repository.list_items(category="ai")] == ["AI 新品发布"]
    finally:
        store.close()
