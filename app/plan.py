"""能力画像 → 输入质量等级 → 处理计划。前端预检页和后台处理用的是同一份计划。"""

DIM_LV = {
    "A": ["无，只有画面", "人手动作（关键点）", "机器人状态", "机器人动作 + 状态"],
    "T": ["无时间戳", "推算值 / 主机时间", "设备时钟", "硬件同步"],
    "S": ["无", "估算", "相机内参", "内参 + 外参 + 基座"],
    "V": ["无画面", "单视角彩色", "彩色 + 深度", "多视角 + 深度"],
    "L": ["无", "任务名", "任务名 + 分段"],
    "R": ["未知", "已知型号", "模型文件"],
}
DIM_N = {"A": "动作", "T": "时间", "S": "标定", "V": "视觉", "L": "语义", "R": "本体"}
STOPS = [
    {"n": "能标注任务", "d": "只有画面：可以标出在做什么任务、分成哪几步", "need": "有画面"},
    {"n": "有动作轨迹", "d": "能拿到手或机器人每一刻的位置：可以学“是怎么做的”", "need": "手部或机器人的动作数据"},
    {"n": "能训练机器人", "d": "有机器人自己的动作，并且和画面对齐：可以直接用来训练机器人", "need": "机器人动作数据，且与画面时间对齐"},
]
GRADE = {3: "A", 2: "B", 1: "C", 0: "D"}

# 每小时数据大约要几分钟（本机 CPU + 一张消费级显卡的粗估）
COST = {"objects": 0.5, "track": 8, "contact": 1, "ingest": 3, "sync": 1, "quality": 3, "kp_verify": 2, "hand_est": 20, "traj": 0.3, "imu": 0.3, "joints": 0.3,
        "grip": 0.3, "dex": 0.5, "ft": 0.3, "cuts": 0.5, "task": 1.5, "semantic": 6, "refind": 0.5}


_sam = {"t": 0, "v": False}


def sam_ok():
    import time
    if time.time() - _sam["t"] > 30:
        from .objects import sam_status
        _sam.update(t=time.time(), v=sam_status()["installed"])
    return _sam["v"]


def get_setting_safe(k):
    from .db import get_setting
    v = get_setting(k)
    return True if v is None else bool(v)


def fine_ok():
    from .db import get_setting
    v = get_setting("fine_annotation")
    return True if v is None else bool(v)


def upgrades(r):
    p = r["profile"]
    ups = []
    if blocked(r):
        return ups
    has_robot = any(c["k"] in ("arm", "gripper", "dex") for c in r["body"])
    if p["S"] == 0 and p["V"] >= 1:
        ups.append({"id": "fov", "t": "按视场角估算内参", "eff": {"S": 1}, "est": True,
                    "d": "没有标定文件时，按常见 90° 水平视场角估算焦距。不额外耗时；三维精度有限"})
    if p["A"] == 0 and p["V"] >= 1 and not has_robot:
        ups.append({"id": "hand", "t": "估计手部", "eff": {"A": 1}, "est": True,
                    "d": "从画面逐帧检测 21 点手部关键点（2D）；有深度和内参时再抬升到三维"})
    return ups


def blocked(r):
    return bool(r.get("mismatch")) or not r["streams"] or any(i[0] == "block" for i in r["issues"])


def prof(r, on):
    p = dict(r["profile"])
    est = {}
    for u in upgrades(r):
        if on.get(u["id"]):
            for k, v in u["eff"].items():
                if v > p[k]:
                    p[k] = v
                    if u.get("est"):
                        est[k] = True
    return p, est


def reach(r, p):
    if blocked(r):
        return 0
    if p["V"] >= 1 and p["A"] >= 2 and p["T"] >= 1:
        return 3
    if p["V"] >= 1 and p["A"] >= 1:
        return 2
    if p["V"] >= 1:
        return 1
    return 0


def plan(r, on, vlm_ok=True):
    """返回步骤列表：{key, s(名称), k(run/verify/est/gpu/skip), w(说明), ln(泳道), cost(分钟/小时)}"""
    p, est = prof(r, on)
    L = []

    def a(key, s, k, w, ln="pre"):
        L.append({"key": key, "s": s, "k": k, "w": w, "ln": ln, "cost": COST.get(key, 0.5) if k != "skip" else 0})

    if blocked(r):
        a("ingest", "入库", "skip", "彩色与深度疑似来自不同录制，等待重新配对" if r.get("mismatch") else "存在阻断问题，先解决")
        return L
    has_dep = any(s["kind"] == "depth" for s in r["streams"])
    a("ingest", "入库", "run", "建立统一时间轴，抽缩略图，生成可在浏览器播放的预览" + ("（深度上色）" if has_dep else ""))
    if len(r["streams"]) >= 2:
        if p["T"] >= 2:
            a("sync", "时间同步", "verify", "各路都有设备时间戳：按时间戳配对，核查偏移与漂移")
        elif p["T"] == 1:
            a("sync", "时间同步", "est", "时间戳是推算值：用画面运动互相关估计两路之间的偏移，再按时间戳配对")
        else:
            a("sync", "时间同步", "est", "无时间戳：核对帧数和时长，再用画面运动比对")
    a("quality", "画面质检", "run", "清晰度、过暗过曝、画面冻结")
    for c in r["body"]:
        ln = c["n"] + "|" + c["d"]
        if c.get("idle"):
            a("idle", "全程静止", "skip", "通道无变化：不处理", ln)
            continue
        if c["k"] == "human" and not c.get("none"):
            a("kp_verify", "关键点核查", "verify" if p["S"] >= 2 else "skip",
              "已有关键点不重算：投影回画面，与独立检测的 2D 手部点对比，算像素误差" if p["S"] >= 2 else "缺相机内参，无法投影核查", ln)
        elif c["k"] in ("human", "unknown"):
            if on.get("hand"):
                d3 = has_dep and p["S"] >= 1
                a("hand_est", "估计手部", "est", "逐帧检测 21 点手部关键点" + ("，用深度抬升到三维" if d3 else "（2D）"), ln)
            else:
                a("hand_est", "手部追踪", "skip", "画面里没有手部数据；可选“估计手部”", ln)
        elif c["k"] == "pose":
            a("traj", "相机轨迹", "verify", "已有 6 自由度位姿：检查连续性，画出运动轨迹", ln)
        elif c["k"] == "imu":
            pass  # 冲击事件已取消：头戴 IMU 测的是头的晃动，对切分动作帮助不大
        elif c["k"] == "arm":
            a("joints", "关节检查", "run", "关节跳变、卡滞、静止段", ln)
            a("fk", "算末端位置", "skip", "需要机器人模型文件（URDF）；本试用版未包含正运动学", ln)
        elif c["k"] == "gripper":
            a("grip", "开合时刻", "run", "开口归一化，找出每次张开、闭合的时刻", ln)
        elif c["k"] == "dex":
            a("dex", "手指信号", "run", "手指通道归一化，找接触变化", ln)
        elif c["k"] == "ft":
            a("ft", "力信号整理", "run", "去零漂，找接触和受力突变的时刻", ln)
    kinds = {c["k"] for c in r["body"]}
    # 精细标注：大模型框物体 → SAM 2 跟踪 → 指尖与物体接触。需要画面里有手的关键点
    has_hands = any(c["k"] == "human" and not c.get("none") for c in r["body"]) or on.get("hand")
    fine = has_hands and vlm_ok and fine_ok()
    if has_hands:
        if not vlm_ok:
            a("objects", "物体清单", "skip", "未检测到本地视觉模型", "post")
        else:
            a("objects", "物体清单", "gpu", "本地大模型看第一帧，列出可操作的物体并给出边界框，每个物体编号", "post")
        sam = sam_ok()
        a("track", "物体跟踪", "gpu" if (fine and sam) else "skip",
          "SAM 2 按物体框在整段视频里逐帧跟踪轮廓" if sam else "未安装 SAM 组件（设置页可一键安装）", "post")
        a("contact", "接触事件", "run" if (fine and sam) else "skip",
          "指尖落进物体轮廓且深度贴合 → 抓取 / 松开时刻、物体移动距离" if sam else "需要物体跟踪结果", "post")
    src = "手与物体的接触变化（没有接触时退回手腕速度）" if (has_hands and fine and sam_ok()) else "夹爪开合" if "gripper" in kinds else "手腕速度" if (("human" in kinds and p["A"] >= 1) or on.get("hand")) else "画面变化，置信度偏低"
    a("cuts", "事件切点", "run", "候选切点来自：" + src, "post")
    g = "gpu" if vlm_ok else "skip"
    no = "未检测到本地视觉模型（Ollama），跳过"
    a("semantic", "语义标注", g, ("逐段送本地大模型：画面 + 手部放大图 + 物体编号 + 测得的接触事实 + 前文，输出哪只手、动作、物体、去向、是否完成；最后通读全部分段，归纳高层步骤并核对任务名" if (has_hands and fine) else "按切点分段，每段抽帧送本地大模型：动作、物体、是否完成；最后通读全部分段核对任务名") if vlm_ok else no, "post")
    if has_hands:
        ref_on = get_setting_safe("offlist_refind")
        a("refind", "清单外补找", "gpu" if (fine and sam_ok() and vlm_ok and ref_on) else "skip",
          ("语义标注里发现清单外的物体时，在那一段的画面里让大模型框出来、加进物体清单，再重跑跟踪、接触、切点和语义（每条录制一轮）；没有清单外物体时直接跳过"
           if ref_on else "设置里已关闭“清单外补找”") if (fine and sam_ok()) else "需要物体跟踪结果", "post")
    return L


def group_view(recs, on_by_group, vlm_ok):
    """把录制按组汇总，给前端预检页"""
    groups = {}
    for r in recs:
        groups.setdefault(r["group"], []).append(r)
    out = []
    for key, rs in groups.items():
        r0 = rs[0]
        on = on_by_group.get(key, {})
        p, est = prof(r0, on)
        ups = upgrades(r0)
        allon = {u["id"]: True for u in ups}
        hours = sum(r["duration"] for r in rs) / 3600
        steps = plan(r0, on, vlm_ok)
        for s in steps:
            s["min"] = s["cost"] * hours
        issues = {}
        for r in rs:
            for lvl, t, d in r["issues"]:
                k = (lvl, t)
                if k not in issues:
                    issues[k] = [lvl, t, d, 0]
                issues[k][3] += 1
        alts = {}
        for u in ups:
            on2 = dict(on)
            on2[u["id"]] = not on.get(u["id"])
            p2, est2 = prof(r0, on2)
            st2 = plan(r0, on2, vlm_ok)
            for s in st2:
                s["min"] = s["cost"] * hours
            alts[u["id"]] = {"p": p2, "est": est2, "reach": reach(r0, p2), "plan": st2}
        out.append({
            "key": key, "alts": alts, "name": r0["group_name"] + (f" · {r0['name']}" if r0.get("mismatch") else ""),
            "n": len(rs), "dur": sum(r["duration"] for r in rs), "bytes": sum(r["bytes"] for r in rs),
            "profile": r0["profile"], "p": p, "est": est, "reach": reach(r0, p), "max_reach": reach(r0, prof(r0, allon)[0]),
            "body": r0["body"], "issues": sorted(issues.values(), key=lambda i: {"block": 0, "warn": 1, "info": 2}[i[0]]),
            "ups": ups, "on": on, "plan": steps, "blocked": blocked(r0), "mismatch": r0.get("mismatch"),
            "recs": [{"id": r["id"], "name": r["name"], "rel": r["rel"], "dur": r["duration"],
                      "issues": len([i for i in r["issues"] if i[0] != "info"])} for r in rs],
            "thumb_rec": r0["id"],
        })
    out.sort(key=lambda g: (not g["blocked"], -g["reach"], g["name"]))
    return out
