from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from character_memory.app import build_app, build_app_from_settings, build_embedding, build_model
from character_memory.config import load_settings
from character_memory.eval.runner import EvalRunner
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
        for memory, vector in zip(memories, vectors):
            store.update_memory_embedding(memory.id, vector)
        print(f"re-embedded {len(memories)} memories")
    finally:
        store.close()


def _run_eval(config_path: str, path: str):
    settings = load_settings(config_path)
    with tempfile.TemporaryDirectory(prefix="character-memory-eval-") as tmp:
        isolated = settings.model_copy(update={"db_path": str(Path(tmp) / "eval.db")})
        bundle = build_app_from_settings(isolated)
        try:
            results = EvalRunner(bundle.runtime).run_jsonl(path)
        finally:
            bundle.close()
    _print_json({"passed": sum(1 for r in results if r["pass"]), "total": len(results), "results": results})


def _run_server(config_path: str, host: str, port: int):
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install API extras first: pip install -e '.[api]'") from exc
    os.environ["CHARACTER_MEMORY_CONFIG"] = config_path
    print(f"web: http://{host}:{port}")
    uvicorn.run("character_memory.server:app", host=host, port=port, reload=False)


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

    day = sub.add_parser("day")
    day.add_argument("--character", default="rin")
    sim = sub.add_parser("simulate")
    sim.add_argument("days", type=int)
    sim.add_argument("--character", default="rin")
    tick = sub.add_parser("tick")
    tick.add_argument("--character", default="rin")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--character", default="rin")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--remote", action="store_true")
    reembed = sub.add_parser("reembed")
    reembed.add_argument("--character", default="rin")
    evaluate = sub.add_parser("eval")
    evaluate.add_argument("path", nargs="?", default="evals/smoke.jsonl")

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
    if args.cmd == "eval":
        _run_eval(args.config, args.path)
        return
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
            _print_json({"event_id": result.event.id, "event_time": result.event.event_time, "action": result.reaction.action.model_dump(mode="json"), "perception": result.reaction.perception, "reaction": result.reaction.reaction, "mental_state": result.reaction.mental_state_update, "recalled_memories": [m.model_dump(mode="json", exclude={"embedding"}) for m in result.recalled_memories]})
        elif args.cmd == "day":
            _print_json(bundle.days.run_next_day(args.character))
        elif args.cmd == "simulate":
            if args.days < 1 or args.days > 3650:
                raise SystemExit("days must be between 1 and 3650")
            for index, result in enumerate(bundle.days.simulate(args.character, args.days), start=1):
                print(f"day {index}: {result['date']} | life={result['life_events']} | ticks={result['ticks']} | proactive={result['proactive_messages']}")
        elif args.cmd == "tick":
            now = bundle.days.current_time(args.character)
            results = bundle.ticker.tick(args.character, now)
            bundle.store.set_world_time(args.character, now + timedelta(hours=1))
            _print_json([{"action": r.reaction.action.model_dump(mode="json"), "reaction": r.reaction.reaction, "recalled_memories": [m.id for m in r.recalled_memories]} for r in results])
    finally:
        bundle.close()


if __name__ == "__main__":
    main()
