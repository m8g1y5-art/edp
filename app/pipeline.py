"""后台处理：每条录制按计划逐步执行，进度写进数据库，前端轮询。"""
import json
import re
import math
import os
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

from . import hands, media, objects as OB, plan as P, vlm
from .scan import HAND_NAMES
from .db import event, get_setting, jd, jl, q, work_dir, x

STATE = {"paused": set(), "current": None, "stop": False, "gen": {}}


class Cancelled(Exception):
    pass


def bump(rid):
    """这条录制被重新排队（重新处理 / 重跑某步）：版本号加一。
    正在跑的旧步骤会在下一个检查点退出，跑完的结果也不会写回，避免覆盖新的排队状态"""
    STATE["gen"][rid] = STATE["gen"].get(rid, 0) + 1
_wake = threading.Event()


# ============================================================ 工具 ==============
def load_rec(bid, rid):
    return json.loads((work_dir(bid, rid) / "rec.json").read_text(encoding="utf-8"))


def save_json(p, o):
    Path(p).write_text(jd(o), encoding="utf-8")


def rgb_stream(r):
    s = next((s for s in r["streams"] if s["kind"] == "rgb"), None)
    return media.Stream(s) if s else None


def depth_stream(r):
    s = next((s for s in r["streams"] if s["kind"] == "depth"), None)
    return media.Stream(s) if s else None


def depth_check(dep):
    """深度能不能用来算三维距离：要有真实单位，抽 5 帧看有效像素比例和中位距离是否正常。
    返回 (能用, 说明)。不能用时三维步骤退回二维，并在结果里写明原因"""
    if dep is None:
        return False, "没有深度"
    if not dep.metric():
        return False, "深度是 8 位视频，只有相对值"
    meds, valid = [], []
    for i in np.linspace(0, dep.n - 1, 5).astype(int):
        m = media.depth_to_m(dep.read(i))
        if m is None:
            continue
        v = m[m > 0]
        valid.append(v.size / m.size)
        if v.size:
            meds.append(float(np.median(v)))
    if not meds or np.mean(valid) < 0.2:
        return False, f"深度有效像素太少（{np.mean(valid) if valid else 0:.0%}）"
    md = float(np.median(meds))
    if not 0.1 < md < 6:
        return False, f"深度中位数 {md:.2f} m，不像正常距离"
    return True, f"深度正常（中位 {md:.2f} m，有效 {np.mean(valid):.0%}）"


def base_t0(r):
    s = next((s for s in r["streams"] if s["kind"] == "rgb"), None) or r["streams"][0]
    return float(s["ts"][0]) if s.get("ts") else 0.0


class Ctx:
    def __init__(self, bid, rid, key, on):
        self.bid, self.rid, self.key, self.on = bid, rid, key, on
        self.r = load_rec(bid, rid)
        self.wd = work_dir(bid, rid)
        self._last = 0

    def report(self, p, msg=None):
        now = time.time()
        if now - self._last > 0.3 or p >= 1:
            self._last = now
            x("UPDATE steps SET progress=?, msg=COALESCE(?,msg) WHERE rec_id=? AND key=?", (float(p), msg, self.rid, self.key))

    def check_pause(self):
        while self.bid in STATE["paused"] and not STATE["stop"]:
            time.sleep(0.3)
        if STATE["gen"].get(self.rid, 0) != getattr(self, "gen", 0):
            raise Cancelled()


# ============================================================ 各步骤 ============
def step_ingest(c):
    r = c.r
    out = {}
    rgb = rgb_stream(r)
    if rgb:
        for i, k in enumerate(np.linspace(0, rgb.n - 1, 6).astype(int)):
            img = rgb.read(k)
            if img is not None:
                h, w = img.shape[:2]
                media.imwrite(c.wd / f"thumb_{i}.jpg", cv2.resize(img, (320, int(h * 320 / w))))
        c.report(0.1, "缩略图")
        s = rgb.d
        p = Path(s["path"])
        if s["src"] == "video" and s.get("codec") in ("h264", "avc1") and p.suffix.lower() in (".mp4", ".m4v", ".mov"):
            out["rgb_preview"] = "original"
        elif s["src"] == "video":
            c.report(0.15, "转码彩色视频为 H.264")
            media.transcode(p, c.wd / "preview_rgb.mp4")
            out["rgb_preview"] = "preview_rgb.mp4"
        else:
            out["rgb_preview"] = "preview_rgb.mp4"
            _encode_seq(c, rgb, c.wd / "preview_rgb.mp4", lambda im: im, 0.15, 0.5)
        out["rgb_fps"] = rgb.fps
        rgb.close()
    dep = depth_stream(r)
    if dep:
        lo, hi = 0.2, 2.0
        meds = [media.depth_to_m(dep.read(i)) for i in np.linspace(0, dep.n - 1, 5).astype(int)]
        v = np.concatenate([m[m > 0].ravel() for m in meds if m is not None and (m > 0).any()] or [np.array([0.3, 2])])
        lo, hi = float(np.percentile(v, 2)), float(np.percentile(v, 98))
        step = max(1, int(round(dep.fps / 15)))
        fps_out = dep.fps / step
        c.report(0.5, "深度上色并编码预览")
        _encode_seq(c, dep, c.wd / "preview_depth.mp4", lambda im: media.colorize_depth(media.depth_to_m(im), lo, hi), 0.5, 0.98, step, max_w=640)
        out.update(depth_preview="preview_depth.mp4", depth_fps_out=fps_out, depth_step=step, depth_range=[lo, hi])
        dep.close()
    out["t0"] = base_t0(r)
    out["duration"] = r["duration"]
    save_json(c.wd / "ingest.json", out)
    n = sum(s["n"] for s in r["streams"])
    return "ok", f"{n} 帧", out


def _encode_seq(c, st, out, fn, p0, p1, step=1, max_w=1280):
    idx = range(0, st.n, step)
    first = fn(st.read(0))
    h, w = first.shape[:2]
    if w > max_w:
        h, w = int(h * max_w / w), max_w
    enc = media.Encoder(out, w, h, st.fps / step)
    for j, i in enumerate(idx):
        c.check_pause()
        img = st.read(i)
        img = fn(img) if img is not None else np.zeros((h, w, 3), np.uint8)
        enc.write(img)
        if j % 10 == 0:
            c.report(p0 + (p1 - p0) * i / st.n)
    enc.close()


def _energy_series(st, t_rel, kind):
    prev, out = None, []
    t0 = st.ts[0]
    for t in t_rel:
        img = st.read(st.index_at(t0 + t))
        if img is None:
            out.append(0.0)
            continue
        g = media.small_gray(np.clip(media.depth_to_m(img), 0, 4) * 60 if kind == "depth" else img, 200)
        if prev is None:
            out.append(0.0)
        else:
            m = (g > 0) & (prev > 0) if kind == "depth" else np.ones_like(g, bool)
            out.append(float(np.abs(g - prev)[m].mean()) if m.any() else 0.0)
        prev = g
    return np.array(out)


def step_sync(c):
    r = c.r
    rgb, dep = rgb_stream(r), depth_stream(r)
    res = {"depth_t_offset": 0.0}
    if not (rgb and dep):
        return "skip", "只有一路画面", res
    # 1) 时间戳给出的偏移
    ts_off = float(rgb.ts[0] - dep.ts[0]) if (rgb.ts[0] > 1e8 and dep.ts[0] > 1e8) else 0.0
    pair_err = None
    if rgb.ts[0] > 1e8 and dep.ts[0] > 1e8:
        j = np.clip(np.searchsorted(dep.ts, rgb.ts), 1, dep.n - 1)
        near = np.minimum(np.abs(dep.ts[j] - rgb.ts), np.abs(dep.ts[j - 1] - rgb.ts))
        pair_err = float(np.median(near) * 1000)
        res["pair_err_ms"] = pair_err
        res["pair_err_max_ms"] = float(near.max() * 1000)
    # 2) 画面运动互相关：在前 40 秒、以接近原帧率的速率取帧，找最吻合的平移量
    win = min(r["duration"], 40.0)
    rate = min(rgb.fps, dep.fps, 30.0)
    t = np.arange(0, win, 1 / rate)
    c.report(0.1, "提取彩色运动曲线")
    ea = _energy_series(rgb, t, "rgb")
    c.report(0.5, "提取深度运动曲线")
    eb = _energy_series(dep, t + ts_off, "depth")  # 先按时间戳对齐，再找剩余偏移
    rgb.close()
    dep.close()
    max_lag = int(rate * 0.3)
    best, lag, cs = -1, 0, []
    for L in range(-max_lag, max_lag + 1):
        a = ea[max(0, L):len(ea) + min(0, L)]
        b = eb[max(0, -L):len(eb) - max(0, L)]
        cc = float(np.corrcoef(a, b)[0, 1]) if a.std() > 1e-9 and b.std() > 1e-9 else -1
        cs.append(cc)
        if cc > best:
            best, lag = cc, L
    # 抛物线插值到亚帧
    k = lag + max_lag
    sub = 0.0
    if 0 < k < len(cs) - 1:
        y0, y1, y2 = cs[k - 1], cs[k], cs[k + 1]
        den = y0 - 2 * y1 + y2
        sub = 0.5 * (y0 - y2) / den if abs(den) > 1e-9 else 0
    shift = -(lag + sub) / rate  # 深度内容相对彩色的残余偏移（秒）
    noms = any(s.get("sync") == "nominal" for s in r["streams"])
    use_est = best > 0.5 and (noms or not ts_off)
    # 深度预览里的时间 = 彩色时间 + depth_t_offset
    res.update(ts_offset_s=ts_off, corr=round(best, 3), est_shift_ms=round(shift * 1000, 1), used="互相关" if use_est else "时间戳",
               depth_t_offset=ts_off + (shift if use_est else 0.0))
    save_json(c.wd / "sync.json", res)
    msg = f"偏移 {res['depth_t_offset']*1000:+.1f} ms"
    if use_est and abs(shift) > 1 / rate / 2:
        return "warn", f"已校正 {shift*1000:+.0f} ms", res
    if best < 0.3:
        return "warn", f"互相关弱 {best:.2f}", res
    return "ok", msg, res


def step_quality(c):
    r = c.r
    st = rgb_stream(r)
    if not st:
        return "skip", "无彩色画面", {}
    rate = min(5.0, 600 / max(r["duration"], 1))
    t = np.arange(0, r["duration"] + 1e-6, 1 / rate)
    blur, bright, frozen = [], [], []
    prev = None
    for i, tt in enumerate(t):
        c.check_pause()
        img = st.read(st.index_at(st.ts[0] + tt))
        if img is None:
            blur.append(0); bright.append(0); frozen.append(1)
            continue
        g = cv2.cvtColor(cv2.resize(img, (320, int(img.shape[0] * 320 / img.shape[1]))), cv2.COLOR_BGR2GRAY)
        blur.append(float(cv2.Laplacian(g, cv2.CV_64F).var()))
        bright.append(float(g.mean()))
        frozen.append(1 if prev is not None and np.abs(g.astype(int) - prev).mean() < 0.3 else 0)
        prev = g.astype(int)
        if i % 10 == 0:
            c.report(i / len(t))
    st.close()
    blur = np.array(blur)
    med = np.median(blur[blur > 0]) if (blur > 0).any() else 1
    flags = []
    for b, br, fz in zip(blur, bright, frozen):
        f = "blur" if b < 0.35 * med else "dark" if br < 25 else "bright" if br > 235 else "frozen" if fz else ""
        flags.append(f)
    spans = _spans(t, flags)
    res = {"t": t.round(3).tolist(), "sharp": (blur / (2 * med)).clip(0, 1).round(3).tolist(), "bright": np.round(bright, 1).tolist(),
           "flags": flags, "spans": spans}
    save_json(c.wd / "quality.json", res)
    bad = np.mean([f != "" for f in flags])
    nb = np.mean([f == "blur" for f in flags])
    return ("warn" if bad > 0.1 else "ok"), f"模糊 {nb:.1%}" + (f" · 其他 {bad-nb:.0%}" if bad - nb > 0.005 else ""), {"bad": bad, "blur": nb}


def _spans(t, flags):
    out, cur = [], None
    for tt, f in zip(t, flags):
        if f and (cur is None or cur["k"] != f):
            if cur:
                out.append(cur)
            cur = {"k": f, "t0": float(tt), "t1": float(tt)}
        elif f:
            cur["t1"] = float(tt)
        elif cur:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return [s for s in out if s["t1"] - s["t0"] >= 0.2 or True]


# ---------- 手部 ----------
def load_vendor_kp(path):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    fr = obj["frames"]
    items = fr.items() if isinstance(fr, dict) else [(f.get("timestamp", i), f) for i, f in enumerate(fr)]
    out = []
    for k, v in items:
        try:
            t = float(k)
        except Exception:
            continue
        hs = []
        for h in (v.get("hands") or []):
            kp = h.get("keypoints_3d_cam_m") or h.get("keypoints_3d")
            if isinstance(kp, dict):
                pts = [kp.get(n) for n in HAND_NAMES]
                if any(p is None for p in pts):
                    continue
            elif isinstance(kp, list) and len(kp) == 21:
                pts = kp
            else:
                continue
            hs.append({"side": "right" if h.get("is_right") else "left", "conf": h.get("confidence"), "p3": np.array(pts, float)})
        out.append({"t": t, "excluded": bool(v.get("excluded")), "hands": hs})
    out.sort(key=lambda f: f["t"])
    return out


def _K(cam):
    return np.array([[cam["fx"], 0, cam["cx"]], [0, cam["fy"], cam["cy"]], [0, 0, 1]], float)


def project(p3, cam, use_dist=True):
    d = np.array(cam.get("dist") or [0] * 5, float)[:5] if use_dist else np.zeros(5)
    p3 = np.asarray(p3, float)
    ok = p3[:, 2] > 1e-3
    pts, _ = cv2.projectPoints(p3[ok].reshape(-1, 1, 3), np.zeros(3), np.zeros(3), _K(cam), d)
    out = np.full((len(p3), 2), np.nan)
    out[ok] = pts.reshape(-1, 2)
    return out


def step_kp_verify(c):
    r = c.r
    sg = next(s for s in r["signals"] if s["kind"] == "hand_kp")
    frames = load_vendor_kp(sg["file"])
    st = rgb_stream(r)
    c.report(0.05, f"读入 {len(frames)} 帧关键点")
    ft = np.array([f["t"] for f in frames])
    # 候选内参：标定里所有不同的组合，各带/不带畸变
    cands = []
    for cam in r["calib"]["cams"]:
        for ud in (True, False):
            key = (round(cam["fx"], 2), round(cam["cx"], 2), ud)
            if key not in [k for k, _, _ in cands]:
                cands.append((key, cam, ud))
    # 抽 16 帧，独立跑一遍 2D 手部检测作为参照
    idx = [i for i in np.linspace(0, len(frames) - 1, 16).astype(int) if frames[i]["hands"] and not frames[i]["excluded"]]
    errs = {k: [] for k, _, _ in cands}
    for n_, i in enumerate(idx):
        c.check_pause()
        f = frames[i]
        img = st.read(st.index_at(f["t"]))
        det = hands.detect(img)
        for h in f["hands"]:
            m = [d for d in det if d["side"] == h["side"]] or det
            if not m:
                continue
            for key, cam, ud in cands:
                p2 = project(h["p3"], cam, ud)
                best = min(np.nanmean(np.linalg.norm(p2 - d["pts"], axis=1)) for d in m)
                errs[key].append(best)
        c.report(0.1 + 0.6 * n_ / max(len(idx), 1), f"核查第 {n_+1}/{len(idx)} 帧")
    table = []
    for key, cam, ud in cands:
        e = errs[key]
        table.append({"cam": f"{cam['source']} · {cam['name']}", "dist": ud, "fx": cam["fx"], "median_px": float(np.median(e)) if e else None, "n": len(e)})
    valid = [(t, cands[i]) for i, t in enumerate(table) if t["median_px"] is not None]
    if not valid:
        st.close()
        return "warn", "画面中未检出手，无法核查", {"table": table}
    best_t, (key, cam, ud) = min(valid, key=lambda v: v[0]["median_px"])
    # 生成叠加用的逐帧 2D 点（按彩色帧号）
    c.report(0.75, "生成叠加数据")
    over = []
    for i in range(st.n):
        j = int(np.argmin(np.abs(ft - st.ts[i]))) if len(ft) else -1
        if j < 0 or abs(ft[j] - st.ts[i]) > 0.5 / st.fps + 1e-3:
            over.append([])
            continue
        f = frames[j]
        over.append([[h["side"], h["conf"], np.round(project(h["p3"], cam, ud), 1).tolist(), f["excluded"]] for h in f["hands"]])
    st.close()
    save_json(c.wd / "hands2d.json", {"src": "厂商关键点投影", "frames": over})
    wr = {"t": (ft - base_t0(r)).round(4).tolist(),
          "left": [_wrist(f, "left") for f in frames], "right": [_wrist(f, "right") for f in frames],
          "excluded": [f["excluded"] for f in frames]}
    save_json(c.wd / "hands3d.json", wr)
    res = {"table": table, "chosen": best_t, "median_px": best_t["median_px"]}
    save_json(c.wd / "kp_verify.json", res)
    e = best_t["median_px"]
    return ("ok" if e < 25 else "warn"), f"误差 {e:.1f} px", res


def _wrist(f, side):
    h = next((h for h in f["hands"] if h["side"] == side), None)
    return None if h is None or f["excluded"] else np.round(h["p3"][0], 4).tolist()


def fov_cam(w, h, hfov=90):
    fx = w / 2 / math.tan(math.radians(hfov / 2))
    return {"fx": fx, "fy": fx, "cx": w / 2, "cy": h / 2, "dist": None, "name": "估算(90°)", "source": "估算"}


def step_hand_est(c):
    r = c.r
    st = rgb_stream(r)
    dep = depth_stream(r)
    if dep is not None and not depth_check(dep)[0]:
        dep.close()
        dep = None   # 深度不可信就不用它算手的三维位置
    cam = r["calib"]["cams"][0] if r["calib"]["cams"] else (fov_cam(st.d["w"], st.d["h"]) if c.on.get("fov") else None)
    step = max(1, int(round(st.fps / 10)))
    over = [[] for _ in range(st.n)]
    wr = {"t": [], "left": [], "right": [], "excluded": []}
    sync = jl((c.wd / "sync.json").read_text(encoding="utf-8")) if (c.wd / "sync.json").exists() else {}
    off = sync.get("depth_t_offset", 0.0)
    n3 = 0
    for k, i in enumerate(range(0, st.n, step)):
        c.check_pause()
        img = st.read(i)
        det = hands.detect(img)
        d_m = None
        if dep is not None and cam is not None and det:
            t_rel = st.ts[i] - st.ts[0]
            d_m = media.depth_to_m(dep.read(dep.index_at(dep.ts[0] + t_rel + off)))
        rows = []
        w3 = {"left": None, "right": None}
        for d in det:
            rows.append([d["side"], round(d["score"], 2), np.round(d["pts"], 1).tolist(), False])
            if d_m is not None:
                p3 = _lift(d["pts"], d_m, cam, img.shape)
                if p3 is not None:
                    w3[d["side"]] = np.round(p3, 4).tolist()
                    n3 += 1
        for j in range(i, min(i + step, st.n)):
            over[j] = rows
        wr["t"].append(round(float(st.ts[i] - st.ts[0]), 4))
        wr["left"].append(w3["left"] if d_m is not None else _wrist2d(det, "left", img.shape))
        wr["right"].append(w3["right"] if d_m is not None else _wrist2d(det, "right", img.shape))
        wr["excluded"].append(False)
        if k % 5 == 0:
            c.report(i / st.n, f"第 {i}/{st.n} 帧")
    st.close()
    if dep:
        dep.close()
    save_json(c.wd / "hands2d.json", {"src": "画面估计", "frames": over})
    wr["mode"] = "3d" if n3 else "2d"
    save_json(c.wd / "hands3d.json", wr)
    hit = np.mean([bool(o) for o in over[::step]])
    return ("ok" if hit > 0.3 else "warn"), f"检出 {hit:.0%}" + (" · 三维" if n3 else " · 2D"), {"hit": hit}


def _wrist2d(det, side, shape):
    d = next((d for d in det if d["side"] == side), None)
    return None if d is None else [round(d["pts"][0][0] / shape[1], 4), round(d["pts"][0][1] / shape[0], 4), 0]


def _lift(pts, d_m, cam, shape):
    h, w = shape[:2]
    sy, sx = d_m.shape[0] / h, d_m.shape[1] / w
    p = pts[0]
    u, v = int(p[0] * sx), int(p[1] * sy)
    win = d_m[max(0, v - 3):v + 4, max(0, u - 3):u + 4]
    z = win[win > 0]
    if z.size == 0:
        return None
    z = float(np.median(z))
    return np.array([(p[0] - cam["cx"]) * z / cam["fx"], (p[1] - cam["cy"]) * z / cam["fy"], z])


# ---------- 位姿 / IMU / 机器人 ----------
def step_traj(c):
    r = c.r
    sg = next(s for s in r["signals"] if s["kind"] == "pose")
    d = sg["data"]
    ks = list(d.keys())
    pick = lambda suf: np.array(d[next(k for k in ks if k.endswith(suf))])
    xyz = np.stack([pick("tx"), pick("ty"), pick("tz")], 1)
    ts = np.array(sg["ts"]) if sg.get("ts") else np.arange(len(xyz)) / 30
    step = np.linalg.norm(np.diff(xyz, axis=0), axis=1)
    jumps = int((step > 0.05).sum())
    length = float(step.sum())
    k = max(1, len(xyz) // 300)
    res = {"t": (ts[::k] - base_t0(r)).round(3).tolist(), "xyz": xyz[::k].round(4).tolist(), "length_m": length, "jumps": jumps,
           "extent_m": (xyz.max(0) - xyz.min(0)).round(3).tolist()}
    save_json(c.wd / "traj.json", res)
    return ("warn" if jumps else "ok"), f"路程 {length:.2f} m" + (f" · 跳变 {jumps}" if jumps else ""), res


def step_imu(c):
    r = c.r
    sg = next(s for s in r["signals"] if s["kind"] == "imu")
    acc = np.stack([np.array(v) for k, v in sg["data"].items() if "acc" in k][:3], 1)
    ts = np.array(sg["ts"])
    mag = np.linalg.norm(acc, axis=1)
    win = max(3, int(0.5 * sg.get("rate", 200)))
    from numpy.lib.stride_tricks import sliding_window_view
    pad = np.pad(mag, (win // 2, win - win // 2 - 1), mode="edge")
    base = np.median(sliding_window_view(pad, win), axis=1)
    hp = np.abs(mag - base)
    thr = max(1.5, 6 * np.median(np.abs(hp - np.median(hp))))
    ev, last = [], -1e9
    for i in np.argsort(-hp):
        if hp[i] < thr or len(ev) > 60:
            break
        if all(abs(ts[i] - e) > 0.5 for e in ev):
            ev.append(ts[i])
    ev = sorted(float(e - base_t0(r)) for e in ev)
    save_json(c.wd / "imu.json", {"events": ev, "thr": thr})
    return "ok", f"{len(ev)} 个冲击", {"events": ev}


def _robot(r, part):
    for s in r["signals"]:
        if s["kind"] == "robot" and part in s["parts"]:
            return s
    return None


def step_grip(c):
    s = _robot(c.r, "gripper")
    col = s["parts"]["gripper"][0]
    v = np.array(s["data"][col])
    ts = np.array(s["ts"]) if s.get("ts") else np.arange(len(v)) / 30
    lo, hi = np.nanpercentile(v, 2), np.nanpercentile(v, 98)
    n = np.clip((v - lo) / max(hi - lo, 1e-9), 0, 1)
    ev, state = [], n[0] > 0.5
    for t, x_ in zip(ts, n):
        if state and x_ < 0.35:
            state = False
            ev.append({"t": float(t - base_t0(c.r)), "k": "close"})
        elif not state and x_ > 0.65:
            state = True
            ev.append({"t": float(t - base_t0(c.r)), "k": "open"})
    save_json(c.wd / "grip.json", {"events": ev, "range": [float(lo), float(hi)]})
    return "ok", f"{len(ev)} 次开合", {"events": ev}


def step_joints(c):
    s = _robot(c.r, "joints")
    jumps = 0
    for col in s["parts"]["joints"]:
        v = np.array(s["data"][col])
        dv = np.abs(np.diff(v))
        jumps += int((dv > 10 * (np.median(dv) + 1e-6) + 0.2).sum())
    idle = len(s.get("idle") or [])
    return ("warn" if jumps else "ok"), f"跳变 {jumps}" + (f" · 静止 {idle}" if idle else ""), {"jumps": jumps}


def step_simple(c):
    return "ok", "完成", {}



# ---------- 精细标注：物体清单 / 跟踪 / 接触 ----------
def fine_gate(c):
    """大批量时不必每条都做最贵的精细标注：按质量和抽样比例筛选。返回不做的原因（空 = 做）"""
    import hashlib
    if get_setting("fine_scope") == "good" and (c.wd / "quality.json").exists():
        flags = jl((c.wd / "quality.json").read_text(encoding="utf-8")).get("flags") or []
        bad = float(np.mean([f != "" for f in flags])) if flags else 0.0
        if bad > 0.3:
            return f"画面质量差（{bad:.0%} 的时间模糊或过暗），按设置不做精细标注"
    pct = float(get_setting("fine_sample_pct") or 100)
    if pct < 100 and int(hashlib.md5(c.r["id"].encode()).hexdigest(), 16) % 100 >= pct:
        return f"按抽样比例 {pct:.0f}% 未选中，只做基础处理"
    return ""


def task_text(r):
    """任务说明：任务名 + 步骤（厂商的 task_info.json），给物体清单当必找清单"""
    t = r.get("task") or {}
    parts = [t.get("name") or ""] + [str(x) for x in (t.get("steps") or [])]
    return "；".join(p for p in parts if p.strip())[:300]


def reach_filter(c, r, t_rel, shape):
    """用深度去掉手够不着的远处背景：框中间一半区域的深度中位数超过设定距离就不列。
    深度不可用、或设为 0 时不过滤"""
    reach = float(get_setting("inventory_reach_m") or 0)
    if reach <= 0:
        return None, "未启用"
    dep = depth_stream(r)
    ok, note = depth_check(dep) if dep else (False, "没有深度")
    if not ok:
        if dep:
            dep.close()
        return None, note + "，未按距离过滤"
    sync = jl((c.wd / "sync.json").read_text(encoding="utf-8")) if (c.wd / "sync.json").exists() else {}
    dm = media.depth_to_m(dep.read(dep.index_at(dep.ts[0] + t_rel + sync.get("depth_t_offset", 0.0))))
    dep.close()
    if dm is None:
        return None, "深度读取失败，未按距离过滤"
    H, W = shape[:2]
    sy, sx = dm.shape[0] / H, dm.shape[1] / W

    def near(box):
        x1, y1, x2, y2 = box
        cx1, cx2 = x1 + (x2 - x1) * 0.25, x2 - (x2 - x1) * 0.25
        cy1, cy2 = y1 + (y2 - y1) * 0.25, y2 - (y2 - y1) * 0.25
        v = dm[int(cy1 * sy):max(int(cy2 * sy), int(cy1 * sy) + 1), int(cx1 * sx):max(int(cx2 * sx), int(cx1 * sx) + 1)]
        v = v[v > 0]
        return v.size < 10 or float(np.median(v)) <= reach  # 深度缺失的不冤枉

    return near, f"去掉 {reach:g} 米以外的背景"


def step_objects(c):
    r = c.r
    why = fine_gate(c)
    if why:
        for f in ("objects.json", "track.json", "masks.npz", "contact.json"):
            if (c.wd / f).exists():
                (c.wd / f).unlink()
        return "skip", why, {}
    model, s = vlm.need_model()
    if not model:
        return "skip", "Ollama 里没有视觉模型：" + "、".join(m["name"] for m in s["models"]), {}
    st = rgb_stream(r)
    fi = st.index_at(st.ts[0] + min(0.2, r["duration"] / 10))
    img = st.read(fi)
    t_img = float(st.ts[fi] - st.ts[0])
    st.close()
    near_fn, reach_note = reach_filter(c, r, t_img, img.shape)
    t0 = time.time()
    info, raw, objs = {}, "", None
    from . import detect as DT
    if (get_setting("inventory_mode") or "detector") == "detector":
        if DT.available():
            c.report(0.2, "大模型列名词 → 检测模型画框 → 大模型起名")
            try:
                objs, info = DT.inventory(s["model"], img)
            except Exception as e:
                info = {"mode": "检测模型", "err": str(e)}
                objs = None
        else:
            info = {"mode": "检测模型", "err": "检测组件未安装（设置页“精细标注”里一键安装）"}
    if objs is None:  # 纯大模型方案（或检测方案失败时退回）
        c.report(0.3, f"{s['model']} 识别物体")
        t1 = time.time()
        objs, raw, inv = OB.inventory(s["model"], img, zoom=get_setting("inventory_zoom") is not False,
                                       max_n=int(get_setting("inventory_max") or 20), task=task_text(r), keep=near_fn)
        info = dict(info, fallback=bool(info), mode2="大模型", vlm_sec=round(time.time() - t1, 1), **(inv or {}), reach=reach_note)
    sec = time.time() - t0
    man_p = c.wd / "objects_manual.json"
    if man_p.exists() and objs is not None:  # 人工改过的物体清单：叠加在自动结果上
        objs = OB.apply_manual(objs, jl(man_p.read_text(encoding="utf-8")))
    media.imwrite(c.wd / "objects.jpg", OB.draw_objects(img, [o for o in (objs or []) if not o.get("t")]))
    save_json(c.wd / "objects.json", {"objects": objs, "model": s["model"], "raw": raw, "sec": round(sec, 1), "info": info,
                                      "size": [img.shape[1], img.shape[0]]})
    how = "检测模型" if not info.get("fallback") and info.get("mode") == "检测模型" and "err" not in info else "大模型"
    tail = f" · {how} · {sec:.0f} 秒" + (f"（检测方案失败，已退回大模型：{info['err'][:60]}）" if info.get("err") else "")
    if info.get("dropped_far"):
        tail += f" · 去掉远处背景 {info['dropped_far']} 个"
    if info.get("task_objects"):
        tail += f" · 任务物体找到 {len(info['task_found'])}/{len(info['task_objects'])}"
        if info.get("task_missing"):
            tail += "（没找到：" + "、".join(info["task_missing"][:3]) + "）"
    if not objs:
        return "warn", "没有识别到物体" + tail, {"raw": raw[:300], "info": info}
    return "ok", f"{len(objs)} 个：" + "、".join(o["label"] for o in objs[:4]) + ("…" if len(objs) > 4 else "") + tail, {"n": len(objs), "info": info}


def step_track(c):
    r = c.r
    oj = c.wd / "objects.json"
    if not oj.exists() or not jl(oj.read_text(encoding="utf-8"))["objects"]:
        return "skip", "没有物体清单", {}
    if not OB.sam_status()["installed"]:
        return "skip", "未安装 SAM 组件（设置页可一键安装）", {}
    objs = jl(oj.read_text(encoding="utf-8"))["objects"]
    dev = getattr(c, "dev", None)
    model = vlm.status().get("model") if dev in (None, 0) else None
    if model:
        vlm.unload(model)  # 先把大模型移出显存（本机大模型在第 0 张卡上；跑在别的卡上时不用动它）
    s = next(x for x in r["streams"] if x["kind"] == "rgb")
    ing = jl((c.wd / "ingest.json").read_text(encoding="utf-8"))
    src = s["path"] if ing.get("rgb_preview") == "original" else str(c.wd / ing["rgb_preview"])
    fps = float(os.environ.get("EDP_TRACK_FPS") or get_setting("track_fps") or 5)
    trk, masks = OB.track(src, objs, s["w"], c.report, fps=fps, mask_w=int(get_setting("mask_width") or 640), device=dev, tag=c.rid)
    save_json(c.wd / "track.json", trk)
    np.savez_compressed(c.wd / "masks.npz", m=np.packbits(masks, axis=-1), shape=np.array(masks.shape))
    save_json(c.wd / "masks_poly.json", {"t": trk["t"], "polys": OB.mask_polygons(trk, masks)})
    st = {o["id"]: min(len(trk["t"]) - 1, int(round(float(o.get("t") or 0) * fps))) for o in objs}  # 人工中途补的物体只看它出现之后
    lost = [o["id"] for o in trk["objects"] if sum(1 for b in o["bbox"][st.get(o["id"], 0):] if b) < 0.5 * (len(trk["t"]) - st.get(o["id"], 0))]
    return ("warn" if lost else "ok"), f"{len(objs)} 个物体 · {len(trk['t'])} 帧" + (f" · 常丢失 {','.join(lost)}" if lost else ""), {"lost": lost}


def load_masks(wd):
    z = np.load(wd / "masks.npz")
    shp = tuple(z["shape"])
    return np.unpackbits(z["m"], axis=-1)[..., :shp[-1]].astype(bool).reshape(shp)


def step_contact(c):
    r = c.r
    if not (c.wd / "track.json").exists():
        return "skip", "没有物体跟踪结果", {}
    if not (c.wd / "hands2d.json").exists():
        return "skip", "没有手部关键点", {}
    trk = jl((c.wd / "track.json").read_text(encoding="utf-8"))
    masks = load_masks(c.wd)
    hf = jl((c.wd / "hands2d.json").read_text(encoding="utf-8"))["frames"]
    rgb = next(x for x in r["streams"] if x["kind"] == "rgb")
    dep = depth_stream(r)
    sync = jl((c.wd / "sync.json").read_text(encoding="utf-8")) if (c.wd / "sync.json").exists() else {}
    off = sync.get("depth_t_offset", 0.0)
    kp = jl((c.wd / "kp_verify.json").read_text(encoding="utf-8")) if (c.wd / "kp_verify.json").exists() else {}
    cam = next((x for x in r["calib"]["cams"] if kp.get("chosen") and abs(x["fx"] - kp["chosen"]["fx"]) < 1e-3), None) \
        or (r["calib"]["cams"][0] if r["calib"]["cams"] else None)
    depth_fn = None
    dep_ok, dep_note = depth_check(dep)
    if dep_ok:
        def depth_fn(t, _d=dep):
            return media.depth_to_m(_d.read(_d.index_at(_d.ts[0] + t + off)))
    W, H = trk["src_size"]
    tips3d, tip_note = vendor_tips(c, r, hf, depth_fn)
    st = rgb_stream(r)
    def frame_fn(t, _s=st):
        return _s.read(_s.index_at(_s.ts[0] + t))

    def run_contacts():
        c.report(0.1, "计算物体三维位置")
        obj3d = OB.objects_3d(trk, masks, depth_fn, cam, W, H) if (depth_fn and cam) else None
        if obj3d is not None:
            save_json(c.wd / "objects_3d.json", {"t": trk["t"], "frame": "相机坐标系（米）：x 向右、y 向下、z 向前",
                                                 "objects": {o["id"]: obj3d[k] for k, o in enumerate(trk["objects"])}})
        c.report(0.2, "计算指尖与物体的接触")
        return OB.contacts(trk, masks, hf, rgb.get("fps") or 30, depth_fn, cam, gap=float(get_setting("contact_gap") or 0.8), frame_fn=frame_fn, obj3d=obj3d,
                           min_dur=float(get_setting("contact_min") or 0.4), tool_min=float(get_setting("tool_min_s") if get_setting("tool_min_s") is not None else 4.0),
                           tips3d=tips3d, dist_thr=float(get_setting("contact_dist_cm") or 2.0) / 100,
                           mode3d=get_setting("contact_mode") != "2d",
                           edge_tol=float(get_setting("edge_tol_cm") or 1.0) / 100)

    ev = run_contacts()
    st.close()
    if dep:
        dep.close()
    save_json(c.wd / "contact.json", {"events": ev, "method": ev[0]["method"] if ev else "", "tips": tip_note})
    mv = sum(1 for e in ev if e["moved"])
    is3d = bool(depth_fn and cam) and get_setting("contact_mode") != "2d"
    meth = "三维距离" if is3d else "二维轮廓"
    warn = dep is not None and not dep_ok and get_setting("contact_mode") != "2d"
    return ("warn" if warn else "ok"), f"{len(ev)} 次接触 · {mv} 次移动物体 · {meth}" + (f"（{tip_note}）" if is3d else "") + \
        (f" · {dep_note}，已改用二维" if warn else ""), {"n": len(ev), "depth": dep_note}


def vendor_tips(c, r, hf, depth_fn):
    """厂商三维关键点 → 按彩色帧号对齐的指尖三维位置 [{side: [5 个指尖 xyz]}]。
    先抽样核对：厂商指尖的深度和深度图在同一位置的读数是否一致；偏差大就不用（改用深度图反投影）"""
    sg = next((s for s in r["signals"] if s["kind"] == "hand_kp"), None)
    if sg is None:
        return None, "指尖位置来自深度图"
    try:
        frames = load_vendor_kp(sg["file"])
    except Exception:
        return None, "厂商关键点读取失败，指尖位置来自深度图"
    st = rgb_stream(r)
    ts, fps = st.ts, st.fps
    st.close()
    ft = np.array([f["t"] for f in frames])
    if not len(ft):
        return None, "指尖位置来自深度图"
    tips = []
    for i in range(len(ts)):
        j = int(np.argmin(np.abs(ft - ts[i])))
        if abs(ft[j] - ts[i]) > 0.5 / fps + 1e-3 or frames[j]["excluded"]:
            tips.append(None)
            continue
        tips.append({h["side"]: [h["p3"][k].tolist() for k in OB.FINGERTIPS] for h in frames[j]["hands"]})
    if depth_fn is None:
        return tips, "指尖位置来自厂商三维关键点"
    # 抽样核对深度一致性
    rgb = next(x for x in r["streams"] if x["kind"] == "rgb")
    W, Hh = rgb.get("w") or 1280, rgb.get("h") or 720
    diffs = []
    t0 = ts[0]
    for i in np.linspace(0, len(ts) - 1, 60).astype(int):
        if not tips[i] or i >= len(hf) or not hf[i]:
            continue
        dm = depth_fn(ts[i] - t0)
        if dm is None:
            continue
        dh, dw = dm.shape
        for side, conf, pts, ex in hf[i]:
            if ex or side not in tips[i]:
                continue
            for q2, q3 in zip([pts[k] for k in OB.FINGERTIPS], tips[i][side]):
                if q2 is None or q2[0] is None or q3 is None:
                    continue
                u, v = int(q2[0] * dw / W), int(q2[1] * dh / Hh)
                if 0 <= u < dw and 0 <= v < dh:
                    win = dm[max(0, v - 2):v + 3, max(0, u - 2):u + 3]
                    z = win[win > 0]
                    if z.size:
                        diffs.append(float(z.min()) - q3[2])
    if len(diffs) < 20:
        return tips, "指尖位置来自厂商三维关键点（深度核对样本不足）"
    d = float(np.median(diffs))
    mad = float(np.median(np.abs(np.array(diffs) - d)))
    if abs(d) > 0.05 or mad > 0.03:
        return None, f"厂商关键点和深度图对不上（相差 {d*100:.1f} cm），指尖位置改用深度图"
    if abs(d) > 0.01:  # 系统性偏差：沿视线方向把指尖整体挪到和深度图一致
        for fr in tips:
            for side in (fr or {}):
                fr[side] = [None if q is None else (np.array(q) * (q[2] + d) / q[2]).tolist() for q in fr[side]]
    return tips, f"指尖位置来自厂商三维关键点，与深度图相差 {d*100:.1f} cm"

# ---------- 切点 / 语义 ----------
def step_cuts(c):
    r = c.r
    dur = r["duration"]
    cj = c.wd / "contact.json"
    if cj.exists():
        ev = jl(cj.read_text(encoding="utf-8"))["events"]
        if ev:
            segs = OB.segments_from_contacts(ev, dur, min_len=float(get_setting("min_seg") or 1.0))
            for sg in segs:
                sg.update(action="", object="", success=None, conf=None, src="auto")
            save_json(c.wd / "segments.json", {"segments": segs, "sources": ["接触事件"], "mode": "fine"})
            return "ok", f"{len(segs)} 段（按接触切分）", {"n": len(segs), "sources": ["接触事件"]}
    cands = []
    srcs = []
    h3 = c.wd / "hands3d.json"
    if h3.exists():
        d = jl(h3.read_text(encoding="utf-8"))
        t = np.array(d["t"])
        if len(t) > 10:
            sp = np.zeros(len(t))
            for side in ("left", "right"):
                P = np.array([p if p is not None else [np.nan] * 3 for p in d[side]], float)
                v = np.linalg.norm(np.diff(P, axis=0), axis=1) / np.maximum(np.diff(t), 1e-3)
                v = np.concatenate([[0], np.nan_to_num(v)])
                sp += v
            k = max(1, int(len(t) / max(dur, 1) * 0.3))
            sm = np.convolve(sp, np.ones(2 * k + 1) / (2 * k + 1), mode="same")
            thr = np.percentile(sm, 35)
            for i in range(1, len(sm) - 1):
                if sm[i] <= sm[i - 1] and sm[i] <= sm[i + 1] and sm[i] < thr:
                    cands.append((float(t[i]), "手速低点", float(thr - sm[i])))
            srcs.append("手腕速度")
    for fn, label in (("grip.json", "夹爪"), ("imu.json", "IMU 冲击")):
        p = c.wd / fn
        if p.exists():
            for e in jl(p.read_text(encoding="utf-8"))["events"]:
                t_ = e["t"] if isinstance(e, dict) else e
                cands.append((float(t_), label, 1.0))
            srcs.append(label)
    if not cands:  # 退回画面变化
        st = rgb_stream(r)
        t = np.arange(0, dur, 0.25)
        e = _energy_series(st, t, "rgb")
        st.close()
        sm = np.convolve(e, np.ones(5) / 5, mode="same")
        thr = np.percentile(sm, 30)
        for i in range(1, len(sm) - 1):
            if sm[i] <= sm[i - 1] and sm[i] <= sm[i + 1] and sm[i] < thr:
                cands.append((float(t[i]), "画面静止点", float(thr - sm[i])))
        srcs.append("画面变化")
    # 合并相近切点，限制最短段长与段数
    cands.sort()
    merged = []
    for t_, s_, w_ in cands:
        if 0.8 < t_ < dur - 0.8:
            if merged and t_ - merged[-1][0] < 1.2:
                if w_ > merged[-1][2]:
                    merged[-1] = (t_, s_, w_)
            else:
                merged.append((t_, s_, w_))
    maxn = int(get_setting("max_segments") or 8)
    while len(merged) + 1 > maxn:
        merged.remove(min(merged, key=lambda m: m[2]))
    bounds = [0.0] + [m[0] for m in merged] + [float(dur)]
    segs = [{"t0": round(bounds[i], 3), "t1": round(bounds[i + 1], 3), "action": "", "object": "", "success": None,
             "conf": None, "src": "auto", "cut": merged[i - 1][1] if i else "开始"} for i in range(len(bounds) - 1)]
    save_json(c.wd / "segments.json", {"segments": segs, "sources": srcs})
    return "ok", f"{len(merged)} 个切点", {"n": len(merged), "sources": srcs}


def sample_times(t0, t1):
    """按片段长度决定取几帧：每秒 vlm_fps 帧，最少 4 帧，最多 vlm_max_frames 帧；取每个小区间的中点"""
    fps = float(get_setting("vlm_fps") or 2)
    mx = int(get_setting("vlm_max_frames") or 12)
    k = int(np.clip(round((t1 - t0) * fps), 4, max(4, mx)))
    return t0 + (np.arange(k) + 0.5) * (t1 - t0) / k


def key_times(t0, t1, facts):
    """按事件取帧：抓住、松手的瞬间一定要有，其余按时长均匀补齐"""
    fps = float(get_setting("vlm_fps") or 1)
    mx = int(get_setting("vlm_max_frames") or 6)
    k = int(np.clip(round((t1 - t0) * fps), 4, max(4, mx)))
    keys = []
    for e in facts or []:
        for t in (e["t0"] + 0.1, e["t1"] - 0.1):
            if t0 + 0.05 < t < t1 - 0.05:
                keys.append(t)
    keys = sorted(set(round(t, 2) for t in keys))[:k]
    out = list(keys)
    for t in t0 + (np.arange(k) + 0.5) * (t1 - t0) / k:  # 均匀补齐，避开已有关键帧
        if len(out) >= k:
            break
        if all(abs(t - x) > 0.15 for x in out):
            out.append(float(t))
    return np.array(sorted(out))


def stamp(img, t):
    """在画面左下角印上时间（秒），让模型知道每帧对应的时刻"""
    out = img.copy()
    h = out.shape[0]
    s = max(0.6, h / 600)
    txt = f"{t:.1f}s"
    (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, s, 2)
    cv2.rectangle(out, (0, h - th - int(14 * s)), (tw + int(14 * s), h), (0, 0, 0), -1)
    cv2.putText(out, txt, (int(7 * s), h - int(7 * s)), cv2.FONT_HERSHEY_SIMPLEX, s, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def frame_size(k):
    """帧越多每帧越小，控制送给模型的总量（显存和速度）"""
    return 768 if k <= 6 else 640 if k <= 10 else 512


def _frames_at(r, times):
    st = rgb_stream(r)
    out = [st.read(st.index_at(st.ts[0] + t)) for t in times]
    st.close()
    return [f for f in out if f is not None]


def step_task(c):
    r = c.r
    model, s = vlm.need_model()
    if not model:
        return "skip", "Ollama 里没有视觉模型：" + "、".join(m["name"] for m in s["models"]), {}
    dur = r["duration"]
    imgs = _frames_at(r, np.linspace(0.05, 0.95, 6) * dur)
    name = (r.get("task") or {}).get("name")
    c.report(0.2, f"{s['model']} 看 {len(imgs)} 帧")
    if name:
        j, raw, sec = vlm.ask(s["model"], vlm.VERIFY_PROMPT.format(n=len(imgs), name=name), imgs)
        res = {"name": name, "consistent": j.get("consistent"), "seen": j.get("task_seen"), "conf": j.get("confidence"), "sec": sec, "raw": raw, "model": s["model"]}
        save_json(c.wd / "task.json", res)
        ok = j.get("consistent") is not False
        return ("ok" if ok else "warn"), (f"一致 · {j.get('task_seen','')}" if ok else f"不一致：{j.get('task_seen','')}"), res
    j, raw, sec = vlm.ask(s["model"], vlm.TASK_PROMPT.format(n=len(imgs)), imgs)
    res = {"name": j.get("task") or "", "conf": j.get("confidence"), "sec": sec, "raw": raw, "model": s["model"], "generated": True}
    save_json(c.wd / "task.json", res)
    return ("ok" if res["name"] else "warn"), res["name"] or "模型未给出", res


def check_groups(gs, segs):
    """模型给的子任务分组：只能按顺序合并已有的段，不能跳号、重叠，也不能改切点时间。
    不合规的部分退回“一段一组”，时间一律取所含原子段的起止"""
    n = len(segs)
    out, nxt = [], 1
    for g in gs if isinstance(gs, list) else []:
        try:
            a, b = int(g.get("from")), int(g.get("to"))
        except Exception:
            continue
        if a != nxt or b < a or b > n:
            continue
        out.append({"from": a, "to": b, "title": str(g.get("title") or "").strip()[:40]})
        nxt = b + 1
    while nxt <= n:  # 没覆盖到的段：各自成组，标题沿用段描述
        s = segs[nxt - 1]
        out.append({"from": nxt, "to": nxt, "title": s.get("description") or s.get("action") or ""})
        nxt += 1
    for g in out:
        g["t0"], g["t1"] = segs[g["from"] - 1]["t0"], segs[g["to"] - 1]["t1"]
        if not g["title"]:
            g["title"] = segs[g["from"] - 1].get("description") or segs[g["from"] - 1].get("action") or ""
    return out


def summarize_and_check(c, model, seg, p, timeout=180):
    """语义标注最后一步：通读全部分段 → 高层步骤、任务指令、是否完成；有登记任务名时核对一致性（代替原来单独的“任务识别”）"""
    r = c.r
    from . import batchq
    if batchq.pending():  # 批量模式：分段结果还没回来，先不做通读归纳（它依赖分段结果），下一轮再做
        return
    registered = (r.get("task") or {}).get("name") or ""
    segs = seg["segments"]
    if not any(x.get("verb") != "静止" and (x.get("description") or x.get("action")) for x in segs):
        return
    # 全部段都带编号给模型（静止段也给，子任务分组要覆盖每一段）
    lines = "\n".join(f"#{i + 1} {x['t0']:.1f}–{x['t1']:.1f}s {x.get('description') or x.get('action') or '（未标注）'}"
                      for i, x in enumerate(segs))
    try:
        j, raw, sec = vlm.ask(model, OB.summary_prompt(registered, lines), [], timeout=timeout)
    except Exception as e:
        seg["summary"] = {"err": str(e)}
        seg.pop("groups", None)
        for x in segs:
            x.pop("group", None)
        save_json(p, seg)
        return str(e)
    if not j and batchq.active():  # 批量模式：汇总请求刚登记、结果还没回来，先保留原样
        return
    seg["summary"] = {"steps": j.get("steps") or [], "task_success": j.get("task_success"), "text": j.get("summary") or ""}
    seg["groups"] = check_groups(j.get("groups"), segs)
    for gi, g in enumerate(seg["groups"]):
        for k in range(g["from"] - 1, g["to"]):
            segs[k]["group"] = gi
    save_json(p, seg)
    old = jl((c.wd / "task.json").read_text(encoding="utf-8")) if (c.wd / "task.json").exists() else {}
    verdict = str(j.get("consistency") or "").strip()
    res = {"name": registered or str(j.get("task_seen") or ""), "seen": str(j.get("task_seen") or ""),
           "generated": not registered, "model": model, "sec": round(sec, 1), "from": "语义标注通读"}
    if registered:
        res.update(verdict=verdict or None, reason=str(j.get("reason") or ""),
                   consistent=None if not verdict else (verdict != "不一致"))
    if old.get("user_name"):
        res["user_name"] = old["user_name"]
    save_json(c.wd / "task.json", res)


def step_semantic(c):
    r = c.r
    model, s = vlm.need_model()
    if not model:
        return "skip", "Ollama 里没有视觉模型：" + "、".join(m["name"] for m in s["models"]), {}
    p = c.wd / "segments.json"
    seg = jl(p.read_text(encoding="utf-8"))
    task = jl((c.wd / "task.json").read_text(encoding="utf-8")) if (c.wd / "task.json").exists() else {}
    tname = task.get("seen") or task.get("name") or (r.get("task") or {}).get("name")
    if (c.wd / "objects.json").exists() and jl((c.wd / "objects.json").read_text(encoding="utf-8"))["objects"]:
        return semantic_fine(c, s["model"], seg, p, (r.get("task") or {}).get("name") or "", None)
    ctx = f"整段视频的任务是：“{tname}”。" if tname else ""
    low, fails, last_err = 0, 0, ""
    for i, sgm in enumerate(seg["segments"]):
        c.check_pause()
        c.report(i / len(seg["segments"]), f"第 {i+1}/{len(seg['segments'])} 段")
        ts = sample_times(sgm["t0"], sgm["t1"])
        imgs = [stamp(f, t) for f, t in zip(_frames_at(r, ts), ts)]
        try:
            j, raw, sec = vlm.ask(s["model"], vlm.SEG_PROMPT.format(t0=sgm["t0"], t1=sgm["t1"], n=len(imgs), ctx=ctx), imgs, max_side=frame_size(len(imgs)))
            err = ""
        except Exception as e:
            j, raw, sec, err = {}, "", 0, str(e)
            fails += 1
            last_err = err
        sgm.update(action=str(j.get("action") or ""), object=str(j.get("object") or ""), success=j.get("success"),
                   conf=j.get("confidence"), src="vlm", model=s["model"], sec=round(sec, 1), err=err)
        if not sgm["action"] or (isinstance(sgm["conf"], (int, float)) and sgm["conf"] < 0.5):
            low += 1
        save_json(p, seg)
    if fails == len(seg["segments"]):
        return "fail", last_err[:200], {"err": last_err}
    serr = summarize_and_check(c, s["model"], seg, p)
    return ("warn" if low or serr else "ok"), f"{len(seg['segments'])} 段" + (f" · 低置信 {low}" if low else "") + (f" · 失败 {fails}" if fails else "") + (f" · 汇总与子任务分组失败：{serr[:80]}" if serr else ""), {"low": low}


def _wrist_track(hf, fps, t0, t1):
    """这段时间里每只手手腕的位置序列（像素）。返回 {side: [(t, x, y), ...]} 和手出现的比例"""
    if not hf:
        return {}, 0.0
    i0, i1 = max(0, int(t0 * fps)), min(len(hf) - 1, int(t1 * fps))
    step = max(1, int(fps / 10))
    out, seen, tot = {"left": [], "right": []}, 0, 0
    for fi in range(i0, i1 + 1, step):
        tot += 1
        row = hf[fi] or []
        if row:
            seen += 1
        for side, conf, pts, ex in row:
            if ex or not pts or not pts[0] or pts[0][0] is None:
                continue
            out.setdefault(side, []).append((fi / fps, pts[0][0], pts[0][1]))
    return out, (seen / tot if tot else 0.0)


def _obj_center(trk, oid, t):
    if not trk:
        return None
    o = next((o for o in trk["objects"] if o["id"] == oid), None)
    if not o:
        return None
    ti = min(max(0, int(round(t * trk["fps"]))), len(trk["t"]) - 1)
    for d in range(0, 6):  # 当前时刻没有就往附近找
        for j in (ti - d, ti + d):
            if 0 <= j < len(o["centroid"]) and o["centroid"][j]:
                return o["centroid"][j]
    return None


def rule_label(segs, i, hf, fps, W, trk, by_id):
    """没有接触的段先用规则判断，不调用大模型。返回标注 dict 或 None（规则判断不了）"""
    sg = segs[i]
    if sg.get("facts"):
        return None
    dur = max(sg["t1"] - sg["t0"], 1e-3)
    tr, present = _wrist_track(hf, fps, sg["t0"], sg["t1"])
    speed = {}
    for side, pts in tr.items():
        if len(pts) >= 2:
            d = sum(np.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(pts[:-1], pts[1:]))
            speed[side] = d / dur / W  # 每秒移动多少个画面宽度
    fast = max(speed.values()) if speed else 0.0
    base = dict(object_id="", object="", target="", success=None, evidence="规则判断", src="rule", sec=0.0, err="", facts=[])
    if present < 0.3 or fast < 0.08:
        return dict(base, hand="无", verb="静止", action="静止", description="手没有操作物体")
    side = max(speed, key=speed.get)
    pts = tr[side]
    hand = {"left": "左手", "right": "右手"}[side]
    nxt = next((e for s2 in segs[i + 1:] for e in s2.get("facts", []) if e["hand"] == side), None)
    prv = next((e for s2 in segs[:i][::-1] for e in s2.get("facts", []) if e["hand"] == side), None)
    def dist(e, p):
        c = _obj_center(trk, e["obj"], p[0])
        return np.hypot(c[0] - p[1], c[1] - p[2]) if c else None
    if nxt:
        d0, d1 = dist(nxt, pts[0]), dist(nxt, pts[-1])
        if d0 is not None and d1 is not None and d0 - d1 > 0.05 * W:
            lab = by_id.get(nxt["obj"], {}).get("label", "")
            return dict(base, hand=hand, verb="伸向", object_id=nxt["obj"], object=lab, action="伸向" + lab,
                        description=f"{hand}伸向{nxt['obj']}（{lab}）")
    if prv:
        d0, d1 = dist(prv, pts[0]), dist(prv, pts[-1])
        if d0 is not None and d1 is not None and d1 - d0 > 0.05 * W:
            lab = by_id.get(prv["obj"], {}).get("label", "")
            return dict(base, hand=hand, verb="收回", object_id="", object="", action="收回",
                        description=f"{hand}离开{prv['obj']}（{lab}）后收回")
    return None


def prev_line(sg):
    """给下一段的“前面几段”上下文。批量模式下各段同时提交，互相看不到模型的描述，
    改用规则测出来的事实（谁、什么时候、碰了哪个物体），这样每段的请求都不依赖别的段的结果"""
    from . import batchq
    head = f"  {sg['t0']:.1f}–{sg['t1']:.1f}s："
    if not batchq.active():
        return head + (sg.get("description") or sg.get("action") or "（未标注）")
    hand = {"left": "左手", "right": "右手"}
    fs = sg.get("facts") or []
    if not fs:
        return head + ((sg.get("description") if sg.get("src") == "rule" else "") or "手没有接触已识别的物体")
    return head + "；".join(f"{hand.get(e['hand'], '')}{e.get('kind') or '接触'} {e['obj']}（{e.get('label', '')}）" for e in fs[:3])


def seg_objs(trk, objs, t0, t1, facts):
    """这段时间里画面上出现过（有跟踪框）或被接触的物体，按清单顺序"""
    if not trk:
        return objs
    a = max(0, int(t0 * trk["fps"]))
    b = min(len(trk["t"]) - 1, int(np.ceil(t1 * trk["fps"])))
    vis = {o["id"] for o in trk["objects"] if any(o["bbox"][i] for i in range(a, b + 1))} | {e["obj"] for e in facts or []}
    return [o for o in objs if o["id"] in vis]


def semantic_fine(c, model, seg, p, tname, seen):
    r = c.r
    objs = jl((c.wd / "objects.json").read_text(encoding="utf-8"))["objects"]
    by_id = {o["id"]: o for o in objs}
    trk = jl((c.wd / "track.json").read_text(encoding="utf-8")) if (c.wd / "track.json").exists() else None
    hf = jl((c.wd / "hands2d.json").read_text(encoding="utf-8"))["frames"] if (c.wd / "hands2d.json").exists() else None
    rgb = next(x for x in r["streams"] if x["kind"] == "rgb")
    fps = rgb.get("fps") or 30
    W = rgb.get("w") or 1280
    task_txt = (tname or "未知") + (f"（模型看到的是：{seen}）" if seen and seen != tname else "")
    vtext = OB.vocab_text()
    timeout = int(get_setting("vlm_timeout") or 180)
    masks = load_masks(c.wd) if (c.wd / "masks.npz").exists() and trk else None
    light_model = get_setting("vlm_model_light") or ""
    if light_model and light_model not in [m["name"] for m in vlm.status().get("models", [])]:
        light_model = ""
    prev, fails, last_err, n_rule, t_start, n_to = [], 0, "", 0, time.time(), 0
    segs = seg["segments"]
    for i, sg in enumerate(segs):
        c.check_pause()
        c.report(i / len(segs), f"第 {i+1}/{len(segs)} 段")
        facts = sg.get("facts") or []
        rl = rule_label(segs, i, hf, fps, W, trk, by_id) if hf is not None else None
        if rl is not None:
            sg.update(rl, review=False, model="", conf=None)
            n_rule += 1
            prev.append(prev_line(sg))
            save_json(p, seg)
            continue
        light = not facts  # 没接触但手在动、规则判断不了：少送几帧
        ts = key_times(sg["t0"], sg["t1"], facts) if not light else sg["t0"] + (np.arange(4) + 0.5) * (sg["t1"] - sg["t0"]) / 4
        raw_imgs = _frames_at(r, ts)
        tm = (sg["t0"] + sg["t1"]) / 2
        vis_objs = seg_objs(trk, objs, sg["t0"], sg["t1"], facts)
        # 每段只列这段时间画面里出现过的物体：中途才露面（或人工补进清单）的物体不会被提前提到；清单改动也只影响相关的段
        inv = "\n".join(f"  {o['id']} · {o['label']}" for o in vis_objs) or "  （这段时间画面里没有已识别的物体）"
        mark = OB.near_ids(trk, [e["obj"] for e in facts], tm) if facts else ({o["id"] for o in vis_objs[:6]} if trk else set())
        raw_imgs = [OB.overlay_marks(f, trk, masks, t, mark) for f, t in zip(raw_imgs, ts)]
        imgs = [stamp(f, t) for f, t in zip(raw_imgs, ts)]
        crop = None
        if imgs and hf is not None and facts:
            tm = (sg["t0"] + sg["t1"]) / 2
            fi = min(int(round(tm * fps)), len(hf) - 1)
            boxes = []
            if trk:
                ti = min(int(round(tm * trk["fps"])), len(trk["t"]) - 1)
                boxes = [o["bbox"][ti] for o in trk["objects"] if any(f["obj"] == o["id"] for f in facts)]
            crop = OB.crop_for(raw_imgs[len(raw_imgs) // 2], hf[fi], facts, boxes)
        send = imgs + ([crop] if crop is not None else [])
        marks = ("\n画面里用彩色轮廓和编号标出了物体（" + "、".join(sorted(mark)) + "），编号和下面的物体清单一致，描述时请用这些编号。") if mark and masks is not None else ""
        prompt = OB.SEG2_PROMPT.format(t0=sg["t0"], t1=sg["t1"], n=len(imgs), crop="，最后一张是手部附近的放大图" if crop is not None else "",
                                       marks=marks, task=task_txt, inv=inv, facts=OB.fact_text(facts, by_id, trk),
                                       prev="\n".join(prev[-4:]) or "  （这是第一段）", verbs=vtext)
        try:
            simple = len(facts) == 1  # 单手、单物体、一次接触：交给小模型（如果设置了）
            use = light_model if (light_model and (light or simple)) else model
            j, raw, sec = vlm.ask(use, prompt, send, timeout=timeout if not light else max(60, timeout // 2),
                                  max_side=frame_size(len(imgs)) if not light else 512)
            err = ""
            n_to = 0
        except Exception as e:
            j, raw, sec, err = {}, "", 0, str(e)
            use = model
            if "秒没有返回" in err:
                n_to += 1
                if n_to >= 2:  # 连续两段都超时：模型基本卡住了，别再一段段地等，直接结束这一步
                    raise RuntimeError("连续两段模型调用超时，已停止这一步。" + err[-160:])
            else:
                n_to = 0
            if facts:  # 有接触却没标出来，才算真失败
                fails += 1
                last_err = err
        if not j and not facts:  # 模型没话说 / 失败，且这段没接触：按静止处理
            j = {"hand": "无", "verb": "静止", "description": "手没有操作物体"}
            err = ""
        oid = str(j.get("object") or "").strip().upper()
        oname = str(j.get("object_name") or "").strip()
        off_list = False
        # 模型说的物体名和这个编号在清单里的名字对不上：多半是清单外的东西借用了别的编号（胶带借了试管 O11）。
        # 宁可缺，不能错：编号清空，记成“清单外”，交给人工复核
        if oid in by_id and oname and not OB._name_match(oname, by_id[oid]["label"]):
            off_list = True
        elif oid not in by_id and oname:
            off_list = True
        if off_list:
            if oid:
                desc0 = str(j.get("description") or "")
                j["description"] = re.sub(r"[（(]\s*" + re.escape(oid) + r"\s*[）)]", "", desc0).replace(oid, oname)
            oid = ""
        lab = (oname if off_list else by_id[oid]["label"]) if (off_list or oid in by_id) else str(j.get("object") or "")
        verb_raw = str(j.get("verb") or "")
        verb, review = OB.normalize_verb(verb_raw) if verb_raw else ("", bool(j))
        # 去向优先用规则算出来的空间关系（量出来的，比模型猜的可靠）
        rule_tg = next((OB.move_text(trk, e)[1] for e in facts if e.get("moved") and OB.move_text(trk, e)[1]), "")
        sg.update(hand=str(j.get("hand") or ""), verb=verb, verb_raw=verb_raw if verb_raw != verb else "",
                  review=review or off_list, off_list=off_list, object_id=oid if oid in by_id else "", object=lab,
                  target=rule_tg or str(j.get("target") or ""), target_vlm=str(j.get("target") or ""), description=OB.fix_dir(str(j.get("description") or ""), facts, oid),
                  action=(verb + (lab if lab else "")) or str(j.get("description") or ""), success=j.get("success"),
                  evidence=str(j.get("evidence") or ""), conf=None, src="vlm", model=use, sec=round(sec, 1), err=err,
                  facts=facts)
        prev.append(prev_line(sg))
        save_json(p, seg)
    serr = summarize_and_check(c, model, seg, p, timeout)
    seg["vocab_version"] = get_setting("vocab_version") or 1
    seg["elapsed"] = round(time.time() - t_start, 1)
    save_json(p, seg)
    n = len(segs)
    n_vlm = n - n_rule
    if n_vlm and fails == n_vlm:
        return "fail", last_err[:200], {"err": last_err}
    el = seg["elapsed"]
    tm = f"{el/60:.1f} 分钟" if el >= 90 else f"{el:.0f} 秒"
    msg = f"{n} 段 · 规则 {n_rule} · 模型 {n_vlm} · 用时 {tm}" + (f" · 失败 {fails}" if fails else "") + (f" · 汇总与子任务分组失败：{serr[:80]}" if serr else "")
    return ("warn" if fails or serr else "ok"), msg, {"fails": fails, "elapsed": el, "rule": n_rule}


# ============================================================ 清单外补找 ==========
REFIND_MAX = 5  # 每条录制最多补找几种物体


def _near_box(trk, oid, fi, win=3):
    """跟踪结果里某个物体在第 fi 帧附近（±win 帧）最近的一个框"""
    o = next((o for o in trk["objects"] if o["id"] == oid), None)
    if not o:
        return None
    for d in range(win + 1):
        for k in (fi - d, fi + d):
            if 0 <= k < len(o["bbox"]) and o["bbox"][k]:
                return o["bbox"][k]
    return None


def requeue(rid, keys):
    """把这条录制已有的这些步骤重新排队（不打断当前正在跑的步骤）"""
    from . import batchq
    keys = [k for k in keys if q("SELECT 1 FROM steps WHERE rec_id=? AND key=?", (rid, k), one=True)]
    batchq.reset_failed(rid, keys)
    for k in keys:
        x("UPDATE steps SET status='wait', progress=0, msg='' WHERE rec_id=? AND key=?", (rid, k))
    x("UPDATE recordings SET status='queued' WHERE id=?", (rid,))
    _wake.set()
    return keys


def step_refind(c):
    """语义标注里有“清单外”的段（模型说到的物体不在物体清单里）：在那一段的画面里让大模型把它框出来，
    以 A1、A2… 编号加进物体清单（存在 objects_manual.json，标记“补找”），再重跑跟踪、接触、切点和语义。
    这一步跑完就是完成状态，重跑语义后即使又出现清单外物体也不会再补，避免循环；没有清单外物体时立刻结束"""
    if get_setting("offlist_refind") is False:
        return "skip", "设置里已关闭“清单外补找”", {}
    sp, oj, tj = c.wd / "segments.json", c.wd / "objects.json", c.wd / "track.json"
    if not sp.exists() or not oj.exists():
        return "skip", "没有分段或物体清单", {}
    if not tj.exists():
        return "skip", "需要物体跟踪结果", {}
    segs = jl(sp.read_text(encoding="utf-8")).get("segments") or []
    names = []  # [(名称, [段号…])]，同名合并
    for i, sg in enumerate(segs):
        nm = str(sg.get("object") or "").strip()
        if not sg.get("off_list") or not nm:
            continue
        hit = next((n for n in names if OB._name_match(n[0], nm)), None)
        if hit:
            hit[1].append(i)
        else:
            names.append([nm, [i]])
    if not names:
        return "ok", "没有清单外物体", {"n": 0}
    model, s = vlm.need_model()
    if not model:
        return "skip", "没有可用的视觉模型", {}
    objs = jl(oj.read_text(encoding="utf-8"))["objects"]
    trk = jl(tj.read_text(encoding="utf-8"))
    tfps = float(trk.get("fps") or 5)
    mp = c.wd / "objects_manual.json"
    man = jl(mp.read_text(encoding="utf-8")) if mp.exists() else {"objects": {}}
    mo = man.setdefault("objects", {})
    na = max([int(k[1:]) for k in mo if k.startswith("A") and k[1:].isdigit()] or [0])
    st = rgb_stream(c.r)
    added, same, miss, calls = [], [], [], []
    from . import batchq
    try:
        for k, (nm, idx) in enumerate(names[:REFIND_MAX]):
            c.check_pause()
            c.report(k / min(len(names), REFIND_MAX), f"找“{nm}”")
            sg = segs[idx[0]]
            t0, t1 = float(sg["t0"]), float(sg["t1"])
            # 先看这段开头（手还没碰到时多半完整露出），再看结尾（放下之后），最后看中间
            found = None
            for t in (t0 + 0.1, t1 - 0.1, (t0 + t1) / 2):
                t = min(max(0.0, t), max(0.0, c.r["duration"] - 0.05))
                img = st.read(st.index_at(st.ts[0] + t))
                if img is None:
                    continue
                h, w = img.shape[:2]
                boxes, raw = OB._ask_boxes(model, img, OB.FIND_PROMPT.format(name=nm.replace('"', '')), (0, 0, w, h))
                calls.append({"name": nm, "t": round(t, 2), "n": len(boxes)})
                boxes = [b for _, b in boxes if b[2] > b[0] and b[3] > b[1] and 0.00008 < (b[2] - b[0]) * (b[3] - b[1]) / (w * h) < 0.5]
                if boxes:
                    b = boxes[0]
                    found = ([round(float(np.clip(b[0], 0, w)), 1), round(float(np.clip(b[1], 0, h)), 1),
                              round(float(np.clip(b[2], 0, w)), 1), round(float(np.clip(b[3], 0, h)), 1)], t)
                    break
            if not found:
                miss.append(nm)
                continue
            box, t = found
            fi = min(len(trk["t"]) - 1, int(round(t * tfps)))
            # 和清单里已有的物体是同一个吗（其实不是清单外，只是叫法不同）。只认两种情况：
            #   名字对得上，且框基本重合；或者名字对不上，但两个框几乎一模一样、大小也相近。
            # 不能只看“新框落在旧框里”：书放在置物架里、数据线放在抽屉里，新物体的框天然在容器的框里，但它们是不同的物体
            def _dup(o):
                bb = _near_box(trk, o["id"], fi)
                if bb is None:
                    return False
                a1, a2 = (box[2] - box[0]) * (box[3] - box[1]), (bb[2] - bb[0]) * (bb[3] - bb[1])
                similar = a1 > 0 and a2 > 0 and min(a1, a2) / max(a1, a2) > 0.5
                if OB._name_match(nm, o["label"]):
                    return OB._iou(box, bb) > 0.3 or (similar and OB._contain(box, bb) > 0.7)
                return similar and OB._iou(box, bb) > 0.7
            dup = next((o for o in objs if _dup(o)), None)
            dup = dup or next((o for o in objs if o.get("found") == "补找" and OB._name_match(nm, o["label"])), None)
            if dup:
                same.append(f"{nm}（判为已有的 {dup['id']} {dup['label'][:12]}）")
                continue
            na += 1
            mo[f"A{na}"] = {"label": nm[:40], "bbox": box, "t": round(t, 3), "found": "补找", "seg": idx[0]}
            added.append(f"A{na} {nm}")
    finally:
        st.close()
    if batchq.active() and batchq.pending():
        return "ok", "等批量结果", {}  # 批量模式：本步还有请求没回来，先不改清单（结果回来后整步重跑）
    info = {"n": len(added), "added": added, "same": same, "miss": miss, "calls": calls, "names": [n for n, _ in names]}
    if not added:
        tail = (f"和清单里已有物体是同一个：{'、'.join(same)}" if same else "") + (("；" if same else "") + f"没框出来：{'、'.join(miss)}" if miss else "")
        return "warn", f"清单外 {len(names)} 种，没有新增 · " + tail, info
    man["updated"] = time.time()
    mp.write_text(jd(man), encoding="utf-8")
    d = jl(oj.read_text(encoding="utf-8"))
    d["objects"] = OB.apply_manual(d["objects"], man)
    save_json(oj, d)
    keys = requeue(c.rid, ["track", "contact", "cuts", "semantic"])
    event(c.bid, c.rid, "info", f"{c.r.get('name', c.rid)} 清单外补找：新增 {'、'.join(added)}，重新跟踪并标注")
    msg = f"新增 {len(added)} 个：" + "、".join(added) + (f" · 判为已有物体 {len(same)}" if same else "") + (f" · 没框出来 {len(miss)}" if miss else "") + " · 已重新排队：跟踪→语义"
    info["rerun"] = keys
    return "ok", msg, info


STEPS = {"objects": step_objects, "track": step_track, "contact": step_contact, "ingest": step_ingest, "sync": step_sync, "quality": step_quality, "kp_verify": step_kp_verify, "hand_est": step_hand_est,
         "traj": step_traj, "imu": step_imu, "grip": step_grip, "joints": step_joints, "dex": step_simple, "ft": step_simple,
         "cuts": step_cuts, "task": step_task, "semantic": step_semantic, "refind": step_refind}


# ============================================================ 调度 ==============
def enqueue(bid, recs, on_by_group, vlm_ok):
    """为每条可处理的录制写入步骤表"""
    n = 0
    for r in recs:
        on = on_by_group.get(r["group"], {})
        steps = P.plan(r, on, vlm_ok)
        if P.blocked(r):
            continue
        bump(r["id"])
        x("DELETE FROM steps WHERE rec_id=?", (r["id"],))
        seen = set()
        for i, s in enumerate(steps):
            key = s["key"] if s["key"] not in seen else f"{s['key']}_{i}"
            seen.add(key)
            st = "skip" if s["k"] == "skip" else "wait"
            x("INSERT INTO steps(rec_id,key,ord,label,mode,status,msg) VALUES(?,?,?,?,?,?,?)",
              (r["id"], key, i, s["s"], s["k"], st, s["w"] if st == "skip" else ""))
        x("UPDATE recordings SET status='queued', edits=? WHERE id=?", (jd({"on": on}), r["id"]))
        n += 1
    x("UPDATE batches SET status='processing' WHERE id=?", (bid,))
    event(bid, None, "info", f"开始处理 {n} 条录制")
    _wake.set()
    return n


# ============================================================ 资源感知调度 ==========
# 同一套代码适配笔记本、单卡工作站、多卡服务器：启动时看本机有什么（显卡数量和显存、CPU 核数、大模型在哪），
# 每个步骤按它要用的资源归类，调度器按“空位”派活。同一条录制的步骤始终按顺序一个接一个跑。
GPU_STEPS = {"track"}                                  # 显卡上的重活：SAM 2 跟踪
VLM_STEPS = {"objects", "semantic", "refind", "task"}  # 调大模型
RUNNING = {}            # rec_id -> {bid, rid, key, res, dev, since}
OOM = {}                # 显卡编号 -> 因显存不够自动减掉的并发数
_sched_lock = threading.Lock()
_res = {"t": 0, "v": None}


def resources(force=False):
    """本机资源（60 秒缓存）：显卡列表 [{i, name, mem_mb}]、CPU 逻辑核数"""
    if not force and _res["v"] and time.time() - _res["t"] < 60:
        return _res["v"]
    import subprocess
    gpus = []
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5, creationflags=media._NOWIN)
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                i, name, mem = [p.strip() for p in line.split(",")]
                gpus.append({"i": int(i), "name": name, "mem_mb": int(float(mem))})
    except Exception:
        pass
    v = {"gpus": gpus, "cores": int(os.environ.get("EDP_CPU_CORES") or 0) or os.cpu_count() or 2}  # 环境变量可覆盖（测试或限制用）
    _res.update(t=time.time(), v=v)
    return v


def _int(k, d=0):
    try:
        return int(get_setting(k) or d)
    except Exception:
        return d


def slots():
    """各类资源能同时跑几个任务。
    gpu：每张卡按显存算（每个跟踪任务预留约 4 GB，系统和显示再留 2.5 GB），1–4 个；设置里可手动指定；显存不够失败过会自动减。
    vlm：外部接口（百炼）时同时发几个请求；本机大模型（Ollama / Mage-VL）时和第 0 张卡上的跟踪共用一个位置（跟踪前要把大模型移出显存）。
    cpu：入库、同步、接触等，按核数的一半，最多 6 个。
    total：总并发上限，设置里填 1 就和以前完全一样，一次只跑一个步骤。"""
    r = resources()
    per = _int("gpu_jobs")
    g = {}
    for gp in r["gpus"]:
        auto = max(1, min(4, int((gp["mem_mb"] / 1024 - 2.5) // 4)))
        g[gp["i"]] = max(1, (per or auto) - OOM.get(gp["i"], 0))
    local = vlm.backend() != "api"
    if local and 0 in g:
        g[0] = 1
    out = {"gpu": g, "cpu_track": 1 if not g else 0, "vlm": 1 if local else max(1, _int("api_parallel", 4)),
           "cpu": max(1, min(6, r["cores"] // 2)), "local_vlm": local}
    total = _int("max_jobs")
    out["total"] = total if total > 0 else sum(g.values()) + out["vlm"] + out["cpu"] + out["cpu_track"]
    return out


def _res_class(key):
    base = key.split("_")[0] if key.split("_")[-1].isdigit() else key
    if base in GPU_STEPS or (base == "objects" and (get_setting("inventory_mode") or "detector") == "detector"):
        return "gpu"
    if base in VLM_STEPS:
        return "vlm"
    return "cpu"


def _place(res, sl):
    """给这类任务找一个空位，返回设备编号（显卡号；CPU 任务为 None），没有空位返回 False"""
    use = [v for v in RUNNING.values()]
    if res == "cpu":
        return None if sum(1 for u in use if u["res"] == "cpu") < sl["cpu"] else False
    gpu0_busy = sum(1 for u in use if u["dev"] == 0 and u["res"] in ("gpu", "vlm"))
    if res == "vlm":
        if sl["local_vlm"] and sl["gpu"]:  # 本机大模型在第 0 张卡上，和那张卡上的跟踪轮流用
            return 0 if gpu0_busy < 1 else False
        return "net" if sum(1 for u in use if u["res"] == "vlm") < sl["vlm"] else False
    if not sl["gpu"]:  # 没有显卡：跟踪在 CPU 上跑，一次一个
        return None if sum(1 for u in use if u["res"] == "gpu") < 1 else False
    best = None
    for i, cap in sorted(sl["gpu"].items()):
        n = sum(1 for u in use if u["dev"] == i and (u["res"] == "gpu" or (i == 0 and sl["local_vlm"] and u["res"] == "vlm")))
        if n < cap and (best is None or n < best[1]):
            best = (i, n)
    return best[0] if best else False


def running_list():
    return sorted(RUNNING.values(), key=lambda u: u["since"])


def is_running(rid, keys=None):
    u = RUNNING.get(rid)
    return bool(u) and (keys is None or u["key"] in keys)


def _candidates():
    """可以开始的步骤：每条录制只看它的下一步（前面没有还在等、在跑、在等批量结果的步骤）。暂停的批次跳过"""
    ps = list(STATE["paused"])
    cond = (" AND r.batch_id NOT IN (" + ",".join("?" * len(ps)) + ")") if ps else ""
    # 置顶的批次（prio 大）先跑；同优先级按导入先后；同一批次按录制顺序
    return q(f"""SELECT s.rec_id, s.key, r.batch_id, r.edits FROM steps s JOIN recordings r ON r.id=s.rec_id
                LEFT JOIN batches b ON b.id=r.batch_id
                WHERE s.status='wait'{cond}
                  AND NOT EXISTS (SELECT 1 FROM steps s2 WHERE s2.rec_id=s.rec_id AND s2.status IN ('wait','run','batch') AND s2.ord < s.ord)
                ORDER BY COALESCE(b.prio,0) DESC, b.created, r.id, s.ord LIMIT 400""", tuple(ps))


def next_job():
    """（兼容旧接口）下一个要跑的步骤"""
    c = _candidates()
    return c[0] if c else None


def _dispatch():
    """按空位派活，返回这一轮开始了几个任务"""
    started = 0
    with _sched_lock:
        sl = slots()
        for j in _candidates():
            if len(RUNNING) >= sl["total"]:
                break
            if j["rec_id"] in RUNNING:
                continue
            res = _res_class(j["key"])
            dev = _place(res, sl)
            if dev is False:
                continue
            u = {"bid": j["batch_id"], "rid": j["rec_id"], "key": j["key"], "res": res, "dev": dev, "since": time.time()}
            RUNNING[j["rec_id"]] = u
            x("UPDATE steps SET status='run', started=?, progress=0 WHERE rec_id=? AND key=?", (u["since"], j["rec_id"], j["key"]))
            x("UPDATE recordings SET status='running' WHERE id=?", (j["rec_id"],))
            threading.Thread(target=_run_job, args=(j, u), daemon=True).start()
            started += 1
        cur = running_list()
        STATE["current"] = (cur[0]["bid"], cur[0]["rid"], cur[0]["key"]) if cur else None
        STATE["since"] = cur[0]["since"] if cur else time.time()
    return started


def worker():
    """调度线程：有空位就派活；每个任务在自己的线程里跑，跑完叫醒调度线程"""
    while not STATE["stop"]:
        try:
            n = _dispatch()
        except Exception:  # 任何意外都不能让调度线程退出，否则队列会一直停着
            traceback.print_exc()
            n = 0
            time.sleep(1)
        if not n:
            _wake.wait(1.0)
            _wake.clear()


def _work_once():
    """（兼容旧接口）派一轮活"""
    return _dispatch()


def _run_job(j, u):
    rid, key, bid = j["rec_id"], j["key"], j["batch_id"]
    try:
        meta = q("SELECT s.label, r.name FROM steps s JOIN recordings r ON r.id=s.rec_id WHERE s.rec_id=? AND s.key=?", (rid, key), one=True) or {"label": key, "name": rid}
        base = key.split("_")[0] if key.split("_")[-1].isdigit() else key
        fn = STEPS.get(base, step_simple)
        on = jl(j["edits"]).get("on", {})
        gen0 = STATE["gen"].get(rid, 0)
        from . import batchq
        batchq.begin(bid, rid, key, batchq.batch_mode(bid))
        err = None
        try:
            c = Ctx(bid, rid, key, on)
            c.gen = gen0
            c.dev = u["dev"] if isinstance(u["dev"], int) else None
            st, msg, res = fn(c)
        except Cancelled:
            st, msg, res = None, "", {}
        except Exception as e:
            st, msg, res = "fail", f"{type(e).__name__}: {str(e)[:120]}", {"trace": traceback.format_exc()[-2000:]}
            err = msg
        misses = batchq.end()
        if STATE["gen"].get(rid, 0) != gen0:  # 跑的过程中这条录制被重新排队了：丢掉旧结果
            event(bid, rid, "info", f"{meta['name']} · {meta['label']}：已重新排队，旧的这一步作废")
            return
        if err and u["res"] == "gpu" and isinstance(u["dev"], int) and "out of memory" in err.lower():
            # 显存不够：这张卡少放一个并发，这一步放回队列重跑（已经只剩 1 个并发时照常记失败）
            if slots()["gpu"].get(u["dev"], 1) > 1:
                OOM[u["dev"]] = OOM.get(u["dev"], 0) + 1
                x("UPDATE steps SET status='wait', progress=0, msg=? WHERE rec_id=? AND key=?", ("显存不够，减少并发后重新排队", rid, key))
                event(bid, rid, "warn", f"{meta['name']} · {meta['label']}：第 {u['dev']} 张显卡显存不够，这张卡改为少跑一个并发，已重新排队")
                return
        if misses and st is not None:
            # 批量模式：这一步有请求还没拿到结果。先挂起，结果回来后自动重跑（重跑时命中缓存）
            st, msg, res = "batch", f"等待批量结果：本步 {misses} 个模型请求已登记", {"misses": misses}
        elif err:
            event(bid, rid, "bad", f"{meta['name']} · {meta['label']} 失败：{msg}")
        if st is None:
            return
        x("UPDATE steps SET status=?, msg=?, result=?, progress=1, finished=? WHERE rec_id=? AND key=?",
          (st, msg, jd(res)[:20000], time.time(), rid, key))
        if st == "warn":
            event(bid, rid, "warn", f"{meta['name']} · {meta['label']}：{msg}")
        left = q("SELECT COUNT(*) n FROM steps WHERE rec_id=? AND status IN ('wait','run','batch')", (rid,), one=True)["n"]
        if not left:
            fails = q("SELECT COUNT(*) n FROM steps WHERE rec_id=? AND status='fail'", (rid,), one=True)["n"]
            x("UPDATE recordings SET status=? WHERE id=?", ("failed" if fails else "done", rid))
            event(bid, rid, "bad" if fails else "ok", f"{meta['name']} 处理完成" + (f"，{fails} 步失败" if fails else ""))
            if not q("SELECT 1 FROM recordings WHERE batch_id=? AND status IN ('queued','running')", (bid,)):
                x("UPDATE batches SET status='done' WHERE id=?", (bid,))
        else:
            x("UPDATE recordings SET status='queued' WHERE id=? AND status='running'", (rid,))
    except Exception:
        traceback.print_exc()
    finally:
        with _sched_lock:
            if RUNNING.get(rid) is u:
                del RUNNING[rid]
            cur = running_list()
            STATE["current"] = (cur[0]["bid"], cur[0]["rid"], cur[0]["key"]) if cur else None
        _wake.set()


def start_worker():
    # 上次关机时中断的步骤重新排队
    x("UPDATE steps SET status='wait', progress=0 WHERE status='run'")
    threading.Thread(target=worker, daemon=True).start()
    from . import batchq
    batchq.start()
