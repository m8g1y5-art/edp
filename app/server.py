"""HTTP 接口 + 静态页面。启动：python -m app.server"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import batchq, pipeline, plan as P, scan, vlm
from .db import DATA, ROOT, all_settings, event, get_setting, jd, jl, q, set_setting, work_dir, x

app = FastAPI(title="具身数据平台")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def _no_cache(request, call_next):
    """页面和脚本每次都向本机服务确认是否有更新，避免更新后浏览器还用旧版本"""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
_cache = {}  # batch_id -> [rec dict]


def recs_of(bid, fresh=False):
    if fresh or bid not in _cache:
        out = []
        for r in q("SELECT id FROM recordings WHERE batch_id=? ORDER BY id", (bid,)):
            p = work_dir(bid, r["id"]) / "rec.json"
            if p.exists():
                out.append(json.loads(p.read_text(encoding="utf-8")))
        _cache[bid] = out
    return _cache[bid]


def batch(bid):
    b = q("SELECT * FROM batches WHERE id=?", (bid,), one=True)
    if not b:
        raise HTTPException(404, "批次不存在")
    b["info"], b["opts"] = jl(b["info"]), jl(b["opts"])
    p = q("SELECT id,name FROM projects WHERE id=?", (b.get("project_id"),), one=True)
    b["project"] = p or {"id": None, "name": ""}
    return b


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 系统状态 ----
_gpu = {"t": 0, "v": None}


def gpu_info():
    if time.time() - _gpu["t"] < 3:
        return _gpu["v"]
    v = None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode == 0 and r.stdout.strip():
            n, u, mu, mt, tc = [s.strip() for s in r.stdout.strip().splitlines()[0].split(",")]
            v = {"name": n, "util": int(u), "mem_used": int(mu), "mem_total": int(mt), "temp": int(tc)}
    except Exception:
        pass
    _gpu.update(t=time.time(), v=v)
    return v


@app.get("/api/resources")
def res_report():
    """本机资源和调度空位（设置页“运行资源”卡片）"""
    r = pipeline.resources(force=True)
    sl = pipeline.slots()
    runs = []
    for u in pipeline.running_list():
        m = q("SELECT s.label, r.name FROM steps s JOIN recordings r ON r.id=s.rec_id WHERE s.rec_id=? AND s.key=?", (u["rid"], u["key"]), one=True) or {}
        runs.append({"rec": m.get("name") or u["rid"], "label": m.get("label") or u["key"], "res": u["res"],
                     "dev": u["dev"] if isinstance(u["dev"], int) else None, "sec": round(time.time() - u["since"])})
    return {"gpus": r["gpus"], "cores": r["cores"], "slots": {"gpu": {str(k): v for k, v in sl["gpu"].items()}, "vlm": sl["vlm"],
            "cpu": sl["cpu"], "total": sl["total"], "local_vlm": sl["local_vlm"]}, "oom": {str(k): v for k, v in pipeline.OOM.items()},
            "running": runs}


@app.get("/api/sys")
def sys_status():
    du = shutil.disk_usage(DATA)
    cur = pipeline.STATE["current"]
    wait = q("SELECT COUNT(*) n FROM steps WHERE status='wait'", one=True)["n"]
    return {"gpu": gpu_info(), "vlm": vlm.status(), "disk_free": du.free, "disk_total": du.total,
            "data_dir": str(DATA), "current": cur, "running": len(pipeline.RUNNING), "queue": wait, "python": sys.version.split()[0],
            "work_bytes": _dir_size(DATA / "batches")}


def _dir_size(p):
    n = 0
    for d, _, fs in os.walk(p):
        for f in fs:
            try:
                n += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    return n


def _polys(bid, rid):
    """浏览页画轮廓用。老数据没有多边形文件时，从掩码现算一次并存下"""
    wd = work_dir(bid, rid)
    p = wd / "masks_poly.json"
    if p.exists():
        return jl(p.read_text(encoding="utf-8"))
    if (wd / "masks.npz").exists() and (wd / "track.json").exists():
        try:
            from . import objects as OB
            trk = jl((wd / "track.json").read_text(encoding="utf-8"))
            d = {"t": trk["t"], "polys": OB.mask_polygons(trk, pipeline.load_masks(wd))}
            p.write_text(jd(d), encoding="utf-8")
            return d
        except Exception:
            return None
    return None


@app.get("/api/settings")
def get_settings():
    return all_settings()


@app.put("/api/settings")
async def put_settings(req: Request):
    for k, v in (await req.json()).items():
        set_setting(k, v)
    return all_settings()


@app.get("/api/vocab")
def get_vocab():
    from . import objects as OB
    from .db import get_setting
    return {"version": get_setting("vocab_version") or 1, "items": OB.vocab(), "custom": bool(get_setting("vocab"))}


@app.put("/api/vocab")
async def put_vocab(req: Request):
    """保存动作词表。词表变了版本号加一，导出时写进标注文件，旧数据可按版本对照"""
    from .db import get_setting
    b = await req.json()
    if b.get("reset"):
        set_setting("vocab", None)
    else:
        items = []
        seen = set()
        for it in b.get("items") or []:
            v = str(it.get("verb") or "").strip()
            if not v or v in seen:
                continue
            seen.add(v)
            items.append({"verb": v, "def": str(it.get("def") or "").strip(), "ex": str(it.get("ex") or "").strip(),
                          "syn": " ".join(str(it.get("syn") or "").replace("，", " ").replace(",", " ").replace("、", " ").split())})
        if "其他" not in seen:
            items.append({"verb": "其他", "def": "以上都不合适时使用，并在描述里写清楚", "ex": "", "syn": ""})
        if "静止" not in seen:
            items.insert(0, {"verb": "静止", "def": "手没有在操作任何物体，基本不动或不在画面里", "ex": "", "syn": "无操作 等待"})
        set_setting("vocab", items)
    set_setting("vocab_version", int(get_setting("vocab_version") or 1) + 1)
    return get_vocab()


@app.post("/api/vlm/test")
def vlm_test():
    s = vlm.status()
    if not s["model"]:
        return {"ok": False, "msg": s.get("err") or "没有找到视觉模型"}
    import numpy as np, cv2
    img = np.full((240, 320, 3), 235, np.uint8)
    cv2.circle(img, (160, 120), 60, (40, 40, 200), -1)
    try:
        j, raw, sec = vlm.ask(s["model"], '图里是什么形状、什么颜色？只输出 JSON：{"shape":"", "color":""}', [img], timeout=240)
        return {"ok": True, "model": s["model"], "sec": round(sec, 1), "answer": j or raw}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


# ---------------------------------------------------------------- 批次 / 导入 ----
def _batch_row(b):
    b["info"] = jl(b["info"])
    opts = jl(b.pop("opts", None))
    b["skipped"] = len(opts.get("skip") or [])
    st = q("SELECT status, COUNT(*) n FROM recordings WHERE batch_id=? GROUP BY status", (b["id"],))
    b["rec_status"] = {s["status"]: s["n"] for s in st}
    b["n"] = sum(b["rec_status"].values())
    sc = q("""SELECT s.status, COUNT(*) n FROM steps s JOIN recordings r ON r.id=s.rec_id
              WHERE r.batch_id=? AND s.key!='idle' GROUP BY s.status""", (b["id"],))
    b["step_status"] = {s["status"]: s["n"] for s in sc}
    rs = recs_of(b["id"]) if b["status"] not in ("new", "scanning") else []
    b["dur"] = sum(r.get("duration", 0) for r in rs)
    b["issues"] = sum(1 for r in rs if any(i[0] != "info" for i in r["issues"]))
    b["paused"] = b["id"] in pipeline.STATE["paused"]
    return b


_mu_cache = {}


def model_usage(bid):
    """这个批次实际用到的大模型：从每条录制的分段结果和物体清单里统计（不是当前设置）。
    按文件修改时间缓存，列表页每 4 秒刷新时不重复解析"""
    root = DATA / "batches" / bid
    files = []
    if root.is_dir():
        for d in os.scandir(root):
            if d.is_dir():
                for fn in ("segments.json", "objects.json"):
                    p = os.path.join(d.path, fn)
                    try:
                        files.append((p, os.stat(p).st_mtime))
                    except OSError:
                        pass
    sig = hash(tuple(files))
    hit = _mu_cache.get(bid)
    if hit and hit[0] == sig:
        return hit[1]
    use = {}
    for p, _ in files:
        try:
            j = jl(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        if p.endswith("segments.json"):
            for sg in j.get("segments") or []:
                if sg.get("src") == "vlm" and sg.get("model"):
                    use.setdefault(sg["model"], {"m": sg["model"], "segs": 0, "objs": 0})["segs"] += 1
        elif j.get("model"):
            use.setdefault(j["model"], {"m": j["model"], "segs": 0, "objs": 0})["objs"] += 1
    out = sorted(use.values(), key=lambda u: (-u["segs"], -u["objs"]))
    _mu_cache[bid] = (sig, out)
    return out


@app.get("/api/batches")
def list_batches(project: str = None):
    rows = q("SELECT * FROM batches WHERE project_id=? ORDER BY created DESC", (project,)) if project else \
        q("SELECT * FROM batches ORDER BY created DESC")
    out = [_batch_row(b) for b in rows]
    for b in out:
        b["models"] = model_usage(b["id"])
    return out


@app.put("/api/batches/{bid}")
async def edit_batch(bid: str, req: Request):
    """改批次名称、备注，或移到另一个项目"""
    batch(bid)
    body = await req.json()
    for k in ("name", "note", "project_id"):
        if k in body and body[k] is not None:
            if k == "project_id" and not q("SELECT 1 FROM projects WHERE id=?", (body[k],), one=True):
                raise HTTPException(400, "项目不存在")
            x(f"UPDATE batches SET {k}=? WHERE id=?", (str(body[k]).strip(), bid))
    return {"ok": True}


# ---------------------------------------------------------------- 项目 ----
@app.get("/api/projects")
def list_projects():
    out = []
    for p in q("SELECT * FROM projects ORDER BY created"):
        bs = [_batch_row(b) for b in q("SELECT * FROM batches WHERE project_id=?", (p["id"],))]
        p["batches"] = len(bs)
        p["recs"] = sum(b["n"] for b in bs)
        p["done"] = sum(b["rec_status"].get("done", 0) for b in bs)
        p["failed"] = sum(b["rec_status"].get("failed", 0) for b in bs)
        p["running"] = sum(b["rec_status"].get("running", 0) + b["rec_status"].get("queued", 0) for b in bs)
        p["dur"] = sum(b["dur"] or 0 for b in bs)
        p["last"] = max([b["created"] for b in bs] or [p["created"]])
        out.append(p)
    return out


@app.post("/api/projects")
async def new_project(req: Request):
    body = await req.json()
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "项目名称不能为空")
    pid = "p" + time.strftime("%y%m%d") + uuid.uuid4().hex[:4]
    x("INSERT INTO projects(id,name,note,created) VALUES(?,?,?,?)", (pid, name, str(body.get("note") or ""), time.time()))
    return {"id": pid}


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
    if not p:
        raise HTTPException(404, "项目不存在")
    p["batches"] = list_batches(pid)
    return p


@app.put("/api/projects/{pid}")
async def edit_project(pid: str, req: Request):
    body = await req.json()
    for k in ("name", "note"):
        if k in body:
            x(f"UPDATE projects SET {k}=? WHERE id=?", (str(body[k] or "").strip(), pid))
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def del_project(pid: str):
    if q("SELECT 1 FROM batches WHERE project_id=?", (pid,), one=True):
        raise HTTPException(400, "项目里还有批次，先删除或移走这些批次")
    if q("SELECT COUNT(*) n FROM projects", one=True)["n"] <= 1:
        raise HTTPException(400, "至少保留一个项目")
    x("DELETE FROM projects WHERE id=?", (pid,))
    return {"ok": True}


# ---------------------------------------------------------------- 批量接口 ----
@app.get("/api/batchq")
def batchq_info():
    from . import batchq
    return batchq.summary()


@app.post("/api/batchq/flush")
def batchq_flush():
    from . import batchq
    batchq.flush(force=True)
    batchq.poll(force=True)
    return batchq.summary()


@app.post("/api/batchq/poll")
def batchq_poll():
    from . import batchq
    batchq.poll(force=True)
    return batchq.summary()


@app.post("/api/batchq/{jid}/cancel")
def batchq_cancel(jid: str):
    from . import batchq
    try:
        batchq.cancel(jid)
    except Exception as e:
        raise HTTPException(400, str(e))
    batchq.poll(force=True)
    return {"ok": True}


@app.post("/api/batchq/test")
def batchq_test():
    from . import batchq
    if vlm.backend() != "api" or not all_settings().get("api_key"):
        return {"ok": False, "msg": "先把模型来源改成外部接口，并填好 API Key"}
    try:
        r = batchq.test()
        return {"ok": r["status"] == "completed", **r,
                "msg": f"测试任务 {r['status']}，用时 {r['sec']} 秒" + ("" if r["status"] == "completed" else "（90 秒内没完成也正常，批量任务需要排队；能创建成功就说明接口可用）")}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:300]}


# ---------------------------------------------------------------- 任务队列 ----
@app.get("/api/queue")
def queue():
    cur = pipeline.STATE["current"]
    rows = q("""SELECT b.id, b.name, b.prio, b.created, b.project_id, p.name pname,
                SUM(s.status='wait') wait, SUM(s.status='run') run, SUM(s.status='batch') bwait, SUM(s.status IN ('ok','warn')) done, SUM(s.status='fail') fail,
                SUM(s.status!='skip') total
                FROM steps s JOIN recordings r ON r.id=s.rec_id JOIN batches b ON b.id=r.batch_id
                LEFT JOIN projects p ON p.id=b.project_id WHERE s.key!='idle'
                GROUP BY b.id ORDER BY COALESCE(b.prio,0) DESC, b.created""")
    live = None
    if cur:
        m = q("SELECT s.label, r.name, s.progress, s.msg FROM steps s JOIN recordings r ON r.id=s.rec_id WHERE s.rec_id=? AND s.key=?",
              (cur[1], cur[2]), one=True)
        if m:
            live = {"bid": cur[0], "rid": cur[1], "key": cur[2], "label": m["label"], "rec": m["name"], "progress": m["progress"],
                    "msg": m["msg"], "since": round(time.time() - pipeline.STATE.get("since", time.time()))}
    for r in rows:
        r["paused"] = r["id"] in pipeline.STATE["paused"]
    return {"live": live, "batches": rows}


@app.post("/api/batches/{bid}/prio")
async def set_prio(bid: str, req: Request):
    """置顶：排到所有批次前面；取消置顶：回到按导入先后"""
    body = await req.json()
    top = q("SELECT MAX(COALESCE(prio,0)) m FROM batches", one=True)["m"] or 0
    x("UPDATE batches SET prio=? WHERE id=?", (top + 1 if body.get("top") else 0, bid))
    pipeline._wake.set()
    return {"ok": True}


@app.post("/api/batches")
async def create_batch(req: Request):
    body = await req.json()
    bid = time.strftime("%m%d-%H%M%S") + uuid.uuid4().hex[:3]
    mode = body.get("mode", "path")
    if mode == "path":
        src = Path(body["path"].strip().strip('"'))
        if not src.exists() or not src.is_dir():
            raise HTTPException(400, f"找不到文件夹：{src}")
    else:
        src = DATA / "uploads" / bid
        src.mkdir(parents=True, exist_ok=True)
    name = body.get("name") or src.name
    pid = body.get("project_id")
    if not pid or not q("SELECT 1 FROM projects WHERE id=?", (pid,), one=True):
        pid = q("SELECT id FROM projects ORDER BY created LIMIT 1", one=True)["id"]
    x("INSERT INTO batches(id,name,src,mode,created,status,project_id) VALUES(?,?,?,?,?,?,?)",
      (bid, name, str(src), mode, time.time(), "new", pid))
    if mode == "path":
        start_scan(bid)
    return {"id": bid}


@app.post("/api/batches/{bid}/upload")
async def upload(bid: str, files: list[UploadFile] = File(...), paths: str = Form(...)):
    b = batch(bid)
    rels = json.loads(paths)
    root = Path(b["src"])
    for f, rel in zip(files, rels):
        dst = (root / rel).resolve()
        if root.resolve() not in dst.parents:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as out:
            shutil.copyfileobj(f.file, out, 1 << 20)
    return {"ok": True, "n": len(files)}


@app.post("/api/batches/{bid}/scan")
def scan_api(bid: str):
    start_scan(bid)
    return {"ok": True}


def start_scan(bid):
    b = batch(bid)
    x("UPDATE batches SET status='scanning', progress=0, msg='准备' WHERE id=?", (bid,))
    x("DELETE FROM recordings WHERE batch_id=?", (bid,))

    def report(p, m):
        x("UPDATE batches SET progress=?, msg=? WHERE id=?", (p, m, bid))

    def run():
        try:
            recs, info = scan.scan(bid, Path(b["src"]), report)
            for r in recs:
                x("INSERT OR REPLACE INTO recordings(id,batch_id,name,rel,data,status,grade) VALUES(?,?,?,?,?,?,?)",
                  (r["id"], bid, r["name"], r["rel"], jd({"group": r["group"]}), "scanned", P.reach(r, r["profile"])))
            x("UPDATE batches SET status='checked', progress=1, msg='', info=? WHERE id=?", (jd(info), bid))
            _cache.pop(bid, None)
            event(bid, None, "info", f"预检完成：{len(recs)} 条录制，用时 {info['seconds']} 秒")
        except Exception as e:
            import traceback
            x("UPDATE batches SET status='error', msg=? WHERE id=?", (f"{type(e).__name__}: {e}", bid))
            event(bid, None, "bad", "预检失败：" + traceback.format_exc()[-500:])

    threading.Thread(target=run, daemon=True).start()


@app.get("/api/batches/{bid}")
def get_batch(bid: str):
    return batch(bid)


@app.delete("/api/batches/{bid}")
def del_batch(bid: str):
    b = batch(bid)
    for r in q("SELECT id FROM recordings WHERE batch_id=?", (bid,)):
        pipeline.bump(r["id"])  # 这条录制如果正在跑，让它尽快作废退出
        x("DELETE FROM steps WHERE rec_id=?", (r["id"],))
    x("DELETE FROM recordings WHERE batch_id=?", (bid,))
    x("DELETE FROM batches WHERE id=?", (bid,))
    x("DELETE FROM events WHERE batch_id=?", (bid,))
    shutil.rmtree(DATA / "batches" / bid, ignore_errors=True)
    if b["mode"] == "upload":
        shutil.rmtree(b["src"], ignore_errors=True)
    _cache.pop(bid, None)
    return {"ok": True}


# ---------------------------------------------------------------- 预检 ----
@app.get("/api/batches/{bid}/precheck")
def precheck(bid: str):
    b = batch(bid)
    if b["status"] in ("new", "scanning"):
        return {"batch": b, "groups": None}
    recs = recs_of(bid)
    vs = vlm.status()
    groups = P.group_view(recs, b["opts"].get("on", {}), bool(vs["model"]))
    return {"batch": b, "groups": groups, "vlm": {"ok": vs["ok"], "model": vs["model"]},
            "dims": P.DIM_LV, "dim_n": P.DIM_N, "stops": P.STOPS}


@app.get("/api/batches/{bid}/recs")
def batch_recs(bid: str):
    """批次里每条录制的预检结果（一行一条），给预检页的“全部录制”表"""
    b = batch(bid)
    if b["status"] in ("new", "scanning"):
        return {"recs": []}
    skip = set(b["opts"].get("skip") or [])
    st = {r["id"]: r for r in q("SELECT id,status,grade FROM recordings WHERE batch_id=?", (bid,))}
    out = []
    for r in recs_of(bid):
        dep = next((s for s in r["streams"] if s["kind"] == "depth"), None)
        out.append({
            "id": r["id"], "name": r["name"], "rel": r["rel"], "dur": r["duration"], "bytes": r.get("bytes", 0),
            "group": r["group"], "group_name": r["group_name"], "grade": P.reach(r, r["profile"]),
            "blocked": bool(P.blocked(r)), "skipped": r["id"] in skip, "status": (st.get(r["id"]) or {}).get("status"),
            "streams": [s["kind"] for s in r["streams"]], "signals": [s["kind"] for s in r["signals"]],
            "depth": None if dep is None else ("16 位" if dep.get("dtype") == "uint16" else "8 位"),
            "task": (r.get("task") or {}).get("name") or "",
            "issues": [[lv, t] for lv, t, _ in r["issues"]]})
    return {"recs": out}


@app.post("/api/batches/{bid}/skip")
async def set_skip(bid: str, req: Request):
    """跳过 / 恢复处理某些录制：按编号，或按某一类预检问题批量"""
    b = batch(bid)
    body = await req.json()
    ids = set(body.get("rids") or [])
    if body.get("issue"):
        ids |= {r["id"] for r in recs_of(bid) if any(t == body["issue"] for _, t, _ in r["issues"])}
    skip = set(b["opts"].get("skip") or [])
    skip = (skip | ids) if body.get("skip", True) else (skip - ids)
    b["opts"]["skip"] = sorted(skip)
    x("UPDATE batches SET opts=? WHERE id=?", (jd(b["opts"]), bid))
    return {"ok": True, "n": len(ids), "skipped": len(skip)}


@app.post("/api/batches/{bid}/upgrade")
async def upgrade(bid: str, req: Request):
    body = await req.json()
    b = batch(bid)
    on = b["opts"].setdefault("on", {})
    g = on.setdefault(body["group"], {})
    g[body["id"]] = bool(body["on"])
    if not g[body["id"]] and body["id"] == "fov":
        pass
    x("UPDATE batches SET opts=? WHERE id=?", (jd(b["opts"]), bid))
    return {"ok": True}


@app.post("/api/batches/{bid}/repair")
async def repair(bid: str, req: Request):
    body = await req.json()
    recs = recs_of(bid)
    r = next(r for r in recs if r["id"] == body["rec"])
    sug = (r.get("mismatch") or {}).get("suggest")
    if not sug:
        raise HTTPException(400, "没有可用的配对建议")
    r2 = next(x_ for x_ in recs if x_["id"] == sug["rec"])
    ds = next(s for s in r2["streams"] if s["kind"] == "depth" and s["name"] == sug["stream"])
    scan.repair(bid, r, dict(ds))
    x("UPDATE recordings SET data=?, grade=? WHERE id=?", (jd({"group": r["group"]}), P.reach(r, r["profile"]), r["id"]))
    event(bid, r["id"], "ok", f"已把 {r['name']} 的彩色与 {r2['name']}/{ds['name']} 重新配对")
    recs_of(bid, fresh=True)
    return {"ok": True, "undo": {"rec": r["id"]}}


@app.post("/api/batches/{bid}/rescan")
def rescan(bid: str):
    start_scan(bid)
    return {"ok": True}


# ---------------------------------------------------------------- 处理 ----
@app.post("/api/batches/{bid}/start")
async def start(bid: str, req: Request):
    b = batch(bid)
    try:
        body = await req.json()
    except Exception:
        body = {}
    mode = body.get("vlm_mode") or b["opts"].get("vlm_mode") or "realtime"
    if mode == "batch" and (vlm.backend() != "api"):
        raise HTTPException(400, "批量接口只能配合外部接口（阿里云百炼）使用，请先在设置页把模型来源改成外部接口")
    if mode == "batch" and not batchq.supports_batch(get_setting("api_model")):
        mode = "realtime"
        event(bid, None, "warn", f"当前模型 {get_setting('api_model')} 不支持百炼批量接口，已改用实时接口")
    b["opts"]["vlm_mode"] = mode
    x("UPDATE batches SET opts=? WHERE id=?", (jd(b["opts"]), bid))
    vs = vlm.status()
    skip = set(b["opts"].get("skip") or [])
    for r in recs_of(bid):
        batchq.reset_failed(r["id"])
    n = pipeline.enqueue(bid, [r for r in recs_of(bid) if r["id"] not in skip], b["opts"].get("on", {}), bool(vs["model"]))
    pipeline.STATE["paused"].discard(bid)
    return {"ok": True, "n": n}


@app.post("/api/batches/{bid}/pause")
def pause(bid: str):
    pipeline.STATE["paused"].add(bid)
    event(bid, None, "info", "已暂停")
    return {"ok": True}


@app.post("/api/batches/{bid}/resume")
def resume(bid: str):
    pipeline.STATE["paused"].discard(bid)
    pipeline._wake.set()
    event(bid, None, "info", "继续处理")
    return {"ok": True}


@app.post("/api/batches/{bid}/retry")
def retry(bid: str):
    ids = [r["id"] for r in q("SELECT id FROM recordings WHERE batch_id=?", (bid,))]
    for rid in ids:
        batchq.reset_failed(rid, [r["key"] for r in q("SELECT key FROM steps WHERE rec_id=? AND status='fail'", (rid,))])
        x("UPDATE steps SET status='wait', progress=0, msg='' WHERE rec_id=? AND status='fail'", (rid,))
        if q("SELECT 1 FROM steps WHERE rec_id=? AND status='wait'", (rid,)):
            x("UPDATE recordings SET status='queued' WHERE id=?", (rid,))
    x("UPDATE batches SET status='processing' WHERE id=?", (bid,))
    pipeline._wake.set()
    return {"ok": True}


@app.post("/api/recs/{bid}/{rid}/rerun")
async def rerun(bid: str, rid: str, req: Request):
    body = await req.json()
    keys = body.get("keys") or [body["key"]]
    if pipeline.is_running(rid, keys):  # 要重跑的这一步正在跑：让它作废
        pipeline.bump(rid)
    batchq.reset_failed(rid, keys)
    for k in keys:
        x("UPDATE steps SET status='wait', progress=0, msg='' WHERE rec_id=? AND key=?", (rid, k))
    x("UPDATE recordings SET status='queued' WHERE id=?", (rid,))
    x("UPDATE batches SET status='processing' WHERE id=?", (bid,))
    pipeline._wake.set()
    return {"ok": True}


@app.post("/api/recs/{bid}/{rid}/replan")
def replan(bid: str, rid: str):
    """按当前环境（视觉模型、SAM 组件、升级选项）重新生成这条录制的处理计划并全部重跑"""
    b = batch(bid)
    r = next((r for r in recs_of(bid) if r["id"] == rid), None)
    if r is None:
        raise HTTPException(404)
    vs = vlm.status()
    pipeline.enqueue(bid, [r], b["opts"].get("on", {}), bool(vs["model"]))
    return {"ok": True, "vlm": vs.get("model"), "err": vs.get("err")}


@app.get("/api/batches/{bid}/progress")
def progress(bid: str):
    b = batch(bid)
    rows = q("SELECT id,name,rel,status,grade FROM recordings WHERE batch_id=? ORDER BY id", (bid,))
    recs = {r["id"]: r for r in recs_of(bid)}
    cols, out = [], []
    for r in rows:
        st = q("SELECT key,label,mode,status,progress,msg,started,finished FROM steps WHERE rec_id=? ORDER BY ord", (r["id"],))
        for s in st:
            if s["label"] not in cols and s["key"] != "idle":
                cols.append(s["label"])
        rr = recs.get(r["id"], {})
        r.update(steps=st, dur=rr.get("duration", 0), group_name=rr.get("group_name", ""))
        out.append(r)
    order = ["入库", "时间同步", "画面质检", "关键点核查", "估计手部", "手部追踪", "相机轨迹", "冲击事件", "关节检查", "算末端位置",
             "开合时刻", "手指信号", "力信号整理", "物体清单", "物体跟踪", "接触事件", "事件切点", "任务识别", "语义标注", "清单外补找"]
    cols.sort(key=lambda c: order.index(c) if c in order else 99)
    ev = q("SELECT ts,rec_id,level,msg FROM events WHERE batch_id=? ORDER BY id DESC LIMIT 60", (bid,))
    runs = pipeline.running_list()
    mine = [u for u in runs if u["bid"] == bid]
    live = {"rec": mine[0]["rid"], "key": mine[0]["key"], "all": [{"rec": u["rid"], "key": u["key"]} for u in mine]} if mine else None
    other = None
    cur = (runs[0]["bid"], runs[0]["rid"], runs[0]["key"]) if runs else None
    if runs and not mine:  # 后台正在跑别的批次（或者已删除批次的旧任务），这个批次在排队
        ob = q("SELECT name FROM batches WHERE id=?", (cur[0],), one=True)
        lb = q("SELECT label FROM steps WHERE rec_id=? AND key=?", (cur[1], cur[2]), one=True)
        other = {"batch": ob["name"] if ob else None, "step": lb["label"] if lb else cur[2], "deleted": ob is None,
                 "since": round(time.time() - runs[0]["since"])}
    return {"batch": b, "other": other, "cols": cols, "recs": out, "events": ev, "paused": bid in pipeline.STATE["paused"], "live": live}


# ---------------------------------------------------------------- 浏览 ----
def _rj(bid, rid, name):
    p = work_dir(bid, rid) / name
    return jl(p.read_text(encoding="utf-8")) if p.exists() else None


@app.get("/api/recs/{bid}/{rid}")
def rec_detail(bid: str, rid: str):
    r = next((r for r in recs_of(bid) if r["id"] == rid), None)
    if r is None:
        raise HTTPException(404)
    light = {k: v for k, v in r.items() if k not in ("streams", "signals")}
    light["streams"] = [{k: v for k, v in s.items() if k not in ("ts", "files")} for s in r["streams"]]
    light["signals"] = [{k: v for k, v in s.items() if k not in ("data", "ts", "cols")} for s in r["signals"]]
    steps = q("SELECT key,label,mode,status,msg FROM steps WHERE rec_id=? ORDER BY ord", (rid,))
    others = q("SELECT id,name,status,grade FROM recordings WHERE batch_id=? ORDER BY id", (bid,))
    for o in others:
        o.update(_review_flags(bid, o["id"]))
    return {"rec": light, "steps": steps, "ingest": _rj(bid, rid, "ingest.json"), "sync": _rj(bid, rid, "sync.json"),
            "quality": _rj(bid, rid, "quality.json"), "segments": _rj(bid, rid, "segments.json"), "task": _rj(bid, rid, "task.json"),
            "traj": _rj(bid, rid, "traj.json"), "kp": _rj(bid, rid, "kp_verify.json"), "imu": _rj(bid, rid, "imu.json"),
            "grip": _rj(bid, rid, "grip.json"), "objects": _rj(bid, rid, "objects.json"), "track": _rj(bid, rid, "track.json"),
            "contact": _rj(bid, rid, "contact.json"), "has_hands": (work_dir(bid, rid) / "hands2d.json").exists(),
            "grade": P.reach(r, r["profile"]), "others": others}


def _review_flags(bid, rid):
    """浏览页录制列表用：这条录制有几段需要人工复核"""
    seg = _rj(bid, rid, "segments.json") or {}
    task = _rj(bid, rid, "task.json") or {}
    ss = seg.get("segments") or []
    n = sum(1 for s in ss if s.get("review") or s.get("err") or (s.get("t1", 0) > s.get("t0", 0) and not (s.get("verb") or s.get("action"))))
    return {"review": n, "segs": len(ss), "edited": bool(seg.get("edited")), "verdict": task.get("verdict") or ""}


@app.get("/api/recs/{bid}/{rid}/polys")
def polys(bid: str, rid: str):
    """浏览页画物体轮廓：每个跟踪帧、每个物体的多边形（原图像素坐标）"""
    d = _polys(bid, rid)
    if not d:
        raise HTTPException(404)
    return d


@app.post("/api/recs/{bid}/{rid}/objects")
async def edit_objects(bid: str, rid: str, req: Request):
    """人工修改物体清单：新增（在某一时刻框出）、改名、重新框选、删除。
    修改存进 objects_manual.json（重新识别物体清单后会再叠加一次），然后重跑跟踪、接触、切段和语义"""
    from . import objects as OB
    body = await req.json()
    wd = work_dir(bid, rid)
    p = wd / "objects.json"
    if not p.exists():
        raise HTTPException(400, "这条录制还没有物体清单，先处理到“物体清单”这一步")
    mp = wd / "objects_manual.json"
    man = jl(mp.read_text(encoding="utf-8")) if mp.exists() else {"objects": {}}
    mo = man.setdefault("objects", {})
    n = max([int(k[1:]) for k in mo if k.startswith("M") and k[1:].isdigit()] or [0])
    for a in body.get("add") or []:
        n += 1
        mo[f"M{n}"] = {"label": str(a.get("label") or "").strip()[:40] or f"M{n}", "bbox": a["bbox"], "t": float(a.get("t") or 0)}
    for oid, e in (body.get("set") or {}).items():
        cur = mo.setdefault(oid, {})
        if e.get("label"):
            cur["label"] = str(e["label"]).strip()[:40]
        if e.get("bbox"):
            cur["bbox"], cur["t"] = e["bbox"], float(e.get("t") or 0)
            cur.pop("found", None)  # 自动补找的物体被人工重新框过：算人工
    removed = set()
    for oid in body.get("delete") or []:
        if oid[:1] in ("M", "A") and oid in mo:  # 人工加的 / 自动补找的物体删掉 = 撤销那条新增
            del mo[oid]
            removed.add(oid)
        else:
            mo[oid] = {"deleted": True}
    man["updated"] = time.time()
    mp.write_text(jd(man), encoding="utf-8")
    d = jl(p.read_text(encoding="utf-8"))
    d["objects"] = [o for o in OB.apply_manual(d["objects"], man) if o["id"] not in removed]
    p.write_text(jd(d), encoding="utf-8")
    keys = [k for k in ("track", "contact", "cuts", "semantic") if q("SELECT 1 FROM steps WHERE rec_id=? AND key=?", (rid, k), one=True)]
    if pipeline.is_running(rid, keys):
        pipeline.bump(rid)
    batchq.reset_failed(rid, keys)
    for k in keys:
        x("UPDATE steps SET status='wait', progress=0, msg='' WHERE rec_id=? AND key=?", (rid, k))
    x("UPDATE recordings SET status='queued' WHERE id=?", (rid,))
    x("UPDATE batches SET status='processing' WHERE id=?", (bid,))
    nm = next((r["name"] for r in recs_of(bid) if r["id"] == rid), rid)
    event(bid, rid, "info", f"{nm} 物体清单已人工修改（新增 {len(body.get('add') or [])} · 修改 {len(body.get('set') or {})} · 删除 {len(body.get('delete') or [])}），重新跟踪并标注")
    pipeline._wake.set()
    return {"ok": True, "objects": d["objects"], "rerun": keys}


@app.get("/api/recs/{bid}/{rid}/hands2d")
def hands2d(bid: str, rid: str):
    p = work_dir(bid, rid) / "hands2d.json"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/json")


@app.put("/api/recs/{bid}/{rid}/segments")
async def save_segments(bid: str, rid: str, req: Request):
    body = await req.json()
    p = work_dir(bid, rid) / "segments.json"
    old = jl(p.read_text(encoding="utf-8")) if p.exists() else {}
    old["segments"] = body["segments"]
    old["edited"] = time.time()
    # 人工切开 / 合并后，按每段带的 group 编号重排子任务分组（同一组必须连续），时间取所含段的起止
    gs_old, gs = old.get("groups") or [], []
    for i, sgm in enumerate(old["segments"]):
        g = sgm.get("group")
        if gs and g is not None and gs[-1]["_g"] == g:
            gs[-1]["to"] = i + 1
            gs[-1]["t1"] = sgm["t1"]
        else:
            title = gs_old[g]["title"] if isinstance(g, int) and g < len(gs_old) else (sgm.get("description") or sgm.get("action") or "")
            gs.append({"_g": g, "from": i + 1, "to": i + 1, "t0": sgm["t0"], "t1": sgm["t1"], "title": title})
    if gs_old:
        for k, g in enumerate(gs):
            for j in range(g["from"] - 1, g["to"]):
                old["segments"][j]["group"] = k
            g.pop("_g", None)
        old["groups"] = gs
    p.write_text(jd(old), encoding="utf-8")
    if body.get("task") is not None:
        t = _rj(bid, rid, "task.json") or {}
        t["user_name"] = body["task"]
        (work_dir(bid, rid) / "task.json").write_text(jd(t), encoding="utf-8")
    event(bid, rid, "info", f"{rid} 人工修改了分段标注")
    return {"ok": True}


@app.get("/api/media/{bid}/{rid}/{name}")
def media_file(bid: str, rid: str, name: str):
    if name == "rgb":
        ing = _rj(bid, rid, "ingest.json") or {}
        if ing.get("rgb_preview") == "original":
            r = next(r for r in recs_of(bid) if r["id"] == rid)
            s = next(s for s in r["streams"] if s["kind"] == "rgb")
            return FileResponse(s["path"], media_type="video/mp4")
        name = ing.get("rgb_preview") or "preview_rgb.mp4"
    p = (work_dir(bid, rid) / name).resolve()
    if work_dir(bid, rid).resolve() not in p.parents or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------------------------------------------------------- 导出 ----
EXPORTS = DATA / "exports"


@app.get("/api/exports")
def list_exports():
    EXPORTS.mkdir(exist_ok=True)
    out = []
    for d in sorted(EXPORTS.iterdir(), key=lambda p: -p.stat().st_mtime):
        m = d / "manifest.json"
        if m.exists():
            mj = jl(m.read_text(encoding="utf-8"))
            out.append({"name": d.name, "path": str(d), "episodes": len(mj.get("episodes", [])), "created": mj.get("created"),
                        "bytes": _dir_size(d), "status": mj.get("status", "done"), "format": mj.get("format", "edp-v1"),
                        "progress": mj.get("progress"), "error": mj.get("error")})
    return out


@app.post("/api/export")
async def export(req: Request):
    body = await req.json()
    from . import exporter
    name = body.get("name") or time.strftime("dataset_%m%d_%H%M")
    out = EXPORTS / name
    if out.exists():
        raise HTTPException(400, "同名数据集已存在")
    items = []
    for it in body["items"]:
        r = next(r for r in recs_of(it["bid"]) if r["id"] == it["rid"])
        items.append((it["bid"], r))
    opts = body.get("opts", {})
    if opts.get("format", "lerobot") == "lerobot":
        from . import exporter_lerobot
        threading.Thread(target=exporter_lerobot.export, args=(out, items, opts), daemon=True).start()
    else:
        threading.Thread(target=exporter.export, args=(out, items, opts), daemon=True).start()
    return {"ok": True, "path": str(out)}


@app.get("/api/exportable")
def exportable():
    out = []
    for b in q("SELECT b.id, b.name, b.project_id, p.name pname FROM batches b LEFT JOIN projects p ON p.id=b.project_id ORDER BY b.created DESC"):
        recs = {r["id"]: r for r in recs_of(b["id"])}
        for r in q("SELECT id,name,status,grade FROM recordings WHERE batch_id=? AND status IN ('done','failed') ORDER BY id", (b["id"],)):
            rr = recs.get(r["id"], {})
            seg = _rj(b["id"], r["id"], "segments.json") or {}
            task = _rj(b["id"], r["id"], "task.json") or {}
            out.append({"bid": b["id"], "batch": b["name"], "pid": b["project_id"], "project": b["pname"], "rid": r["id"], "name": r["name"], "status": r["status"], "grade": r["grade"],
                        "dur": rr.get("duration", 0), "group_name": rr.get("group_name", ""), "segs": len(seg.get("segments", [])),
                        "edited": bool(seg.get("edited")), "task": task.get("user_name") or (rr.get("task") or {}).get("name") or task.get("name") or ""})
    return out


@app.post("/api/open")
async def open_path(req: Request):
    p = (await req.json())["path"]
    if os.name == "nt":
        os.startfile(p)  # 在资源管理器里打开
        return {"ok": True}
    return {"ok": False, "msg": "仅 Windows 支持"}


@app.post("/api/cleanup")
def cleanup():
    """删除预览视频等可再生成的缓存"""
    n = 0
    for p in (DATA / "batches").rglob("preview_*.mp4"):
        n += p.stat().st_size
        p.unlink()
    return {"freed": n}


# ---------------------------------------------------------------- 精细标注组件（SAM 2）----
SAM_LOG = DATA / "sam_install.log"
_inst = {"running": False}


@app.get("/api/sam")
def sam_info():
    from . import objects
    st = objects.sam_status()
    st["installing"] = _inst["running"]
    st["log"] = SAM_LOG.read_text(encoding="utf-8", errors="ignore")[-4000:] if SAM_LOG.exists() else ""
    return st


@app.post("/api/sam/install")
def sam_install():
    if _inst["running"]:
        return {"ok": True}
    from . import objects

    def run():
        _inst["running"] = True
        with open(SAM_LOG, "w", encoding="utf-8") as log:
            def sh(args):
                log.write("\n$ " + " ".join(args[2:]) + "\n"); log.flush()
                p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, creationflags=0x08000000 if os.name == "nt" else 0)
                return p.wait() == 0
            py = [sys.executable, "-m", "pip", "install"]
            tsing = ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"]
            try:
                ok = objects.sam_status()["installed"] or \
                    sh(py + ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu128"]) or \
                    sh(py + ["torch", "torchvision", "-f", "https://mirrors.aliyun.com/pytorch-wheels/cu128/"] + tsing)
                if ok:
                    ok = sh(py + ["--no-deps", "ultralytics", "ultralytics-thop"] + tsing) and \
                        sh(py + ["polars", "psutil", "matplotlib", "pillow", "requests", "pyyaml"] + tsing)
                if ok:
                    objects.ensure_weights(lambda m: (log.write(m + "\n"), log.flush()))
                    try:
                        from . import detect as DT
                        DT._weights(lambda m: (log.write(m + "\n"), log.flush()))
                    except Exception as e:
                        log.write(f"检测模型下载失败（不影响跟踪，物体清单会退回大模型）：{e}\n"); log.flush()
                    log.write("\n完成。\n" if objects.sam_status()["installed"] else "\n安装后仍无法导入，请把这段日志发给开发者。\n")
                else:
                    log.write("\n安装失败，请把这段日志发给开发者。\n")
            except Exception as e:
                log.write(f"\n出错：{e}\n")
        _inst["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@app.on_event("startup")
def _start():
    from .db import migrate
    migrate()
    pipeline.start_worker()


def main():
    import uvicorn
    port = int(os.environ.get("EDP_PORT", "8765"))
    if "--open" in sys.argv:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
