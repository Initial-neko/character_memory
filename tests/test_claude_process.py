from dataclasses import replace
import json
import pytest
import sys
from pathlib import Path

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
with open(''' + repr(str(tmp_path / "received.jsonl")) + ''',"a",encoding="utf-8") as receipt:
    receipt.write(json.dumps({"stdin":task,"argv":args,"config_dir":os.environ.get("CLAUDE_CONFIG_DIR"),"cwd":os.getcwd()})+"\\n")
mode=''' + repr(mode) + '''
if mode == "orphan":
    import subprocess
    subprocess.Popen([sys.executable,"-c","import time;time.sleep(15)"],stdout=sys.stdout,stderr=sys.stderr)
    sys.exit(0)
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
    second = runner.run(replace(request("new task", "run:2"), character_id="bob"))
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
    assert detail["tool_steps"] == second.data["tool_steps"]
    assert detail["tool_steps"][0]["request_id"] == "run:2"
    assert detail["tool_steps"][0]["status"] == "SUCCESS"
    assert "--no-session-persistence" in detail["argv_flags"]
    assert not set(detail["argv_flags"]) & {"--resume", "--continue", "--fork-session"}
    received = [json.loads(line) for line in (tmp_path / "received.jsonl").read_text().splitlines()]
    assert [row["stdin"] for row in received] == ["public photography", "new task"]
    assert received[0]["cwd"] != received[1]["cwd"]
    assert received[0]["config_dir"] != received[1]["config_dir"]
    assert all("--bare" in row["argv"] and "--safe-mode" not in row["argv"] for row in received)
    runner.close()


def test_direct_task_can_return_analysis_without_claiming_external_observation(tmp_path):
    from character_memory.runtime.claude_process import ClaudePrintRunner
    runner=ClaudePrintRunner(Observer(),tmp_path/'runs',command=fake_cli(tmp_path,'no_tool'))
    task=replace(request(),channel='DIRECT',capability='CLAUDE_TASK',constraints={**request().constraints,
        'backend':'claude','role_grant':'claude_task','claude_skills':[], 'claude_daily_max':3,
        'claude_daily_budget_usd':None,'claude_max_budget_usd':None,'cost_saving':False})
    result=runner.run(task)
    assert result.status=='SUCCESS'
    assert result.data['assistant_response']=='Visible answer'
    assert result.data['knowledge_scope']=='MODEL_RESPONSE' and not result.data['observations']
    assert result.data['execution']['tool_calls']==0
    runner.close()


def test_exited_parent_cannot_leave_child_holding_pipes_past_timeout(tmp_path):
    import time
    from character_memory.runtime.claude_process import ClaudePrintRunner
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=fake_cli(tmp_path, "orphan"), timeout_seconds=.5)
    started = time.monotonic()
    result = runner.run(request())
    assert time.monotonic() - started < 3
    assert result.status == "TIMEOUT" and result.data["execution"]["process_stopped"]
    runner.close()


def test_user_auth_config_only_imports_routing_fields(tmp_path, monkeypatch):
    from character_memory.runtime.claude_process import configured_auth_environment
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN":"test-secret", "ANTHROPIC_MODEL":"test-model", "BASH_ENV":"unsafe"}, "hooks":{"x":"unsafe"}}))
    assert configured_auth_environment() == {"ANTHROPIC_AUTH_TOKEN":"test-secret", "ANTHROPIC_MODEL":"test-model"}


def test_close_before_resolution_finishes_never_launches_cli(tmp_path, monkeypatch):
    import threading
    import character_memory.runtime.claude_process as module
    entered, release = threading.Event(), threading.Event()
    command = fake_cli(tmp_path)
    def resolve():
        entered.set()
        assert release.wait(3)
        return command
    monkeypatch.setattr(module, "resolve_claude", resolve)
    monkeypatch.setattr(module, "configured_auth_environment", lambda: {"ANTHROPIC_AUTH_TOKEN":"test-only"})
    runner = module.ClaudePrintRunner(Observer(), tmp_path / "runs")
    results=[]
    thread=threading.Thread(target=lambda: results.append(runner.run(request())))
    thread.start()
    assert entered.wait(3)
    runner.close()
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert results[0].reason == "EXECUTOR_CLOSED"
    assert not (tmp_path / "received.jsonl").exists()


def test_detail_retention_only_removes_expired_owned_files(tmp_path):
    import os
    import time
    from character_memory.runtime.claude_process import ClaudePrintRunner
    directory=tmp_path/"runs"
    directory.mkdir()
    old=directory/"11111111-1111-1111-1111-111111111111.json"
    unrelated=directory/"settings.json"
    for path in (old,unrelated):
        path.write_text("{}")
        os.utime(path,(time.time()-31*86400,)*2)
    runner=ClaudePrintRunner(Observer(),directory,command=fake_cli(tmp_path))
    assert runner.run(request()).status=="SUCCESS"
    assert not old.exists() and unrelated.exists()
    runner.close()


def test_failed_cli_zero_placeholder_does_not_claim_known_free_usage():
    from character_memory.runtime.claude_process import _usage
    usage=_usage({"is_error":True,"subtype":"error_during_execution","total_cost_usd":0,"modelUsage":{}})
    assert usage["usage_state"]=="UNKNOWN" and usage["estimated_cost_usd"] is None


def test_success_without_tool_evidence_cannot_become_reading(tmp_path):
    from character_memory.runtime.claude_process import ClaudePrintRunner
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=fake_cli(tmp_path, "no_tool"))
    result = runner.run(request())
    assert result.status == "FAILED" and result.reason == "NO_VERIFIED_CONTENT"
    assert result.data["observations"] == []
    assert result.data["tool_steps"] == []
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


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_only_selected_skills_mount_into_temporary_claude_directory(tmp_path, monkeypatch, newline):
    import hashlib
    from character_memory.runtime.claude_process import ClaudePrintRunner
    content = "---\nname: anime-research\ndescription: Verified seasonal anime research\n---\nCheck sourced episodes."
    content = content.replace("\n", newline)
    from character_memory.runtime import claude_process
    original_popen = claude_process.subprocess.Popen
    mounted = []
    def inspect_mount(argv, *args, **kwargs):
        root = Path(argv[argv.index("--add-dir") + 1])
        mounted.append((root / ".claude" / "skills" / "anime-research" / "SKILL.md").read_bytes())
        return original_popen(argv, *args, **kwargs)
    monkeypatch.setattr(claude_process.subprocess, "Popen", inspect_mount)
    item = {"name": "anime-research", "text": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}
    base = request("research season", "skills:1")
    req = replace(base, constraints={**base.constraints, "backend": "claude", "role_grant": "claude_research",
        "claude_skills": [item], "claude_daily_max": 1, "claude_daily_budget_usd": None,
        "claude_max_budget_usd": None, "cost_saving": False})
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=fake_cli(tmp_path))
    result = runner.run(req)
    assert result.status == "SUCCESS"
    assert mounted == [content.encode("utf-8")]
    received = json.loads((tmp_path / "received.jsonl").read_text().splitlines()[0])
    argv = received["argv"]
    assert argv[argv.index("--tools") + 1] == "Skill"
    assert "--add-dir" in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__character_world__world_lookup Skill"
    assert result.data["execution"]["selected_skills"] == [{"name":"anime-research","sha256":item["sha256"]}]
    assert not (tmp_path / "runs" / "task-").exists()
    runner.close()


def test_step_queries_and_results_use_existing_credential_redaction(tmp_path, monkeypatch):
    from character_memory.runtime.claude_process import ClaudePrintRunner
    monkeypatch.setenv("ANTHROPIC_API_KEY", 'unit-test"secret')
    runner = ClaudePrintRunner(Observer(), tmp_path / "runs", command=fake_cli(tmp_path))
    result = runner.run(request('public unit-test"secret topic'))
    assert 'unit-test"secret' not in str(result.data["tool_steps"])
    detail = json.loads((tmp_path / "runs" / result.data["execution"]["detail_path"]).read_text())
    assert 'unit-test"secret' not in detail["task"]
    assert "[REDACTED]" in json.dumps(result.data["tool_steps"])
    runner.close()


def test_availability_checks_installed_command_without_spawning_or_creating_output(tmp_path, monkeypatch):
    from character_memory.runtime import claude_process
    output = tmp_path / 'not-created'
    runner = claude_process.ClaudePrintRunner(Observer(), output, command=[sys.executable])
    monkeypatch.setattr(claude_process.subprocess, 'Popen', lambda *a, **k: pytest.fail('availability spawned CLI'))
    assert runner.is_available() is True
    assert not output.exists()
    runner.close()
    assert runner.is_available() is False


def test_availability_handles_missing_claude_installation(tmp_path, monkeypatch):
    from character_memory.runtime import claude_process
    def missing():
        raise FileNotFoundError('CLI_NOT_FOUND')
    monkeypatch.setattr(claude_process, 'resolve_claude', missing)
    runner = claude_process.ClaudePrintRunner(Observer(), tmp_path/'not-created')
    assert runner.is_available() is False
