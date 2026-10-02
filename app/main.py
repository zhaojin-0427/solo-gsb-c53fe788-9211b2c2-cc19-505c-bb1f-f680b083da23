"""FastAPI 入口:静态页面 + REST API。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .solver import solve_schedule

SOLVE_TIME_LIMIT = 10.0  # 求解限时 10 秒

app = FastAPI(title="藏品光照轮换台")

STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------- 请求模型 ----------

class SettingsIn(BaseModel):
    num_slots: int = Field(ge=1, le=db.MAX_SLOTS)
    num_booths: int = Field(ge=1, le=db.MAX_BOOTHS)
    lux: list[int] = Field(min_length=1, max_length=db.MAX_BOOTHS)


class ArtifactIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    budget: int = Field(ge=0, le=db.MAX_BUDGET)
    priority: int = Field(ge=1, le=db.MAX_PRIORITY)
    min_hours: int = Field(ge=0, le=db.MAX_SLOTS)


class LockIn(BaseModel):
    artifact_id: int
    slot: int = Field(ge=0, le=db.MAX_SLOTS - 1)
    # 'storage'、展位序号(0 起),null 表示清除锁定
    location: Optional[Literal["storage"] | int] = None


# ---------- 页面 ----------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------- 状态 ----------

@app.get("/api/state")
def get_state() -> dict:
    settings = db.get_settings()
    return {
        "settings": settings,
        "artifacts": db.list_artifacts(),
        "locks": db.list_locks(),
        "schedules": db.list_schedules(),
        "limits": {
            "max_slots": db.MAX_SLOTS,
            "max_booths": db.MAX_BOOTHS,
            "max_artifacts": db.MAX_ARTIFACTS,
            "max_lux": db.MAX_LUX,
            "max_budget": db.MAX_BUDGET,
            "max_priority": db.MAX_PRIORITY,
            "solve_time_limit": SOLVE_TIME_LIMIT,
        },
    }


@app.put("/api/settings")
def put_settings(body: SettingsIn) -> dict:
    if len(body.lux) != body.num_booths:
        raise HTTPException(400, f"需要 {body.num_booths} 个展位照度值")
    for v in body.lux:
        if not 1 <= v <= db.MAX_LUX:
            raise HTTPException(400, f"照度须在 1~{db.MAX_LUX} lx 之间")
    db.save_settings(body.num_slots, body.num_booths, body.lux)
    return db.get_settings()


@app.post("/api/artifacts", status_code=201)
def post_artifact(body: ArtifactIn) -> dict:
    if db.count_artifacts() >= db.MAX_ARTIFACTS:
        raise HTTPException(400, f"藏品数量已达上限 {db.MAX_ARTIFACTS} 件")
    return db.add_artifact(body.name.strip(), body.budget, body.priority, body.min_hours)


@app.put("/api/artifacts/{artifact_id}")
def put_artifact(artifact_id: int, body: ArtifactIn) -> dict:
    if not db.update_artifact(artifact_id, body.name.strip(), body.budget, body.priority, body.min_hours):
        raise HTTPException(404, "藏品不存在")
    return {"ok": True}


@app.delete("/api/artifacts/{artifact_id}")
def del_artifact(artifact_id: int) -> dict:
    if not db.delete_artifact(artifact_id):
        raise HTTPException(404, "藏品不存在")
    return {"ok": True}


@app.put("/api/locks")
def put_lock(body: LockIn) -> dict:
    settings = db.get_settings()
    if not db.artifact_exists(body.artifact_id):
        raise HTTPException(404, "藏品不存在")
    if body.slot >= settings["num_slots"]:
        raise HTTPException(400, "时段超出范围")
    if isinstance(body.location, int) and not 0 <= body.location < settings["num_booths"]:
        raise HTTPException(400, "展位超出范围")
    db.set_lock(body.artifact_id, body.slot, body.location)
    return {"ok": True}


# ---------- 求解与历史 ----------

@app.post("/api/solve")
def solve() -> dict:
    # 一次求解的设置、藏品、锁定取自同一数据库一致视图;求解期间(最长
    # 10 秒)其他请求可以继续编辑,但 result 必须与这份 snapshot 一起保存,
    # 保存时不得重读配置,以免旧结果配上新快照。
    config = db.read_config()
    if not config["artifacts"]:
        raise HTTPException(400, "请先添加至少一件藏品")
    result = solve_schedule(config, time_limit=SOLVE_TIME_LIMIT)
    schedule_id = db.save_schedule(result, config)
    return db.get_schedule(schedule_id)


@app.get("/api/schedules")
def get_schedules() -> list[dict]:
    return db.list_schedules()


@app.get("/api/schedules/{schedule_id}")
def get_schedule(schedule_id: int) -> dict:
    sched = db.get_schedule(schedule_id)
    if sched is None:
        raise HTTPException(404, "排班不存在")
    return sched


@app.delete("/api/schedules/{schedule_id}")
def del_schedule(schedule_id: int) -> dict:
    if not db.delete_schedule(schedule_id):
        raise HTTPException(404, "排班不存在")
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
