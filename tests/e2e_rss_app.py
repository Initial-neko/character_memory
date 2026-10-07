"""Real RSS routes/storage with deterministic upstream feeds for browser acceptance."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
import httpx

from character_memory.rss_sources import ParsedFeedItem, RssRepository, RssService
from character_memory.rss_web import attach_rss_routes
from character_memory.storage.sqlite import SQLiteStore

app = FastAPI()
web = Path(__file__).parents[1] / "src/character_memory/web"
store = SQLiteStore(os.environ["RSS_E2E_DB"])
repository = RssRepository(store)
source = repository.create_source("https://93.184.216.34/feed.xml", name="桌面验收样例")
now = datetime.now(timezone.utc)
today = now.astimezone(timezone(timedelta(hours=8))).replace(hour=10, minute=0, second=0, microsecond=0)
titles = ["GPT API 开发指南", "机器人产品体验", "React Native 开发", "100%_标题字面匹配"]
for index in range(38):
    title = titles[index] if index < len(titles) else f"技术文章 {index:02d}"
    repository.upsert_items(source["id"], [ParsedFeedItem(
        key=str(index), title=title, summary="这是 Feed 提供的摘要，用于检查卡片阅读与详情返回。",
        content_text="这是 Feed 提供的内容。原文链接保留，未将摘要宣称为完整正文。",
        url=f"https://example.com/article/{index}", image_url="/test/cover.svg" if index % 2 == 0 else "",
        published_at=today - timedelta(minutes=index),
    )], fetched_at=now)
repository.upsert_items(source["id"], [ParsedFeedItem(
    key="yesterday", title="昨天的 AI 新闻", summary="今天抓到的旧文章", content_text="旧文",
    url="https://example.com/old", image_url="", published_at=today - timedelta(days=1),
), ParsedFeedItem(
    key="unknown", title="发布时间未知", summary="无发布时间", content_text="未知",
    url="https://example.com/unknown", image_url="", published_at=None,
)], fetched_at=now)
repository.mark_fetch(source["id"], now=now)


def upstream(request):
    if request.url.path == "/fail.xml":
        return httpx.Response(500)
    return httpx.Response(200, text='''<rss version="2.0"><channel><title>新增验收源</title>
      <item><guid>one</guid><title>新增产品文章</title><link>https://example.com/new</link>
      <description>订阅刷新测试</description></item></channel></rss>''')


service = RssService(repository, client=httpx.Client(transport=httpx.MockTransport(upstream)))
attach_rss_routes(app, web, repository, service)
app.mount("/static", StaticFiles(directory=web), name="static")


@app.get("/test/cover.svg")
def cover():
    return Response('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="360"><rect width="600" height="360" fill="#edeaff"/><circle cx="450" cy="130" r="90" fill="#d1c9ff"/><text x="40" y="230" font-size="42" fill="#5548e8">RSS · DAILY</text></svg>', media_type="image/svg+xml")
