from dataclasses import replace
import json
import sys

from character_memory.runtime.capability_execution import CapabilityRequest


def request(query="public photography", identifier="run:1"):
    return CapabilityRequest(identifier, "alice", identifier, None, "WORLD", "WEB_SEARCH",
                             {"query": query}, {"max_pages": 2, "max_chars_per_page": 6000, "max_calls": 1})


class Observer:
    def observe(self, query, **limits):
        return {"search_results": 1, "fetch_attempts": 1, "observations": [{
            "title": "Source", "url": "https://example.org/source", "source_domain": "example.org",
            "content": "Verified public content", "snippet": ""}], "errors": []}


def fake_cli(tmp_path, mode="success"):
    script = tmp_path / "fake_cli.py"
    script.write_text('''import json, os, sys, urllib.request, time
args=sys.argv[1:]
task=sys.stdin.read()
mode=''' + repr(mode) + '''
if mode == "hang": time.sleep(20)
if mode == "flood":
    print("x"*70000, flush=True); time.sleep(20)
if mode == "crash": sys.exit(7)
if mode == "success":
    req=urllib.request.Request(os.environ["CM_CLAUDE_CALLBACK"], data=json.dumps({"query":task}).encode(),
        headers={"Authorization":"Bearer "+os.environ["CM_CLAUDE_TOKEN"],"Content-Type":"application/json"})
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5).read()
print(json.dumps({"type":"system","subtype":"init","session_id":args[args.index("--session-id")+1],"model":"fake"}))
print(json.dumps({"type":"assistant","message":{"content":[{"type":"thinking","thinking":"HIDDEN"},{"type":"text","text":"Visible answer"}]}}))
print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"Visible answer",
 "total_cost_usd":0.02,"modelUsage":{"fake":{"inputTokens":20,"outputTokens":10,"cacheReadInputTokens":0,"cacheCreationInputTokens":0}}}))
''', encoding="utf-8")
    return [sys.executable, str(script)]


def test_independent_runs_only_send_current_task_and_verified_sources(tmp_path):
    from character_memory.runtime.claude_process import ClaudePrintRunner
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=fake_cli(tmp_path))
    first = runner.run(request())
    second = runner.run(request("new task", "run:2"))
    assert first.status == second.status == "SUCCESS"
    assert first.data["execution"]["session_id"] != second.data["execution"]["session_id"]
    assert first.data["observations"][0]["content"] == "Verified public content"
    assert first.data["execution"]["estimated_cost_usd"] == .02
    assert first.data["execution"]["input_tokens"] == 20
    assert "HIDDEN" not in json.dumps(first.data)
    detail = json.loads((tmp_path / "runs" / second.data["execution"]["detail_path"]).read_text())
    assert detail["task"] == "new task"
    assert "public photography" not in json.dumps(detail)
    assert detail["assistant_response"] == "Visible answer"
    assert "--no-session-persistence" in detail["argv_flags"]
    assert not set(detail["argv_flags"]) & {"--resume", "--continue", "--fork-session"}
    runner.close()


def test_success_without_tool_evidence_cannot_become_reading(tmp_path):
    from character_memory.runtime.claude_process import ClaudePrintRunner
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=fake_cli(tmp_path, "no_tool"))
    result = runner.run(request())
    assert result.status == "FAILED" and result.reason == "NO_VERIFIED_CONTENT"
    assert result.data["observations"] == []
    runner.close()


def test_timeout_and_missing_usage_are_unknown_not_zero(tmp_path):
    from character_memory.runtime.claude_process import ClaudePrintRunner
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=fake_cli(tmp_path, "hang"), timeout_seconds=.3)
    result = runner.run(request())
    assert result.status == "TIMEOUT"
    assert result.data["execution"]["usage_state"] == "UNKNOWN"
    assert result.data["execution"]["estimated_cost_usd"] is None
    assert result.data["execution"]["process_stopped"] is True
    runner.close()


def test_output_limit_and_shutdown_are_bounded(tmp_path):
    from character_memory.runtime.claude_process import ClaudePrintRunner
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=fake_cli(tmp_path, "flood"))
    result = runner.run(request())
    assert result.status == "FAILED" and result.reason == "OUTPUT_LIMIT"
    runner.close()
    assert runner.run(request("later", "run:later")).reason == "EXECUTOR_CLOSED"


def test_non_world_or_unknown_capability_never_starts(tmp_path):
    from character_memory.runtime.claude_process import ClaudePrintRunner
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=["does-not-exist"])
    assert runner.run(replace(request(), channel="DIRECT")).status == "DENIED"
    assert runner.run(replace(request(), capability="READ_RSS")).status == "DENIED"
    runner.close()
