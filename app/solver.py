"""OR-Tools CP-SAT 排班求解器。

模型:
- 每件藏品在每个时段恰好处于一个展位或库房(ExactlyOne)。
- 每个展位每时段最多容纳一件藏品(AtMostOne)。
- 展位剂量 = 照度(lx) x 小时,逐时段累加,不得超过藏品剩余预算(lx*h)。
- 展出小时数不得低于藏品最低展出时数。
- 锁定:指定藏品在指定时段必须处于某展位或库房。
- 跨展位移动:若藏品在相邻两个时段分别处于不同展位则禁止
  (即跨展位移动之间必须间隔至少一小时库房)。

目标(字典序,通过权重合成单个整数目标):
1. 最大化优先级加权的展出小时数;
2. 最小化相邻时段出入库次数(库房状态发生变化的次数)。
"""
from __future__ import annotations

from ortools.sat.python import cp_model

# 字典序权重:出入库次数最多 8 * 71 = 568,取 10000 保证
# 1 单位加权展出小时永远优先于全部出入库次数。
_LEX_WEIGHT = 10000

STATUS_OPTIMAL = "optimal"        # 已证最优
STATUS_FEASIBLE = "feasible"      # 可行但未证最优
STATUS_INFEASIBLE = "infeasible"  # 无解
STATUS_UNKNOWN = "unknown"        # 超时未找到方案
STATUS_ERROR = "error"            # 模型错误


def solve_schedule(config: dict, time_limit: float = 10.0) -> dict:
    """求解排班。

    config = {
        "num_slots": int,
        "booths": [lux, ...],            # 每展位照度
        "artifacts": [{"id", "name", "budget", "priority", "min_hours"}, ...],
        "locks": [{"artifact_id", "slot", "location"}],  # location: "storage" 或展位序号 int
    }
    返回 {"status", "weighted_hours", "transitions", "wall_time",
          "assignment": {artifact_id: [location, ...]},   # 有解时
          "doses": {artifact_id: lxh}, "hours": {artifact_id: h}}
    """
    num_slots = config["num_slots"]
    lux = config["booths"]
    artifacts = config["artifacts"]
    locks = config["locks"]

    T = num_slots
    B = len(lux)
    N = len(artifacts)
    id_of = [a["id"] for a in artifacts]
    index_of = {a["id"]: i for i, a in enumerate(artifacts)}

    model = cp_model.CpModel()

    # x[i,t,b]: 藏品 i 在时段 t 处于展位 b; s[i,t]: 处于库房
    x = {}
    s = {}
    for i in range(N):
        for t in range(T):
            s[i, t] = model.new_bool_var(f"s_{i}_{t}")
            for b in range(B):
                x[i, t, b] = model.new_bool_var(f"x_{i}_{t}_{b}")

    # 每件每时段恰在一个展位或库房
    for i in range(N):
        for t in range(T):
            model.add_exactly_one([s[i, t]] + [x[i, t, b] for b in range(B)])

    # 单件展位:每展位每时段最多一件藏品
    for t in range(T):
        for b in range(B):
            model.add_at_most_one(x[i, t, b] for i in range(N))

    # 剂量预算与最低展出时数
    for i in range(N):
        dose = sum(lux[b] * x[i, t, b] for t in range(T) for b in range(B))
        model.add(dose <= artifacts[i]["budget"])
        hours = sum(x[i, t, b] for t in range(T) for b in range(B))
        model.add(hours >= artifacts[i]["min_hours"])

    # 锁定
    for lk in locks:
        i = index_of[lk["artifact_id"]]
        t = lk["slot"]
        loc = lk["location"]
        if loc == "storage":
            model.add(s[i, t] == 1)
        else:
            model.add(x[i, t, int(loc)] == 1)

    # 跨展位移动须间隔至少一小时库房:
    # 相邻时段不得处于两个不同展位
    for i in range(N):
        for t in range(T - 1):
            for b1 in range(B):
                for b2 in range(B):
                    if b1 != b2:
                        model.add(x[i, t, b1] + x[i, t + 1, b2] <= 1)

    # 出入库次数:相邻时段库房状态发生变化
    z = {}
    for i in range(N):
        for t in range(T - 1):
            z[i, t] = model.new_bool_var(f"z_{i}_{t}")
            model.add(z[i, t] >= s[i, t] - s[i, t + 1])
            model.add(z[i, t] >= s[i, t + 1] - s[i, t])

    weighted_hours = sum(
        artifacts[i]["priority"] * x[i, t, b]
        for i in range(N) for t in range(T) for b in range(B)
    )
    transitions = sum(z[i, t] for i in range(N) for t in range(T - 1))
    model.maximize(_LEX_WEIGHT * weighted_hours - transitions)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8
    status_code = solver.solve(model)

    status_map = {
        cp_model.OPTIMAL: STATUS_OPTIMAL,
        cp_model.FEASIBLE: STATUS_FEASIBLE,
        cp_model.INFEASIBLE: STATUS_INFEASIBLE,
        cp_model.UNKNOWN: STATUS_UNKNOWN,
        cp_model.MODEL_INVALID: STATUS_ERROR,
    }
    result = {
        "status": status_map.get(status_code, STATUS_ERROR),
        "weighted_hours": None,
        "transitions": None,
        "wall_time": round(solver.wall_time, 3),
        "assignment": None,
        "doses": None,
        "hours": None,
    }

    if status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        assignment = {}
        doses = {}
        hours = {}
        for i in range(N):
            locs = []
            dose = 0
            hrs = 0
            for t in range(T):
                loc = "storage"
                for b in range(B):
                    if solver.value(x[i, t, b]):
                        loc = b
                        dose += lux[b]
                        hrs += 1
                        break
                locs.append(loc)
            assignment[str(id_of[i])] = locs
            doses[str(id_of[i])] = dose
            hours[str(id_of[i])] = hrs
        result["assignment"] = assignment
        result["doses"] = doses
        result["hours"] = hours
        result["weighted_hours"] = solver.value(weighted_hours)
        result["transitions"] = solver.value(transitions)

    return result
