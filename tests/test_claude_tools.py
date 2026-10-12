import json
import urllib.error
import urllib.request

import pytest


class Observer:
    def __init__(self): self.calls = []
    def observe(self, query, **limits):
        self.calls.append((query, limits))
        return {"search_results": 1, "fetch_attempts": 1, "observations": [{
            "title": query, "url": "https://example.org/"+query, "source_domain": "example.org",
            "content": "source text"}], "errors": []}


def call(bridge, task, token=None, origin=None):
    headers={"Authorization": "Bearer " + (token or bridge.token), "Content-Type": "application/json"}
    if origin: headers["Origin"]=origin
    request=urllib.request.Request(bridge.url, data=json.dumps(task).encode(), headers=headers)
    return json.loads(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=3).read())


def test_tool_is_bound_to_one_run_and_limits_total_page_attempts():
    from character_memory.runtime.claude_tools import LocalWorldToolBridge
    observer=Observer()
    with LocalWorldToolBridge(observer, max_calls=2, max_pages=2, max_chars=500) as bridge:
        first=call(bridge, {"query":"one"})
        second=call(bridge, {"query":"two"})
        assert len(first["observations"]) == len(second["observations"]) == 1
        assert first["receipts"][0]["content_scope"] == "WEB_EXCERPT"
        assert bridge.observations[0]["content"] == "source text"
        with pytest.raises(urllib.error.HTTPError) as error: call(bridge, {"query":"three"})
        assert error.value.code == 429
    assert len(observer.calls) == 2


@pytest.mark.parametrize("payload,token,origin,status", [
    ({"query":"one"},"wrong",None,403),
    ({"query":"one"},None,"https://example.org",403),
    ({"query":"one","character_id":"other"},None,None,400),
    ({"query":""},None,None,400),
])
def test_callback_rejects_untrusted_identity_and_credentials(payload, token, origin, status):
    from character_memory.runtime.claude_tools import LocalWorldToolBridge
    observer=Observer()
    with LocalWorldToolBridge(observer) as bridge:
        with pytest.raises(urllib.error.HTTPError) as error: call(bridge,payload,token,origin)
        assert error.value.code == status
    assert not observer.calls


def test_mcp_only_exposes_the_single_read_only_tool():
    from character_memory.runtime.claude_tools import mcp_response
    initialized=mcp_response({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}})
    assert initialized["result"]["capabilities"] == {"tools": {}}
    tools=mcp_response({"jsonrpc":"2.0","id":2,"method":"tools/list"})
    assert [tool["name"] for tool in tools["result"]["tools"]] == ["world_lookup"]
    denied=mcp_response({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"shell","arguments":{}}})
    assert "error" in denied
    assert mcp_response({"jsonrpc":"2.0","method":"notifications/initialized"}) is None
