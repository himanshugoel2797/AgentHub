# agent-hub

agent-hub is a self-hosted, autonomous coding platform. It pairs a small web UI with a background job dispatcher that runs [OpenHands](https://github.com/All-Hands-AI/OpenHands) agent jobs against a local SGLang inference server, so you can queue coding tasks for your projects and let agents work on them in git branches while you do other things.

## Features

- **Projects** — each project is a plain git repository under `~/agent-projects` with a `BACKLOG.md` containing `- [ ] task` items. Create and manage projects from the UI.
- **Serialized threads** — a *thread* is one agent job (an OpenHands run with a prompt). Threads within a project run one at a time, each on its own `agent/hub-<id>` branch; on success the branch is fast-forward merged back into its base branch. You can also enqueue ad-hoc jobs that are not backlog items.
- **Git-tracked history** — everything the agents do lives in git: backlog edits are committed, each job's work lands on a dedicated branch, and completed jobs are merged by fast-forward. State (settings, threads, projects) is persisted in `state/state.json`.
- **Background scheduler** — an optional time-windowed scheduler (e.g. 01:00–07:00) that automatically picks up pending backlog items while your machine is idle. Toggle and configure the window from the UI header.
- **Management chat** — a built-in chat with a tool-carrying controller agent (backed by the local LLM) that can list projects, create projects, add tasks, queue jobs, check status, configure the scheduler, and even edit the agent-hub app itself and restart the service.
- **Live logs** — every thread's agent output is captured to `state/logs/<id>.log` and viewable from the UI, with cancel and merge controls per thread.

## Installation & Running

Prerequisites: Python 3.10+, `git`, `openhands` installed at `~/.local/bin/openhands`, and a reachable OpenAI-compatible SGLang server (default: `http://home-spark0:8888/v1/chat/completions`, model `qwen3.8-27b-sglang`).

```bash
cd ~/agent-hub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### systemd user service (recommended)

A user-level service serves the app on **port 8787**:

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-hub
```

Then open <http://localhost:8787>.

To make the service available at login without a TTY:

```bash
loginctl enable-linger $USER
```

### Manual run

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
```

On startup the service creates `state/` and `state/logs/`, recovers any in-flight threads from the last run, and starts the worker and scheduler loops.

## Architecture

```
Browser ──▶ FastAPI app (app/main.py, port 8787)
              ├── app/engine.py  — state, projects, thread queue, worker loop,
              │                    scheduler loop; shells out to `openhands`
              │                    subprocesses, one per thread, each working on
              │                    an agent/hub-* git branch inside the project
              │                    repo under ~/agent-projects
              └── app/mgmt.py    — management chat: tool-calling loop against the
                                   local SGLang server (home-spark0:8888) with
                                   tools that drive the same engine

Projects:  ~/agent-projects/<name>/          (git repos + BACKLOG.md)
State:     ~/agent-hub/state/state.json      (atomic writes)
Logs:      ~/agent-hub/state/logs/<thread>.log
```

Key behaviors: threads are serialized per project (default `max_concurrent: 1`), each run has a `timeout_minutes` cap (default 45), completed threads can be fast-forward merged from the UI or via the chat, and the scheduler ticks during its configured window to keep backlog items moving overnight.
