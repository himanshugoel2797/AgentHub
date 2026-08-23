import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app import engine
from app import mgmt

app = FastAPI(title="agent-hub")


@app.on_event("startup")
async def startup():
    STATE_DIR = engine.STATE_DIR
    LOGS_DIR = engine.LOGS_DIR
    PROJECTS_ROOT = engine.PROJECTS_ROOT
    for d in (STATE_DIR, LOGS_DIR, PROJECTS_ROOT):
        d.mkdir(parents=True, exist_ok=True)
    engine.recover_on_startup()
    asyncio.create_task(engine.worker_loop())
    asyncio.create_task(engine.scheduler_loop())


class ProjectIn(BaseModel):
    name: str
    description: str = ""


class TaskIn(BaseModel):
    task: str


class ThreadIn(BaseModel):
    prompt: str | None = None
    use_backlog: bool = False


class MgmtIn(BaseModel):
    message: str


class SchedulerIn(BaseModel):
    enabled: bool
    start_hour: int | None = None
    end_hour: int | None = None


@app.get("/", response_class=HTMLResponse)
async def index():
    return (Path(__file__).parent.parent / "static" / "index.html").read_text()


@app.get("/api/state")
async def state():
    s = engine.load_state()
    threads = []
    for t in sorted(s["threads"].values(), key=lambda x: x["created"], reverse=True):
        t = dict(t)
        t.pop("prompt", None)
        threads.append(t)
    return {"settings": {k: v for k, v in s["settings"].items()},
            "projects": s["projects"], "threads": threads}


@app.post("/api/projects")
async def add_project(body: ProjectIn):
    try:
        p = engine.create_project(body.name.strip(), body.description.strip())
        return p
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{name}/tasks")
async def add_task(name: str, body: TaskIn):
    s = engine.load_state()
    try:
        p = engine.get_project(s, name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    engine.add_backlog_item(Path(p["path"]), body.task)
    return {"ok": True}


@app.post("/api/projects/{name}/threads")
async def add_thread(name: str, body: ThreadIn):
    s = engine.load_state()
    try:
        p = engine.get_project(s, name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if body.use_backlog:
        items = engine.backlog_items(Path(p["path"]))
        if not items:
            raise HTTPException(400, "backlog is empty")
        t = engine.enqueue_thread(name, items[0])
        st = engine.load_state()
        st["threads"][t["id"]]["source"] = "backlog"
        engine.save_state(st)
        return t
    if not body.prompt:
        raise HTTPException(400, "prompt required")
    return engine.enqueue_thread(name, body.prompt)


@app.get("/api/logs/{tid}", response_class=PlainTextResponse)
async def logs(tid: str):
    f = engine.log_path(tid)
    if not f.exists():
        return "(no log yet)"
    data = f.read_text(errors="replace")
    return data[-20000:]


@app.post("/api/threads/{tid}/cancel")
async def cancel(tid: str):
    return {"cancelled": engine.cancel_thread(tid)}


@app.post("/api/threads/{tid}/merge")
async def merge(tid: str):
    try:
        return {"result": engine.merge_thread(tid)}
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/scheduler")
async def scheduler(body: SchedulerIn):
    s = engine.load_state()
    sch = s["settings"]["scheduler"]
    sch["enabled"] = body.enabled
    if body.start_hour is not None:
        sch["start_hour"] = body.start_hour
    if body.end_hour is not None:
        sch["end_hour"] = body.end_hour
    engine.save_state(s)
    return sch


_mgmt_history: list = []


@app.post("/api/mgmt")
async def mgmt_chat(body: MgmtIn):
    try:
        out = await asyncio.to_thread(mgmt.chat, body.message, _mgmt_history)
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")
    _mgmt_history[:] = out["messages"][-12:]
    return {"reply": out["reply"]}
