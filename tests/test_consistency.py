"""端到端验证:求解期间编辑配置,历史快照不得错配。

环境无法安装 fastapi / ortools,这里用最小桩模块加载真实的 app.main,
求解器被替换为可挂起的桩,以便在"求解中"插入配置编辑。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------- 桩:fastapi ----------
fastapi = types.ModuleType("fastapi")
responses = types.ModuleType("fastapi.responses")
staticfiles_mod = types.ModuleType("fastapi.staticfiles")


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


def _make_decorator(method_name):
    def decorator(self, path, **kwargs):
        def wrap(fn):
            self.routes[(method_name.upper(), path)] = fn
            return fn
        return wrap
    return decorator


class FastAPI:
    def __init__(self, title=""):
        self.title = title
        self.routes = {}

    get = _make_decorator("get")
    post = _make_decorator("post")
    put = _make_decorator("put")
    delete = _make_decorator("delete")

    def on_event(self, _name):
        def wrap(fn):
            self.startup = fn
            return fn
        return wrap

    def mount(self, *a, **k):
        pass

    def call(self, key, *args):
        return self.routes[key](*args)


fastapi.FastAPI = FastAPI
fastapi.HTTPException = HTTPException


class FileResponse:
    def __init__(self, path):
        self.path = path


responses.FileResponse = FileResponse


class StaticFiles:
    def __init__(self, *a, **k):
        pass


staticfiles_mod.StaticFiles = StaticFiles
sys.modules["fastapi"] = fastapi
sys.modules["fastapi.responses"] = responses
sys.modules["fastapi.staticfiles"] = staticfiles_mod

# ---------- 桩:pydantic ----------
pydantic = types.ModuleType("pydantic")


class BaseModel:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def Field(default=None, **_kw):
    return default


pydantic.BaseModel = BaseModel
pydantic.Field = Field
sys.modules["pydantic"] = pydantic

# ---------- 桩:ortools ----------
ortools = types.ModuleType("ortools")
ortools_sat = types.ModuleType("ortools.sat.python")


class _CpModel:
    pass


class _CpSolver:
    pass


ortools_sat.cp_model = types.SimpleNamespace(
    CpModel=_CpModel,
    CpSolver=_CpSolver,
    OPTIMAL=4, FEASIBLE=2, INFEASIBLE=3, UNKNOWN=0, MODEL_INVALID=1,
)
ortools.sat = types.SimpleNamespace(python=ortools_sat)
sys.modules["ortools"] = ortools
sys.modules["ortools.sat"] = ortools.sat
sys.modules["ortools.sat.python"] = ortools_sat

# ---------- 独立 DATA_DIR,隔离真实数据 ----------
tmp = tempfile.mkdtemp(prefix="sched-test-")
os.environ["DATA_DIR"] = tmp

from app import db  # noqa: E402
import app.main as main  # noqa: E402

db.init_db()

failures = []


def check(cond, msg):
    print(("PASS" if cond else "FAIL") + " - " + msg)
    if not cond:
        failures.append(msg)


# ---------- 1. DB:读事务持有期间,其他连接的写入对它不可见 ----------
hold = threading.Event()
proceed = threading.Event()
seen = {}


def reader():
    with db._lock, db._conn() as c:
        c.execute("BEGIN")
        seen["cfg"] = db._read_snapshot(c)
        hold.set()
        proceed.wait(5)
        c.rollback()


t = threading.Thread(target=reader)
t.start()
hold.wait(5)
# 读事务打开后,主线程改动设置与藏品
db.save_settings(24, 3, [300, 400, 500])
db.add_artifact("测试中新藏品", 123, 3, 2)
proceed.set()
t.join()

old = seen["cfg"]
check(old["num_slots"] == 12, f"读事务内快照保持旧时段数 12,实际 {old['num_slots']}")
check(old["booths"] == [200, 200], f"读事务内快照保持旧照度 [200,200],实际 {old['booths']}")
check(len(old["artifacts"]) == 3, f"读事务内快照保持旧藏品 3 件,实际 {len(old['artifacts'])}")

fresh = db.current_config()
check(fresh["num_slots"] == 24 and fresh["booths"] == [300, 400, 500],
      "事务结束后新快照可见新设置 24 时段/300,400,500")
check(len(fresh["artifacts"]) == 4, "新快照可见新增藏品(4 件)")

# 恢复默认形态,便于后续断言(此时共 4 件藏品:3 件种子 + 步骤 1 新增)
db.save_settings(12, 2, [200, 200, 200])


# ---------- 2. /api/solve:求解中编辑,保存的必须是旧快照 ----------
solving = threading.Event()
release_solve = threading.Event()


def fake_solve(config, time_limit=10.0):
    solving.set()
    release_solve.wait(5)
    return {
        "status": "optimal",
        "weighted_hours": 99,
        "transitions": 7,
        "wall_time": 10.0,
        "assignment": {str(a["id"]): ["storage"] * config["num_slots"]
                       for a in config["artifacts"]},
        "doses": {str(a["id"]): 0 for a in config["artifacts"]},
        "hours": {str(a["id"]): 0 for a in config["artifacts"]},
    }


main.solve_schedule = fake_solve

saved_pairs = []
orig_save = db.save_schedule


def spy_save(result, snapshot):
    saved_pairs.append((json.loads(json.dumps(result)), json.loads(json.dumps(snapshot))))
    return orig_save(result, snapshot)


db.save_schedule = spy_save
main.db.save_schedule = spy_save

result_holder = {}


def do_solve(holder):
    holder["sched"] = main.solve()


solve_thread = threading.Thread(target=do_solve, args=(result_holder,))
solve_thread.start()
solving.wait(5)
# ---- 求解进行中:另一请求改时段数、照度、藏品、锁定 ----
db.save_settings(36, 2, [900, 100, 200])
arts = db.list_artifacts()
db.add_artifact("求解中途添加", 5000, 9, 1)
db.set_lock(arts[0]["id"], 0, "storage")
# ---- 编辑完成后才让求解返回 ----
release_solve.set()
solve_thread.join()

sched1 = result_holder["sched"]
snap1 = sched1["snapshot"]
res1 = sched1["result"]
check(snap1["num_slots"] == 12,
      f"进行中的求解保存旧快照 12 时段,实际 {snap1['num_slots']}")
check(snap1["booths"] == [200, 200],
      f"进行中的求解保存旧照度 [200,200],实际 {snap1['booths']}")
check(len(snap1["artifacts"]) == 4,
      f"进行中的求解保存旧藏品 4 件,实际 {len(snap1['artifacts'])}")
check(snap1["locks"] == [], f"进行中的求解保存旧锁定(空),实际 {snap1['locks']}")
check(len(res1["assignment"][str(snap1['artifacts'][0]['id'])]) == 12,
      "结果排班长度对应旧快照 12 时段(时间轴不被截断到新值)")
check(res1["doses"][str(snap1['artifacts'][0]['id'])] == 0, "结果剂量字段与旧快照藏品对应")
# 历史接口取回的记录必须与保存时一致
fetched = main.get_schedule(sched1["id"])
check(fetched["snapshot"] == snap1 and fetched["result"]["status"] == "optimal",
      "GET /api/schedules/{id} 取回的快照与结果和保存时一致")


# ---------- 3. 编辑后再次求解:使用并保存新配置 ----------
sched2 = main.solve()
snap2 = sched2["snapshot"]
check(snap2["num_slots"] == 36 and snap2["booths"] == [900, 100],
      f"新求解采用新快照 36 时段/[900,100],实际 {snap2['num_slots']}/{snap2['booths']}")
check(len(snap2["artifacts"]) == 5, f"新求解快照含 5 件藏品,实际 {len(snap2['artifacts'])}")
check(any(l["artifact_id"] == arts[0]["id"] and l["slot"] == 0 and l["location"] == "storage"
          for l in snap2["locks"]), "新求解快照含中途新增的锁定")
check(sched2["id"] != sched1["id"], "两次求解各自生成独立历史记录")


# ---------- 4. 无解 / 超时状态同样带快照保存且可查看 ----------
def infeasible_solve(config, time_limit=10.0):
    return {"status": "infeasible", "weighted_hours": None, "transitions": None,
            "wall_time": 0.01, "assignment": None, "doses": None, "hours": None}


def unknown_solve(config, time_limit=10.0):
    return {"status": "unknown", "weighted_hours": None, "transitions": None,
            "wall_time": 10.0, "assignment": None, "doses": None, "hours": None}


main.solve_schedule = infeasible_solve
sched3 = main.solve()
check(sched3["status"] == "infeasible" and sched3["snapshot"]["num_slots"] == 36,
      "无解状态与当时输入快照一起保存")
check(main.get_schedule(sched3["id"])["status"] == "infeasible", "无解历史可查看")

main.solve_schedule = unknown_solve
sched4 = main.solve()
check(sched4["status"] == "unknown" and sched4["snapshot"]["booths"] == [900, 100],
      "超时状态与当时输入快照一起保存")


# ---------- 5. 求解器异常:保存 error 状态,接口不 500 ----------
def boom(config, time_limit=10.0):
    raise RuntimeError("solver crashed")


main.solve_schedule = boom
sched5 = main.solve()
check(sched5["status"] == "error" and sched5["result"]["assignment"] is None,
      "求解器异常以 error 状态保存")
check(sched5["snapshot"]["num_slots"] == 36, "error 记录同样携带输入快照")
check(sched5["result"]["error"] == "solver crashed", "error 记录附带错误信息")


# ---------- 6. 并发求解:各自保持自己的输入与结果 ----------
starts = [threading.Event() for _ in range(2)]
rels = [threading.Event() for _ in range(2)]
holders = [{}, {}]
snap_before = db.current_config()
order = []
order_lock = threading.Lock()


def multi_solver(config, time_limit=10.0):
    with order_lock:
        idx = len(order)
        order.append(idx)
    starts[idx].set()
    rels[idx].wait(5)
    return {
        "status": "optimal",
        "weighted_hours": 100 + idx,
        "transitions": idx,
        "wall_time": 1.0,
        "assignment": {str(a["id"]): [idx] * config["num_slots"]
                       for a in config["artifacts"]},
        "doses": {str(a["id"]): idx for a in config["artifacts"]},
        "hours": {str(a["id"]): idx for a in config["artifacts"]},
    }


main.solve_schedule = multi_solver
threads = [threading.Thread(target=do_solve, args=(holders[i],)) for i in range(2)]
for th in threads:
    th.start()
for e in starts:
    e.wait(5)
# 两个求解都已拿到快照、正在求解时,再改配置;随后逐个放行
db.save_settings(48, 1, [50, 200, 200])
for r in rels:
    r.set()
for th in threads:
    th.join()

for i in (0, 1):
    s = holders[i]["sched"]
    check(s["snapshot"] == snap_before, f"并发求解 #{i} 保存自己开始时的快照(编辑前)")
    check(s["result"]["weighted_hours"] == 100 + i,
          f"并发求解 #{i} 结果与自身记录对应(weighted_hours={100 + i})")
check(main.get_schedule(holders[0]["sched"]["id"])["snapshot"] == snap_before
      and main.get_schedule(holders[1]["sched"]["id"])["snapshot"] == snap_before,
      "两条并发历史均可查看且快照互不串改")


# ---------- 7. 旧历史记录(仅快照字段)仍可查看 ----------
with db._conn() as c:
    cur = c.execute(
        "INSERT INTO schedules (created_at, status, weighted_hours, transitions, snapshot, result)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("2025-01-01T00:00:00", "optimal", 1, 2,
         json.dumps({"num_slots": 6, "booths": [100],
                     "artifacts": [{"id": 1, "name": "旧藏品", "budget": 10,
                                    "priority": 1, "min_hours": 0, "position": 0}],
                     "locks": []}),
         json.dumps({"status": "optimal", "weighted_hours": 1, "transitions": 2,
                     "wall_time": 0.1,
                     "assignment": {"1": ["storage"] * 6},
                     "doses": {"1": 0}, "hours": {"1": 0}})),
    )
    legacy_id = cur.lastrowid
legacy = main.get_schedule(legacy_id)
check(legacy is not None and legacy["snapshot"]["num_slots"] == 6
      and legacy["result"]["assignment"]["1"] == ["storage"] * 6,
      "旧格式历史记录仍可正常查看")


# ---------- 8. 求解期间编辑接口不被阻塞(求解不持锁) ----------
main.solve_schedule = fake_solve
solving.clear()
release_solve.clear()
th = threading.Thread(target=do_solve, args=(result_holder,))
t0 = time.time()
th.start()
solving.wait(5)
db.save_settings(30, 2, [150, 250, 200])  # 求解进行中写入
dt = time.time() - t0
release_solve.set()
th.join()
check(dt < 2.0, f"求解进行中编辑不被阻塞(耗时 {dt:.2f}s)")
check(result_holder["sched"]["snapshot"]["num_slots"] == 48,
      "该求解快照是其开始时的配置(48 时段),不受随后编辑影响")


print()
if failures:
    print(f"{len(failures)} 项失败")
    sys.exit(1)
print("全部断言通过")
