import json
import re
import subprocess
from pathlib import Path

import httpx

from app import engine

ROOT = engine.ROOT
STATE_FILE = engine.STATE_FILE
LLM_URL = "http://home-spark0:8888/v1/chat/completions"
MODEL = "qwen3.8-27b-sglang"

TOOLS = [
    {"type": "function", "function": {"name": "list_projects", "description": "List all projects with pending backlog counts", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "create_project", "description": "Create a new project (git repo under ~/agent-projects) with a BACKLOG.md", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "add_task", "description": "Append a task to a project's BACKLOG.md and commit it", "parameters": {"type": "object", "properties": {"project": {"type": "string"}, "task": {"type": "string"}}, "required": ["project", "task"]}}},
    {"type": "function", "function": {"name": "enqueue_job", "description": "Queue an ad-hoc agent job (thread) for a project; runs when the project is idle. Use for one-off instructions that are not backlog items.", "parameters": {"type": "object", "properties": {"project": {"type": "string"}, "instruction": {"type": "string"}}, "required": ["project", "instruction"]}}},
    {"type": "function", "function": {"name": "run_next_backlog", "description": "Queue the next backlog item of a project as a thread", "parameters": {"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]}}},
    {"type": "function", "function": {"name": "get_status", "description": "Full snapshot: settings, scheduler, projects, threads with statuses", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "set_scheduler", "description": "Configure background scheduler: enable and time window (hours, 24h clock)", "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "start_hour": {"type": "integer"}, "end_hour": {"type": "integer"}}, "required": ["enabled"]}}},
    {"type": "function", "function": {"name": "set_config", "description": "Set max_concurrent or timeout_minutes", "parameters": {"type": "object", "properties": {"key": {"type": "string", "enum": ["max_concurrent", "timeout_minutes"]}, "value": {"type": "integer"}}, "required": ["key", "value"]}}},
    {"type": "function", "function": {"name": "merge_thread", "description": "Fast-forward merge a done thread's branch into its base branch", "parameters": {"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"]}}},
    {"type": "function", "function": {"name": "read_app_file", "description": "Read a file inside the agent-hub app directory", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_app_file", "description": "Write/overwrite a file inside the agent-hub directory: anything under app/, static/, or a top-level *.md doc (README.md etc.). Changes are committed to the hub git repo. Call restart_service after changing app code.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "restart_service", "description": "Restart the agent-hub systemd service to apply code/config changes", "parameters": {"type": "object", "properties": {}, "required": []}}},
]

SYSTEM_PROMPT = """You are the management controller of 'agent-hub', a self-hosted autonomous coding platform.
It dispatches OpenHands agent jobs against a local LLM server (Qwen3.8 on home-spark0:8888).
Projects are git repos in ~/agent-projects with a BACKLOG.md of '- [ ] task' items.
Threads are agent jobs; each runs on its own agent/hub-* branch and is merged by fast-forward.
Your job:
- Fulfil the user's operational requests using the tools (create projects, add tasks, run jobs, configure scheduling).
- When asked to improve or change the web interface/tool itself, use read_app_file/write_app_file (paths are relative to the hub root; app code lives in app/, UI in static/index.html, and you may maintain top-level *.md docs such as README.md) then restart_service for code changes. Keep changes small and working; python syntax errors will break the service, so be careful.
- Be concise in final answers. Summarize what you did.
- Never delete projects or user data."""


def _safe_path(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    if not str(p).startswith(str(ROOT)):
        raise ValueError("path escapes hub root")
    return p


def execute(name: str, args: dict) -> str:
    try:
        if name == "list_projects":
            s = engine.load_state()
            out = []
            for p in s["projects"]:
                items = engine.backlog_items(Path(p["path"]))
                out.append({"name": p["name"], "pending_backlog": len(items), "next": items[0] if items else None})
            return json.dumps(out)
        if name == "create_project":
            p = engine.create_project(args["name"].strip(), args.get("description", ""))
            return f"created {p['path']}"
        if name == "add_task":
            s = engine.load_state()
            p = engine.get_project(s, args["project"])
            engine.add_backlog_item(Path(p["path"]), args["task"])
            return "task added to backlog"
        if name == "enqueue_job":
            t = engine.enqueue_thread(args["project"], args["instruction"])
            return f"queued thread {t['id']}"
        if name == "run_next_backlog":
            s = engine.load_state()
            p = engine.get_project(s, args["project"])
            items = engine.backlog_items(Path(p["path"]))
            if not items:
                return "backlog is empty"
            t = engine.enqueue_thread(p["name"], items[0])
            st = engine.load_state()
            st["threads"][t["id"]]["source"] = "backlog"
            engine.save_state(st)
            return f"queued thread {t['id']} for: {items[0][:100]}"
        if name == "get_status":
            s = engine.load_state()
            threads = [{k: v for k, v in t.items() if k != "prompt"} | {"prompt_head": t["prompt"][:80]} for t in s["threads"].values()]
            return json.dumps({"settings": s["settings"], "projects": [p["name"] for p in s["projects"]], "threads": list(threads)[-30:]})
        if name == "set_scheduler":
            s = engine.load_state()
            sch = s["settings"]["scheduler"]
            sch["enabled"] = bool(args["enabled"])
            sch["start_hour"] = int(args.get("start_hour", sch["start_hour"]))
            sch["end_hour"] = int(args.get("end_hour", sch["end_hour"]))
            engine.save_state(s)
            return f"scheduler now {sch}"
        if name == "set_config":
            k, v = args["key"], int(args["value"])
            if k == "max_concurrent" and not 1 <= v <= 4:
                raise ValueError("max_concurrent must be 1-4")
            s = engine.load_state()
            s["settings"][k] = v
            engine.save_state(s)
            return f"{k}={v}"
        if name == "merge_thread":
            return engine.merge_thread(args["thread_id"])
        if name == "read_app_file":
            return _safe_path(args["path"]).read_text()[:20000]
        if name == "write_app_file":
            path = _safe_path(args["path"])
            rel = path.relative_to(ROOT)
            allowed = str(rel).startswith(("app/", "static/")) or (
                "/" not in rel.as_posix() and rel.suffix == ".md"
            )
            if not allowed:
                raise ValueError("only app/, static/, or top-level *.md files may be written")
            if path.suffix == ".py":
                compile(args["content"], str(path), "exec")
            path.parent.mkdir(parents=True, exist_ok=True)
            old = path.read_text() if path.exists() else ""
            path.write_text(args["content"])
            subprocess.run(["git", "-C", str(ROOT), "add", str(rel)], capture_output=True)
            msg = f"mgmt-chat: update {rel}"
            if not old:
                msg = f"mgmt-chat: create {rel}"
            subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m", msg,
                            "--author=mgmt-chat <agent@local>"], capture_output=True)
            return f"wrote {rel} ({len(args['content'])} bytes), committed to hub repo"
        if name == "restart_service":
            import threading

            def _later():
                subprocess.Popen(["systemctl", "--user", "restart", "agent-hub"])

            threading.Timer(2.0, _later).start()
            return "restart scheduled in 2s"
        return f"unknown tool {name}"
    except Exception as e:
        return f"ERROR: {e}"


def chat(message: str, history: list) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history[-12:] + [
        {"role": "user", "content": message}
    ]
    for _ in range(8):
        r = httpx.post(
            LLM_URL,
            json={"model": MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                  "chat_template_kwargs": {"enable_thinking": False}},
            timeout=300,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"reply": msg.get("content") or "", "messages": messages[1:]}
        for c in calls:
            fn = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute(fn, args)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result[:4000]})
    return {"reply": "gave up after 8 tool iterations", "messages": messages[1:]}
