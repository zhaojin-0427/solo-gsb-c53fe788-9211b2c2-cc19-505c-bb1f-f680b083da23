"""SQLite 数据访问层。

表结构:
- settings: 单行,时段数 / 展位数 / 各展位照度(JSON 数组,长度恒为 3)
- artifacts: 藏品(名称、剩余光照预算、优先级、最低展出时数)
- locks: 锁定(藏品, 时段) -> 位置('storage' 或展位序号字符串)
- schedules: 求解历史,保存输入快照与结果,刷新后仍可查看
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "scheduler.db")

_lock = threading.Lock()

MAX_SLOTS = 72
MAX_BOOTHS = 3
MAX_ARTIFACTS = 8
MAX_LUX = 10000
MAX_BUDGET = 720000
MAX_PRIORITY = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    num_slots INTEGER NOT NULL,
    num_booths INTEGER NOT NULL,
    lux TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    budget INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    min_hours INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS locks (
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL,
    location TEXT NOT NULL,
    PRIMARY KEY (artifact_id, slot)
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    weighted_hours INTEGER,
    transitions INTEGER,
    snapshot TEXT NOT NULL,
    result TEXT
);
"""

_DEFAULT_SETTINGS = {"num_slots": 12, "num_booths": 2, "lux": [200, 200, 200]}
_SEED_ARTIFACTS = [
    ("青铜鼎", 600000, 8, 4),
    ("绢本山水画", 50000, 10, 3),
    ("漆器盒", 200000, 5, 0),
]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(_SCHEMA)
        if c.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
            c.execute(
                "INSERT INTO settings (id, num_slots, num_booths, lux) VALUES (1, ?, ?, ?)",
                (_DEFAULT_SETTINGS["num_slots"], _DEFAULT_SETTINGS["num_booths"],
                 json.dumps(_DEFAULT_SETTINGS["lux"])),
            )
        if c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0:
            for pos, (name, budget, priority, min_hours) in enumerate(_SEED_ARTIFACTS):
                c.execute(
                    "INSERT INTO artifacts (name, budget, priority, min_hours, position)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, budget, priority, min_hours, pos),
                )


# ---------- 设置 ----------

def get_settings() -> dict:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    lux = json.loads(row["lux"])
    while len(lux) < MAX_BOOTHS:
        lux.append(_DEFAULT_SETTINGS["lux"][0])
    return {"num_slots": row["num_slots"], "num_booths": row["num_booths"], "lux": lux[:MAX_BOOTHS]}


def save_settings(num_slots: int, num_booths: int, lux: list[int]) -> None:
    lux = (list(lux) + [_DEFAULT_SETTINGS["lux"][0]] * MAX_BOOTHS)[:MAX_BOOTHS]
    with _lock, _conn() as c:
        c.execute(
            "UPDATE settings SET num_slots = ?, num_booths = ?, lux = ? WHERE id = 1",
            (num_slots, num_booths, json.dumps(lux)),
        )
        # 清理超出新范围的锁定
        c.execute("DELETE FROM locks WHERE slot >= ?", (num_slots,))
        c.execute(
            "DELETE FROM locks WHERE location != 'storage' AND CAST(location AS INTEGER) >= ?",
            (num_booths,),
        )


# ---------- 藏品 ----------

def list_artifacts() -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM artifacts ORDER BY position, id"
        ).fetchall()
    return [dict(r) for r in rows]


def count_artifacts() -> int:
    with _lock, _conn() as c:
        return c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]


def add_artifact(name: str, budget: int, priority: int, min_hours: int) -> dict:
    with _lock, _conn() as c:
        pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM artifacts").fetchone()[0]
        cur = c.execute(
            "INSERT INTO artifacts (name, budget, priority, min_hours, position)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, budget, priority, min_hours, pos),
        )
        row = c.execute("SELECT * FROM artifacts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def update_artifact(artifact_id: int, name: str, budget: int, priority: int, min_hours: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE artifacts SET name = ?, budget = ?, priority = ?, min_hours = ? WHERE id = ?",
            (name, budget, priority, min_hours, artifact_id),
        )
        return cur.rowcount > 0


def delete_artifact(artifact_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        return cur.rowcount > 0


def artifact_exists(artifact_id: int) -> bool:
    with _lock, _conn() as c:
        return c.execute("SELECT 1 FROM artifacts WHERE id = ?", (artifact_id,)).fetchone() is not None


# ---------- 锁定 ----------

def list_locks() -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM locks").fetchall()
    out = []
    for r in rows:
        loc = r["location"]
        out.append({
            "artifact_id": r["artifact_id"],
            "slot": r["slot"],
            "location": "storage" if loc == "storage" else int(loc),
        })
    return out


def set_lock(artifact_id: int, slot: int, location) -> None:
    """location: 'storage'、展位序号 int,或 None 表示清除。"""
    with _lock, _conn() as c:
        if location is None:
            c.execute(
                "DELETE FROM locks WHERE artifact_id = ? AND slot = ?", (artifact_id, slot)
            )
        else:
            loc = "storage" if location == "storage" else str(int(location))
            c.execute(
                "INSERT INTO locks (artifact_id, slot, location) VALUES (?, ?, ?)"
                " ON CONFLICT (artifact_id, slot) DO UPDATE SET location = excluded.location",
                (artifact_id, slot, loc),
            )


# ---------- 排班历史 ----------

def read_config() -> dict:
    """在同一个 SQLite 事务内读取设置、藏品与锁定,返回求解输入快照。

    三类数据来自数据库的同一一致视图:读取期间其他事务的提交不会影响
    本快照。求解最长耗时 10 秒,期间配置可被其他请求修改,但调用方拿到
    的始终是求解开始那一刻的配置,保存历史时也必须沿用这份快照。
    """
    c = _conn()
    try:
        with _lock:
            # 显式开启事务,使下面三条 SELECT 共享同一读快照
            # (sqlite3 默认隔离级别不会为 SELECT 自动开启事务)。
            c.execute("BEGIN")
            srow = c.execute("SELECT * FROM settings WHERE id = 1").fetchone()
            arows = c.execute(
                "SELECT * FROM artifacts ORDER BY position, id"
            ).fetchall()
            lrows = c.execute("SELECT * FROM locks").fetchall()
            c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        c.close()
        raise
    c.close()

    lux = json.loads(srow["lux"])
    while len(lux) < MAX_BOOTHS:
        lux.append(_DEFAULT_SETTINGS["lux"][0])
    lux = lux[:MAX_BOOTHS]
    locks = []
    for r in lrows:
        loc = r["location"]
        locks.append({
            "artifact_id": r["artifact_id"],
            "slot": r["slot"],
            "location": "storage" if loc == "storage" else int(loc),
        })
    return {
        "num_slots": srow["num_slots"],
        "booths": lux[: srow["num_booths"]],
        "artifacts": [dict(r) for r in arows],
        "locks": locks,
    }


def save_schedule(result: dict, snapshot: dict) -> int:
    """保存一次求解。snapshot 必须是本次求解实际使用的 read_config() 快照,

    保证结果(含无解 / 超时状态)与其输入配置严格对应:求解期间对设置、
    藏品或锁定的修改不会污染这条历史记录。
    """
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO schedules (created_at, status, weighted_hours, transitions, snapshot, result)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                result["status"],
                result.get("weighted_hours"),
                result.get("transitions"),
                json.dumps(snapshot, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        return cur.lastrowid


def list_schedules() -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id, created_at, status, weighted_hours, transitions"
            " FROM schedules ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_schedule(schedule_id: int) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "weighted_hours": row["weighted_hours"],
        "transitions": row["transitions"],
        "snapshot": json.loads(row["snapshot"]),
        "result": json.loads(row["result"]) if row["result"] else None,
    }


def delete_schedule(schedule_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return cur.rowcount > 0
