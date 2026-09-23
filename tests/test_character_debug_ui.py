from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "character_memory" / "web"


def test_character_persona_secondary_sections_are_collapsed():
    script = (WEB / "persona.js").read_text(encoding="utf-8")

    assert 'class="persona-inspector-summary"' in script
    assert '<details class="inspector-details">' in script
    assert "<summary>性格与交流方式</summary>" in script
    assert "创建来源与初始化" in script


def test_runtime_memory_and_task_inspectors_are_collapsed_by_default():
    script = (WEB / "app.js").read_text(encoding="utf-8")

    runtime = script.split("CM.showRuntime = async () => {", 1)[1].split(
        "CM.bootstrap = async () => {", 1
    )[0]
    assert "runtime-summary-card" in runtime
    assert '<summary>Memory Inspector · 最近' in runtime
    assert '<summary>Intent / Task · 最近' in runtime
    assert '<summary>Mental State</summary>' in runtime
    assert '<summary>Persona 原文</summary>' in runtime
    assert '<details class="inspector-details" open>' not in runtime


def test_per_message_trace_surfaces_real_model_io_and_correlation_id():
    script = (WEB / "app.js").read_text(encoding="utf-8")
    trace = script.split("CM.showTrace = async sourceEventId => {", 1)[1].split(
        "CM.memorySourceText =", 1
    )[0]

    assert 'data-trace="${message.source_event_id}"' in script
    assert ">LLM</button>" in script
    assert "模型真实输入 / 原始响应" in trace
    assert "trace.model_messages" in trace
    assert "trace.raw_model_response" in trace
    assert "trace.llm_logical_call_id" in trace
    assert "Compiled Context" in trace
    assert "Recall / Memory" in trace
    assert "Intent / Task" in trace
    assert "不展示隐藏推理过程" in trace


def test_character_debug_scripts_have_valid_javascript():
    node = shutil.which("node")
    if not node:
        return
    for name in ("app.js", "persona.js"):
        checked = subprocess.run(
            [node, "--check", str(WEB / name)],
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stderr
