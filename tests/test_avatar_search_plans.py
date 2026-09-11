from character_memory.avatars import AvatarSearchService, AvatarStore
from character_memory.search import ImageSearchResult, SearchProvider


class PlannedSearchProvider(SearchProvider):
    def __init__(self, counts):
        self.counts = counts
        self.calls = []

    def search_images(self, query: str, *, limit: int = 12) -> list[ImageSearchResult]:
        self.calls.append((query, limit))
        count = min(self.counts.get(query, 0), limit)
        return [
            ImageSearchResult(
                title=f"{query}-{index}",
                image_url=f"https://cdn.example.org/{query}-{index}.jpg",
                thumbnail_url=f"https://cdn.example.org/{query}-{index}-thumb.jpg",
                source_page_url=f"https://example.org/{query}/{index}",
                source_domain="example.org",
                width=512,
                height=512,
            )
            for index in range(count)
        ]


def test_multi_query_search_stops_after_primary_query_fills_candidate_target(tmp_path):
    provider = PlannedSearchProvider({"primary": 12, "secondary": 12})
    service = AvatarSearchService(provider, AvatarStore(tmp_path / "avatars"))

    result = service.search_queries("mika", ["primary", "secondary"], limit=12)

    assert len(result["candidates"]) == 12
    assert result["queries"] == ["primary", "secondary"]
    assert result["used_queries"] == ["primary"]
    assert provider.calls == [("primary", 12)]
    service.close()


def test_multi_query_search_uses_secondary_only_when_primary_is_insufficient(tmp_path):
    provider = PlannedSearchProvider({"primary": 2, "secondary": 10, "third": 10})
    service = AvatarSearchService(provider, AvatarStore(tmp_path / "avatars"))

    result = service.search_queries("mika", ["primary", "secondary", "third"], limit=6)

    assert len(result["candidates"]) == 6
    assert result["used_queries"] == ["primary", "secondary"]
    assert provider.calls == [("primary", 6), ("secondary", 4)]
    service.close()
