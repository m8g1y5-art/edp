"""百炼批量接口（Batch）：大模型请求不当场发出，攒成文件统一提交，价格是实时调用的一半，结果最晚在设定时限内返回。

工作方式（“记录—回放”）：
  批量模式下，处理步骤照常执行；每次调用大模型时先查结果缓存：
    有结果  → 直接用；
    没结果  → 把这次请求记下来（请求体存文件），返回空结果让步骤先走完。
  步骤结束时只要有没拿到结果的请求，这一步就记为“等待批量”，后面的步骤不会开始。
  后台管理线程把各录制记下的请求按模型打包成 JSONL，上传、创建批量任务、定时查询；
  结果回来后写进缓存，把对应步骤重新排队。重跑时所有请求都命中缓存，得到和实时调用一样的结果。
  请求的编号由“录制 + 步骤 + 模型 + 提示词 + 图片”算出，同样的请求重跑不会重复花钱。
"""
import hashlib
import json
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from .db import DATA, event, get_setting, jd, jl, q, x

REQ_DIR = DATA / "batch_req"
_ctx = threading.local()
_wake = threading.Event()
_state = {"last_poll": 0.0, "err": ""}


# ------------------------------------------------------------------ 步骤上下文 ----
def begin(bid, rid, key, enabled):
    _ctx.on = bool(enabled)
    _ctx.bid, _ctx.rid, _ctx.key = bid, rid, key
    _ctx.misses = 0


def end():
    n = getattr(_ctx, "misses", 0) if getattr(_ctx, "on", False) else 0
    _ctx.on = False
    _ctx.misses = 0
    return n


def pending():
    """本步到目前为止有几个请求还在等批量结果（等结果时不发依赖它们的后续请求，免得白花一次）"""
    return getattr(_ctx, "misses", 0) if active() else 0


def active():
    return bool(getattr(_ctx, "on", False))


# 百炼上不支持批量推理（Batch）的模型：选了也走实时接口。以百炼文档各模型页“批量推理”一栏为准
NO_BATCH = ("deepseek-v4",)


def supports_batch(model):
    m = str(model or "").strip().lower()
    return bool(m) and not any(m.startswith(p) for p in NO_BATCH)


def batch_mode(bid):
    """这个批次是否走批量接口：处理开始时选的方式，当前用的是外部接口，且当前模型支持批量推理"""
    if (get_setting("vlm_backend") or "ollama") != "api":
        return False
    if not supports_batch(get_setting("api_model")):
        return False
    r = q("SELECT opts FROM batches WHERE id=?", (bid,), one=True)
    return bool(r) and jl(r["opts"]).get("vlm_mode") == "batch"


# ------------------------------------------------------------------ 记录 / 回放 ----
def _body(model, prompt, imgs_b64, json_mode=True):
    if imgs_b64:
        content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in imgs_b64]
        content.append({"type": "text", "text": prompt})
    else:  # 纯文字请求（如汇总）：百炼要求一个批量任务里要么全是多模态、要么全是纯文字
        content = prompt
    body = {"model": model, "temperature": 0.2, "messages": [{"role": "user", "content": content}], "enable_thinking": False}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def ask(model, prompt, images, max_side=None):
    """批量模式下替代 vlm.ask：返回 (json, 原文, 用时)。没有结果时登记请求、返回空结果"""
    from . import vlm
    ms = min(max_side or 768, 768)
    b64 = [vlm._b64(im, ms) for im in images]
    h = hashlib.sha1()
    for part in (_ctx.rid, _ctx.key, model, prompt, str(ms)):
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    for b in b64:
        h.update(hashlib.sha1(b.encode()).digest())
    key = h.hexdigest()
    row = q("SELECT status, result, err, attempts FROM batch_reqs WHERE key=?", (key,), one=True)
    if row and row["status"] == "done":
        txt = row["result"] or ""
        return vlm.parse_json(txt, strict=True) or {}, txt, 0.0
    if row and row["status"] == "failed":
        raise RuntimeError("批量接口返回失败：" + (row["err"] or "")[:160])
    if not row:
        # 单个请求有大小上限（文档写 1 MB）：超了就按同样规则缩小图片，结果是确定的，回放时编号不变
        body = _body(model, prompt, b64)
        for side in (640, 512, 384):
            if len(json.dumps(body)) < 950_000:
                break
            body = _body(model, prompt, [vlm._b64(im, side) for im in images])
        p = REQ_DIR / key[:2] / f"{key}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        x("""INSERT INTO batch_reqs(key, batch_id, rec_id, step_key, model, status, attempts, created, size)
             VALUES(?,?,?,?,?,'pending',0,?,?)""", (key, _ctx.bid, _ctx.rid, _ctx.key, model, time.time(), p.stat().st_size))
        _wake.set()
    _ctx.misses = getattr(_ctx, "misses", 0) + 1
    return {}, "", 0.0


# ------------------------------------------------------------------ 百炼接口 ----
def _base():
    from .vlm import DASHSCOPE_BASE
    return (get_setting("api_base") or DASHSCOPE_BASE).rstrip("/")


def _headers(extra=None):
    h = {"Authorization": "Bearer " + (get_setting("api_key") or "")}
    h.update(extra or {})
    return h


def _http(method, path, body=None, raw=None, ctype=None, timeout=120):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    hdr = _headers({"Content-Type": ctype or "application/json"} if data is not None else None)
    from .vlm import guard
    guard(_base())
    req = urllib.request.Request(_base() + path, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} {msg}")
    return b


def upload(jsonl_bytes, name):
    bd = "----edp" + uuid.uuid4().hex
    parts = [f"--{bd}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch\r\n".encode(),
             f"--{bd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: application/jsonl\r\n\r\n".encode(),
             jsonl_bytes, f"\r\n--{bd}--\r\n".encode()]
    r = _http("POST", "/files", raw=b"".join(parts), ctype=f"multipart/form-data; boundary={bd}", timeout=600)
    return json.loads(r.decode())["id"]


def create(file_id, endpoint="/v1/chat/completions", window="72h"):
    r = _http("POST", "/batches", {"input_file_id": file_id, "endpoint": endpoint, "completion_window": window})
    return json.loads(r.decode())


def get(job_id):
    return json.loads(_http("GET", f"/batches/{job_id}").decode())


def content(file_id):
    return _http("GET", f"/files/{file_id}/content", timeout=600)


def cancel(job_id):
    return json.loads(_http("POST", f"/batches/{job_id}/cancel").decode())


# ------------------------------------------------------------------ 打包提交 ----
MAX_LINES = 50_000          # 每个文件最多 5 万个请求（百炼限制）
MAX_BYTES = 450 * 2 ** 20   # 每个文件不超过 500 MB，留余量


def flush(force=False):
    """把待提交的请求按模型打包提交。force：不等凑满、不等时间，立刻提交"""
    per = int(get_setting("batch_max_reqs") or 5000)
    wait_s = float(get_setting("batch_flush_min") or 10) * 60
    idle = not q("SELECT 1 FROM steps WHERE status IN ('wait','run') LIMIT 1", one=True)
    for m in q("SELECT model, COUNT(*) n, MIN(created) t0 FROM batch_reqs WHERE status='pending' GROUP BY model"):
        if not (force or idle or m["n"] >= per or time.time() - m["t0"] >= wait_s):
            continue
        rows = q("SELECT key, size FROM batch_reqs WHERE status='pending' AND model=? ORDER BY created LIMIT ?", (m["model"], min(per, MAX_LINES)))
        packs = {}  # 百炼要求同一个任务里的请求要么全是多模态、要么全是纯文字，不能混
        for r in rows:
            p = REQ_DIR / r["key"][:2] / f"{r['key']}.json"
            if not p.exists():
                x("UPDATE batch_reqs SET status='failed', err='请求文件丢失' WHERE key=?", (r["key"],))
                continue
            body = json.loads(p.read_text(encoding="utf-8"))
            kind = _kind(body)
            if kind == "text":
                body = _plain(body)
            ln = json.dumps({"custom_id": r["key"], "method": "POST", "url": "/v1/chat/completions", "body": body},
                            ensure_ascii=False).encode("utf-8")
            pk = packs.setdefault(kind, {"lines": [], "keys": [], "total": 0, "full": False})
            if pk["full"] or pk["total"] + len(ln) + 1 > MAX_BYTES:
                pk["full"] = True
                continue
            pk["lines"].append(ln)
            pk["keys"].append(r["key"])
            pk["total"] += len(ln) + 1
        for kind, pk in packs.items():
            if pk["lines"] and not _submit(m["model"], kind, pk["lines"], pk["keys"]):
                return


def _kind(body):
    for msg in body.get("messages") or []:
        c = msg.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") != "text" for p in c):
            return "mm"
    return "text"


def _plain(body):
    """旧版登记的纯文字请求 content 是列表，统一改成字符串"""
    for msg in body.get("messages") or []:
        c = msg.get("content")
        if isinstance(c, list):
            msg["content"] = "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return body


def _submit(model, kind, lines, keys):
    name = f"edp_{time.strftime('%m%d_%H%M%S')}_{kind}_{len(lines)}.jsonl"
    try:
        fid = upload(b"\n".join(lines) + b"\n", name)
        job = create(fid, window=f"{int(get_setting('batch_window_h') or 72)}h")
    except Exception as e:
        _state["err"] = f"提交失败：{e}"
        event(None, None, "bad", f"批量任务提交失败（{len(lines)} 个请求，稍后自动重试）：{str(e)[:160]}")
        return False
    jid = job["id"]
    x("""INSERT INTO batch_jobs(id, model, n, status, created, updated, input_file, done, failed, msg)
         VALUES(?,?,?,?,?,?,?,0,0,'')""", (jid, model, len(lines), job.get("status") or "validating", time.time(), time.time(), fid))
    for k in keys:
        x("UPDATE batch_reqs SET status='submitted', job_id=?, attempts=attempts+1 WHERE key=?", (jid, k))
    _state["err"] = ""
    event(None, None, "info", f"已提交批量任务 {jid[-8:]}：{len(lines)} 个{'图片' if kind == 'mm' else '纯文字'}请求，模型 {model}")
    return True


def reset_failed(rid, keys=None):
    """手动重跑时：把这条录制（这些步骤）最终失败的批量请求清掉，重跑时重新登记、重新提交"""
    sql, args = "SELECT key FROM batch_reqs WHERE rec_id=? AND status='failed'", [rid]
    if keys:
        sql += f" AND step_key IN ({','.join('?' * len(keys))})"
        args += list(keys)
    for r in q(sql, tuple(args)):
        p = REQ_DIR / r["key"][:2] / f"{r['key']}.json"
        if p.exists():
            p.unlink()
        x("DELETE FROM batch_reqs WHERE key=?", (r["key"],))


def _job_err(job):
    """百炼在任务整体失败（多为输入文件校验不过）时给的原因"""
    e = job.get("errors")
    if isinstance(e, dict):
        e = e.get("data") or e.get("message") or e
    if isinstance(e, list):
        e = "；".join(str(d.get("message") or d.get("code") or d) if isinstance(d, dict) else str(d) for d in e[:3])
    return str(e)[:200] if e else ""


# ------------------------------------------------------------------ 查询取回 ----
FINAL = ("completed", "failed", "expired", "cancelled")


def _ingest(job):
    """把结果文件、错误文件写进缓存；没有结果的请求重新排队（最多 3 次），之后记为失败"""
    got = set()
    for fid, ok in ((job.get("output_file_id"), True), (job.get("error_file_id"), False)):
        if not fid:
            continue
        try:
            data = content(fid).decode("utf-8", errors="ignore")
        except Exception as e:
            _state["err"] = f"下载结果失败：{e}"
            return False
        for ln in data.splitlines():
            if not ln.strip():
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            k = o.get("custom_id")
            if not k:
                continue
            got.add(k)
            resp = o.get("response") or {}
            body = resp.get("body") or {}
            txt, err = "", ""
            if resp.get("status_code") == 200 and body.get("choices"):
                c = body["choices"][0]["message"].get("content") or ""
                if isinstance(c, list):
                    c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
                txt = c
            else:
                e = o.get("error") or body.get("error") or {}
                err = (e.get("message") if isinstance(e, dict) else str(e)) or f"HTTP {resp.get('status_code')}"
            if txt:
                x("UPDATE batch_reqs SET status='done', result=?, err='' WHERE key=?", (txt, k))
            else:
                _retry_or_fail(k, err)
    # 文件里没有出现的请求（任务过期、取消时没处理到的）：重交
    for r in q("SELECT key FROM batch_reqs WHERE job_id=? AND status='submitted'", (job["id"],)):
        if r["key"] not in got:
            why = _job_err(job)
            _retry_or_fail(r["key"], "批量任务" + {"expired": "过期", "cancelled": "被取消", "failed": "失败"}.get(job.get("status"), "结束") + "时没有处理到" + (f"（{why}）" if why else ""))
    return True


def _retry_or_fail(k, err):
    r = q("SELECT attempts FROM batch_reqs WHERE key=?", (k,), one=True)
    if r and r["attempts"] < 3:
        x("UPDATE batch_reqs SET status='pending', job_id=NULL, err=? WHERE key=?", (err[:300], k))
    else:
        x("UPDATE batch_reqs SET status='failed', err=? WHERE key=?", (err[:300], k))


def _release_steps():
    """“等待批量”的步骤：它的请求都有了结果（或最终失败）就重新排队"""
    from . import pipeline
    n = 0
    for s in q("SELECT rec_id, key FROM steps WHERE status='batch'"):
        left = q("SELECT COUNT(*) n FROM batch_reqs WHERE rec_id=? AND step_key=? AND status IN ('pending','submitted')",
                 (s["rec_id"], s["key"]), one=True)["n"]
        if not left:
            x("UPDATE steps SET status='wait', progress=0, msg='批量结果已返回，重新执行' WHERE rec_id=? AND key=?", (s["rec_id"], s["key"]))
            x("UPDATE recordings SET status='queued' WHERE id=?", (s["rec_id"],))
            n += 1
    if n:
        pipeline._wake.set()
    return n


def poll(force=False):
    every = 60
    if not force and time.time() - _state["last_poll"] < every:
        return
    _state["last_poll"] = time.time()
    for j in q(f"SELECT id FROM batch_jobs WHERE status NOT IN ({','.join('?' * len(FINAL))}) OR ingested IS NULL OR ingested=0", FINAL):
        try:
            job = get(j["id"])
        except Exception as e:
            _state["err"] = f"查询任务失败：{e}"
            continue
        rc = job.get("request_counts") or {}
        st = job.get("status") or ""
        why = _job_err(job)
        x("UPDATE batch_jobs SET status=?, updated=?, done=?, failed=?, output_file=?, error_file=?, msg=? WHERE id=?",
          (st, time.time(), rc.get("completed") or 0, rc.get("failed") or 0, job.get("output_file_id"), job.get("error_file_id"), why, j["id"]))
        if st in FINAL:
            if _ingest(job):
                x("UPDATE batch_jobs SET ingested=1 WHERE id=?", (j["id"],))
                event(None, None, "ok" if st == "completed" else "warn",
                      f"批量任务 {j['id'][-8:]} {({'completed': '完成', 'failed': '失败', 'expired': '过期', 'cancelled': '已取消'})[st]}：成功 {rc.get('completed') or 0}，失败 {rc.get('failed') or 0}" + (f"。原因：{why}" if why else ""))
    _release_steps()


def manager():
    while True:
        try:
            if (get_setting("vlm_backend") or "ollama") == "api":
                flush()
                poll()
            _release_steps()  # 有的请求可能早已有结果（命中缓存）
        except Exception as e:
            _state["err"] = f"{type(e).__name__}: {e}"
        _wake.wait(30)
        _wake.clear()


def start():
    # 整个任务被百炼拒收（校验不过）而判失败的请求，不是请求本身的问题：程序重启时清掉，相关步骤重跑时重新登记提交
    for r in q("SELECT key FROM batch_reqs WHERE status='failed' AND err LIKE '批量任务失败%'"):
        p = REQ_DIR / r["key"][:2] / f"{r['key']}.json"
        if p.exists():
            p.unlink()
        x("DELETE FROM batch_reqs WHERE key=?", (r["key"],))
    threading.Thread(target=manager, daemon=True).start()


def summary():
    pend = q("SELECT status, COUNT(*) n FROM batch_reqs GROUP BY status")
    jobs = q("SELECT * FROM batch_jobs ORDER BY created DESC LIMIT 50")
    return {"reqs": {r["status"]: r["n"] for r in pend}, "jobs": jobs, "err": _state["err"],
            "waiting_steps": q("SELECT COUNT(*) n FROM steps WHERE status='batch'", one=True)["n"]}


# ------------------------------------------------------------------ 连通测试 ----
def test():
    """用百炼的免费测试模型（batch-test-model）走一遍：上传 → 创建 → 等结果。不产生费用"""
    line = {"custom_id": "edp-test-1", "method": "POST", "url": "/v1/chat/ds-test",
            "body": {"model": "batch-test-model", "messages": [{"role": "user", "content": "你好"}]}}
    fid = upload((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"), "edp_test.jsonl")
    job = create(fid, endpoint="/v1/chat/ds-test", window="24h")
    t0 = time.time()
    while time.time() - t0 < 90:
        job = get(job["id"])
        if job.get("status") in FINAL:
            break
        time.sleep(5)
    return {"id": job["id"], "status": job.get("status"), "sec": round(time.time() - t0)}
