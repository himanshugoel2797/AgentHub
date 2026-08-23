import asyncio
import json
import os
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

HOME = Path.home()
ROOT = HOME / "agent-hub"
STATE_DIR = ROOT / "state"
LOGS_DIR = STATE_DIR / "logs"
PROJECTS_ROOT = HOME / "agent-projects"
STATE_FILE = STATE_DIR / "state.json"
OPENHANDS = HOME / ".local/bin/openhands"

DEFAULT_STATE = {
    "settings": {
        "max_concurrent": 1,
        "timeout_minutes": 45,
        "scheduler": {"enabled": False, "start_hour": 1, "end_hour": 7},
    },
    "projects": [],
    "threads": {},
}

_processes: dict[str, asyncio.subprocess.Process] = {}


def _atomic_write(path: Path, data: str):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    return json.loads(STATE_FILE.read_text())


def save_state(s: dict):
    _atomic_write(STATE_FILE, json.dumps(s, indent=2))


def log_path(tid: str) -> Path:
    return LOGS_DIR / f"{tid}.log"


def git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )


def current_branch(repo: Path) -> str:
    r = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip()


def is_dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain").stdout.strip())


BACKLOG_RE = re.compile(r"^(\s*)- \[ \] (.*)$")


def backlog_items(repo: Path) -> list[str]:
    f = repo / "BACKLOG.md"
    if not f.exists():
        return []
    return [m.group(2).strip() for line in f.read_text().splitlines() if (m := BACKLOG_RE.match(line))]


def add_backlog_item(repo: Path, text: str):
    f = repo / "BACKLOG.md"
    with f.open("a") as fh:
        fh.write(f"- [ ] {text.strip()}\n")
    git(repo, "add", "BACKLOG.md")
    git(repo, "commit", "-q", "-m", f"backlog: add task")


def tick_backlog_item(repo: Path, text: str) -> bool:
    f = repo / "BACKLOG.md"
    lines = f.read_text().splitlines()
    for i, line in enumerate(lines):
        m = BACKLOG_RE.match(line)
        if m and m.group(2).strip() == text.strip():
            lines[i] = f"{m.group(1)}- [x] {m.group(2)}"
            f.write_text("\n".join(lines) + "\n")
            git(repo, "add", "BACKLOG.md")
            git(repo, "commit", "-q", "-m", "backlog: mark task done")
            return True
    return False


def create_project(name: str, description: str = "") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("project name must be alphanumeric/dash/underscore")
    path = PROJECTS_ROOT / name
    if path.exists():
        raise ValueError(f"project path already exists: {path}")
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / ".gitignore").write_text("__pycache__/\n.venv/\n")
    (path / "README.md").write_text(f"# {name}\n\n{description}\n")
    (path / "BACKLOG.md").write_text("# Backlog\n\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    s = load_state()
    p = {"name": name, "path": str(path), "description": description,
         "created": datetime.now().isoformat(timespec="seconds")}
    s["projects"].append(p)
    save_state(s)
    return p


def get_project(s: dict, name: str) -> dict:
    for p in s["projects"]:
        if p["name"] == name:
            return p
    raise KeyError(f"unknown project: {name}")


def enqueue_thread(project_name: str, prompt: str) -> dict:
    s = load_state()
    get_project(s, project_name)
    tid = uuid.uuid4().hex[:8]
    t = {
        "id": tid,
        "project": project_name,
        "prompt": prompt,
        "status": "queued",
        "branch": f"agent/hub-{tid}",
        "base_branch": None,
        "created": datetime.now().isoformat(timespec="seconds"),
        "finished": None,
        "result": None,
    }
    s["threads"][tid] = t
    save_state(s)
    return t


def cancel_thread(tid: str) -> bool:
    proc = _processes.get(tid)
    if proc and proc.returncode is None:
        proc.kill()
        return True
    return False


PROMPT_TEMPLATE = """You are working autonomously in the git repository at {repo} on branch {branch}.
Task:
{prompt}

Rules:
- Implement this task completely. Keep changes minimal and focused on the task.
- Do not modify anything outside {repo}. Do not push, fetch, or contact remotes.
- If the project has tests or a build, run them and make sure they pass before finishing.
- Commit all your changes with a concise message prefixed 'agent:'.
- If the task is impossible, ambiguous beyond repair, or you are stuck after honest effort: revert all uncommitted changes (git checkout -- . && git clean -fd), stay on the branch, and end your final message with BLOCKED: <reason>."""


async def _run_thread(tid: str):
    s = load_state()
    t = s["threads"][tid]
    p = get_project(s, t["project"])
    repo = Path(p["path"])
    timeout = s["settings"]["timeout_minutes"] * 60

    try:
        if is_dirty(repo):
            t["status"] = "failed"
            t["result"] = "dirty worktree; refusing to start"
            t["finished"] = datetime.now().isoformat(timespec="seconds")
            save_state(s)
            return
        orig = current_branch(repo)
        t["base_branch"] = orig
        git(repo, "checkout", "-b", t["branch"])
        save_state(s)

        env = dict(os.environ)
        env["PATH"] = str(HOME / ".local/bin") + ":" + env.get("PATH", "")
        env["OPENHANDS_SUPPRESS_BANNER"] = "1"
        lf = log_path(tid).open("w")
        proc = await asyncio.create_subprocess_exec(
            str(OPENHANDS), "--headless", "-t",
            PROMPT_TEMPLATE.format(repo=repo, branch=t["branch"], prompt=t["prompt"]),
            cwd=str(repo), stdout=lf, stderr=subprocess.STDOUT, env=env,
        )
        _processes[tid] = proc
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        finally:
            lf.close()
            _processes.pop(tid, None)

        blocked = "BLOCKED:" in log_path(tid).read_text(errors="replace")
        commits = int(git(repo, "rev-list", "--count", f"{orig}..HEAD").stdout.strip() or 0)
        dirty = is_dirty(repo)

        if dirty:
            git(repo, "checkout", "--", ".")
            git(repo, "clean", "-qfd")

        if commits > 0 and not dirty and not timed_out and not blocked:
            tick_backlog_item(repo, t["prompt"]) if t.get("source") == "backlog" else None
            git(repo, "checkout", "-q", orig)
            t["status"] = "done"
            t["result"] = f"{commits} commit(s) on {t['branch']}"
        elif blocked and commits == 0:
            git(repo, "checkout", "-q", orig)
            t["status"] = "blocked"
            t["result"] = "agent reported BLOCKED"
        elif timed_out:
            t["status"] = "timeout"
            t["result"] = f"timed out after {s['settings']['timeout_minutes']}m; inspect branch {t['branch']}"
            git(repo, "checkout", "-q", orig)
        else:
            git(repo, "checkout", "-q", orig)
            t["status"] = "failed"
            t["result"] = f"rc={proc.returncode} commits={commits} dirty={dirty}"
    except Exception as e:
        t["status"] = "failed"
        t["result"] = f"engine error: {e}"
    t["finished"] = datetime.now().isoformat(timespec="seconds")
    save_state(s)


async def worker_loop():
    while True:
        try:
            s = load_state()
            cap = s["settings"]["max_concurrent"]
            running = [t for t in s["threads"].values() if t["status"] == "running"]
            busy = {t["project"] for t in running}
            if len(running) < cap:
                for t in sorted(s["threads"].values(), key=lambda x: x["created"]):
                    if t["status"] == "queued" and t["project"] not in busy:
                        t["status"] = "running"
                        save_state(s)
                        busy.add(t["project"])
                        asyncio.create_task(_run_thread(t["id"]))
                        break
        except Exception:
            pass
        await asyncio.sleep(3)


def llm_up() -> bool:
    import urllib.request
    try:
        urllib.request.urlopen("http://home-spark0:8888/v1/models", timeout=5)
        return True
    except Exception:
        return False


async def scheduler_loop():
    while True:
        try:
            s = load_state()
            sch = s["settings"]["scheduler"]
            if sch["enabled"]:
                h = datetime.now().hour
                in_window = (
                    sch["start_hour"] <= sch["end_hour"]
                    and sch["start_hour"] <= h < sch["end_hour"]
                )
                if in_window and llm_up():
                    running_projects = {t["project"] for t in s["threads"].values() if t["status"] in ("running", "queued")}
                    for p in s["projects"]:
                        if p["name"] in running_projects:
                            continue
                        repo = Path(p["path"])
                        if is_dirty(repo):
                            continue
                        items = backlog_items(repo)
                        if items:
                            t = enqueue_thread(p["name"], items[0])
                            st = load_state()
                            st["threads"][t["id"]]["source"] = "backlog"
                            save_state(st)
                            break
        except Exception:
            pass
        await asyncio.sleep(60)


def recover_on_startup():
    s = load_state()
    changed = False
    for t in s["threads"].values():
        if t["status"] == "running":
            t["status"] = "failed"
            t["result"] = "interrupted by restart"
            changed = True
    if changed:
        save_state(s)


def merge_thread(tid: str) -> str:
    s = load_state()
    t = s["threads"][tid]
    if t["status"] != "done":
        raise ValueError("only done threads can be merged")
    p = get_project(s, t["project"])
    repo = Path(p["path"])
    if is_dirty(repo):
        raise ValueError("worktree dirty; commit or stash first")
    base = t.get("base_branch") or current_branch(repo)
    cur = current_branch(repo)
    if cur != base:
        git(repo, "checkout", "-q", base)
    r = git(repo, "merge", "--ff-only", t["branch"])
    ok = r.returncode == 0
    if not ok:
        git(repo, "merge", "--abort") if Path(repo / ".git", "MERGE_HEAD").exists() else None
    return r.stdout.strip() or r.stderr.strip() if not ok else f"merged {t['branch']} into {base}"
