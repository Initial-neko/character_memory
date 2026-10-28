from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from character_memory.app import build_app, build_app_from_settings, build_embedding, build_model
from character_memory.config import load_settings
from character_memory.eval.offline import (
    BudgetedModel,
    TeeUsageRecorder,
    build_offline_eval_bundle,
    persona_versions_from_runtimes,
)
from character_memory.eval.report import (
    apply_reviews,
    build_metadata,
    build_report,
    compare_reports,
    load_report,
    load_reviews,
    render_comparison_text,
    render_report_text,
    write_json,
)
from character_memory.eval.runner import EvalCostMeter, EvalRunner
from character_memory.storage.sqlite import SQLiteStore


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _inspect(config_path: str, character: str):
    settings = load_settings(config_path)
    store = SQLiteStore(settings.db_path)
    try:
        print("WORLD TIME:\n", store.get_world_time(character) or "not initialized")
        print("\nMENTAL STATE:\n", store.get_mental_state(character) or "暂无")
        print("\nRECENT CHAT:")
        for e in store.list_chat_events(character, 30):
            print(e.event_time, e.event_type.value, e.content)
        print("\nMEMORIES:")
        for m in store.list_memories(character, limit=30, include_embedding=False):
            print(m.event_time, m.memory_type, m.importance, m.content, f"source={m.source_event_id}")
        print("\nINTENTS:")
        for row in store.list_intents(character):
            print(row["created_at"], row["status"], row["content"])
    finally:
        store.close()


def _doctor(config_path: str, remote: bool):
    settings = load_settings(config_path)
    print(f"config: {config_path}")
    print(f"db: {settings.db_path}")
    print(f"chat model: {settings.chat_model}")
    print(f"base url: {settings.base_url}")
    print(f"embedding: {settings.embedding_provider} / {settings.embedding_model}")
    print(f"api key: {'set' if settings.api_key else 'MISSING'}")
    print("embedding: loading ...")
    embedding = build_embedding(settings)
    vector = embedding.embed("向量检索自检")
    print(f"embedding: OK ({len(vector)} dims)")
    if remote:
        model = build_model(settings)
        try:
            print("remote models:", model.check_remote())
            try:
                print("remote chat:", model.check_remote_chat())
            except Exception as exc:
                print("remote chat: FAILED")
                print(str(exc))
                raise SystemExit(2) from exc
        finally:
            model.close()


def _reembed(config_path: str, character: str):
    settings = load_settings(config_path)
    store = SQLiteStore(settings.db_path)
    try:
        embedding = build_embedding(settings)
        memories = store.list_memories(character, include_inactive=True)
        vectors = embedding.embed_many([m.content for m in memories])
        if len(vectors) != len(memories):
            raise ValueError("embedding batch size does not match memory count")
        # Compute before acquiring the transaction; failed writes roll back all vectors.
        with store.transaction():
            for memory, vector in zip(memories, vectors):
                store.update_memory_embedding(memory.id, vector)
        print(f"re-embedded {len(memories)} memories")
    finally:
        store.close()


def _expire_intents(config_path: str, *, character: str | None, due_before: str | None, all_pending: bool, apply: bool, backup: bool):
    """Retire the already-due Intent backlog without deleting anything.

    Dry-run by default: this touches a live product database, and the operator
    should read the per-character distribution before committing.
    """
    settings = load_settings(config_path)
    store = SQLiteStore(settings.db_path)
    try:
        if apply and backup:
            # sqlite3's online backup API, not a file copy: the store runs in
            # rollback-journal mode, so copying the file under a live service can
            # capture half-committed pages.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = f"{settings.db_path}.bak.{stamp}"
            with sqlite3.connect(target) as destination:
                store.conn.backup(destination)
            print(f"backup: {target}")
        plan = store.expire_due_backlog_intents(
            datetime.now().astimezone(),
            character_id=character,
            due_before=datetime.fromisoformat(due_before) if due_before else None,
            all_pending=all_pending,
            dry_run=not apply,
        )
        _print_json(plan)
        if not apply:
            print("dry-run: nothing written. Re-run with --apply to expire these intents.")
    finally:
        store.close()


def _run_eval(
    config_path: str,
    path: str,
    *,
    label: str = "local",
    report_path: str | None = None,
    live: bool = False,
    max_paid_calls: int = 10,
) -> int:
    settings = load_settings(config_path)
    meter = EvalCostMeter(paid=live)
    mode = "live" if live else "offline"
    with tempfile.TemporaryDirectory(prefix="character-memory-eval-") as tmp:
        # One HTTP attempt per logical eval call keeps the live budget honest:
        # the pre-call guard then caps real provider requests, not just retries.
        isolated = settings.model_copy(
            update={"db_path": str(Path(tmp) / "eval.db"), "llm_attempts": 1}
        )
        if live:
            try:
                bundle = build_app_from_settings(isolated)
            except Exception as exc:  # noqa: BLE001 - report a local setup gap
                raise SystemExit(f"eval --live environment unavailable: {exc}") from exc
            inner_model = bundle.model
            inner_model.usage_recorder = TeeUsageRecorder(meter, inner_model.usage_recorder)
            budgeted = BudgetedModel(inner_model, max_paid_calls)
            bundle.model = budgeted
            runtimes = getattr(bundle, "runtimes", None) or {"default": bundle.runtime}
            if isinstance(runtimes, dict):
                for runtime in runtimes.values():
                    runtime.model = budgeted
        else:
            bundle = build_offline_eval_bundle(isolated)
            runtimes = bundle.runtimes
        try:
            results = EvalRunner(runtimes, meter=meter).run_jsonl(path)
        finally:
            bundle.close()

    model_names = []
    if isinstance(runtimes, dict):
        model_names = [getattr(runtime.model, "model", "") for runtime in runtimes.values()]
    metadata = build_metadata(
        suite_path=path,
        model_names=model_names,
        persona_versions=persona_versions_from_runtimes(runtimes) if isinstance(runtimes, dict) else {},
        mode=mode,
        paid_calls_allowed=max_paid_calls,
    )
    report = build_report(results, label=label, metadata=metadata)
    if report_path:
        write_json(report_path, report)

    by_tag = {}
    for result in results:
        for tag in result.get("tags", []):
            bucket = by_tag.setdefault(tag, {"passed": 0, "total": 0, "statuses": {}})
            bucket["total"] += 1
            bucket["passed"] += int(result["status"] == "passed")
            status = result.get("status") or "engineering"
            bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
    summary = report["summary"]
    _print_json(
        {
            "run_id": report["run_id"],
            "label": report["label"],
            "status_counts": summary["status_counts"],
            "passed": summary["passed"],
            "contract_passed": summary["contract_passed"],
            "reviewed_passed": summary["reviewed_passed"],
            "unscored": summary["unscored"],
            "total": summary["total"],
            "paid_calls": report["cost"]["paid_calls"],
            "report": report_path,
            "metadata": metadata,
            "by_tag": by_tag,
            "results": results,
        }
    )
    if not results:
        return 2
    return 0 if summary["status_counts"].get("passed", 0) == len(results) else 1


def _run_eval_report(report_path: str, *, case_id: str | None = None, as_json: bool = False) -> int:
    report = load_report(report_path)
    if as_json:
        _print_json(report)
    else:
        print(render_report_text(report, case_id))
    return 0


def _run_eval_review(
    report_path: str,
    reviews_path: str,
    *,
    out_path: str | None = None,
    as_json: bool = False,
) -> int:
    report = load_report(report_path)
    reviews = load_reviews(reviews_path)
    try:
        merged = apply_reviews(report, reviews)
    except ValueError as exc:
        raise SystemExit(f"eval-review: {exc}") from exc
    if out_path:
        write_json(out_path, merged)
    if as_json or not out_path:
        _print_json(merged)
    else:
        print(render_report_text(merged))
    return 0


def _run_eval_compare(
    baseline_path: str,
    candidate_path: str,
    *,
    case_id: str | None = None,
    out_path: str | None = None,
    as_json: bool = False,
) -> int:
    baseline = load_report(baseline_path)
    candidate = load_report(candidate_path)
    comparison = compare_reports(baseline, candidate)
    if out_path:
        write_json(out_path, comparison)
    if as_json:
        _print_json(comparison)
    else:
        print(render_comparison_text(comparison, case_id))
    return 1 if comparison["summary"]["regressed"] else 0


def _run_server(config_path: str, host: str, port: int):
    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError as exc:
        raise SystemExit("WebUI dependencies are missing. Run: uv sync --extra api") from exc
    os.environ["CHARACTER_MEMORY_CONFIG"] = config_path
    # Production web startup eagerly warms the Person Runtime (including the
    # strict-offline local embedding) in a background thread while /health stays
    # immediately available.
    os.environ.setdefault("CHARACTER_MEMORY_EAGER_WARMUP", "1")
    print(f"web: http://{host}:{port}")
    # SSE requests are intentionally long-lived, so Uvicorn cannot wait for
    # every connection to end naturally during Ctrl+C. Keep graceful shutdown
    # bounded; the SSE iterator is async/cancellable, so cancellation is clean.
    uvicorn.run(
        "character_memory.server:app",
        host=host,
        port=port,
        reload=False,
        timeout_graceful_shutdown=2,
    )


def main():
    parser = argparse.ArgumentParser(prog="character-memory")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="create config.yaml from config.example.yaml")
    init.add_argument("--force", action="store_true")

    chat = sub.add_parser("chat")
    chat.add_argument("message")
    chat.add_argument("--character", default="rin")
    chat.add_argument("--conversation", default="cli")
    chat.add_argument("--at", default=None, help="ISO datetime; default uses RealClock")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--character", default="rin")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--remote", action="store_true")
    reembed = sub.add_parser("reembed")
    reembed.add_argument("--character", default="rin")
    expire = sub.add_parser("expire-intents", help="retire already-due PENDING intents (dry-run by default)")
    expire.add_argument("--character", default=None)
    expire.add_argument("--due-before", default=None, help="ISO datetime; defaults to now")
    expire.add_argument("--all-pending", action="store_true", help="expire every PENDING intent, not only the due ones")
    expire.add_argument("--apply", action="store_true", help="actually write; without it this is a dry run")
    expire.add_argument("--backup", action="store_true", help="take an online SQLite backup before writing")
    evaluate = sub.add_parser("eval")
    evaluate.add_argument("path", nargs="?", default="evals/smoke.jsonl")
    evaluate.add_argument("--label", default="local", help="report label, e.g. baseline or candidate")
    evaluate.add_argument("--report", default=None, help="write the full eval report JSON here")
    evaluate.add_argument(
        "--live",
        action="store_true",
        help="use the configured paid provider; default is the offline deterministic model",
    )
    evaluate.add_argument(
        "--max-paid-calls",
        type=int,
        default=10,
        help="hard cap for live provider calls (default and maximum 10)",
    )
    eval_report = sub.add_parser("eval-report", help="view an eval report by run or by case")
    eval_report.add_argument("report")
    eval_report.add_argument("--case", default=None)
    eval_report.add_argument("--json", action="store_true")
    eval_review = sub.add_parser("eval-review", help="merge an independent review artifact")
    eval_review.add_argument("report")
    eval_review.add_argument("--reviews", required=True)
    eval_review.add_argument("--out", default=None)
    eval_review.add_argument("--json", action="store_true")
    eval_compare = sub.add_parser("eval-compare", help="compare baseline and candidate reports")
    eval_compare.add_argument("baseline")
    eval_compare.add_argument("candidate")
    eval_compare.add_argument("--case", default=None)
    eval_compare.add_argument("--out", default=None)
    eval_compare.add_argument("--json", action="store_true")

    for name in ("serve", "web"):
        server = sub.add_parser(name)
        server.add_argument("--host", default="127.0.0.1")
        server.add_argument("--port", type=int, default=8000)

    inspector = sub.add_parser("inspector")
    inspector.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()

    if args.cmd == "init":
        dst = Path(args.config)
        if dst.exists() and not args.force:
            raise SystemExit(f"{dst} already exists; use --force to replace it")
        shutil.copyfile("config.example.yaml", dst)
        print(f"created {dst}")
        return
    if args.cmd == "inspect":
        _inspect(args.config, args.character)
        return
    if args.cmd == "doctor":
        _doctor(args.config, args.remote)
        return
    if args.cmd == "reembed":
        _reembed(args.config, args.character)
        return
    if args.cmd == "expire-intents":
        _expire_intents(
            args.config,
            character=args.character,
            due_before=args.due_before,
            all_pending=args.all_pending,
            apply=args.apply,
            backup=args.backup,
        )
        return
    if args.cmd == "eval":
        raise SystemExit(
            _run_eval(
                args.config,
                args.path,
                label=args.label,
                report_path=args.report,
                live=args.live,
                max_paid_calls=min(10, max(0, args.max_paid_calls)),
            )
        )
    if args.cmd == "eval-report":
        raise SystemExit(_run_eval_report(args.report, case_id=args.case, as_json=args.json))
    if args.cmd == "eval-review":
        raise SystemExit(
            _run_eval_review(
                args.report,
                args.reviews,
                out_path=args.out,
                as_json=args.json,
            )
        )
    if args.cmd == "eval-compare":
        raise SystemExit(
            _run_eval_compare(
                args.baseline,
                args.candidate,
                case_id=args.case,
                out_path=args.out,
                as_json=args.json,
            )
        )
    if args.cmd in {"serve", "web"}:
        _run_server(args.config, args.host, args.port)
        return
    if args.cmd == "inspector":
        try:
            import streamlit  # noqa: F401
        except ImportError as exc:
            raise SystemExit("Install UI extras first: pip install -e '.[ui]'") from exc
        os.environ["CHARACTER_MEMORY_CONFIG"] = args.config
        ui_path = Path(__file__).with_name("ui.py")
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(ui_path), "--server.port", str(args.port), "--server.fileWatcherType", "none", "--server.runOnSave", "false"], check=True)
        return

    bundle = build_app(args.config)
    try:
        if args.cmd == "chat":
            at = datetime.fromisoformat(args.at) if args.at else None
            result = bundle.chat.send(args.message, character_id=args.character, conversation_id=args.conversation, at=at)
            _print_json({"event_id": result.event.id, "event_time": result.event.event_time, "action": result.reaction.action.model_dump(mode="json") if result.reaction.action else None, "actions": [action.model_dump(mode="json") for action in result.reaction.actions], "perception": result.reaction.perception, "reaction": result.reaction.reaction, "mental_state": result.reaction.mental_state_update, "recalled_memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in result.recalled_memories]})
    finally:
        bundle.close()


if __name__ == "__main__":
    main()
