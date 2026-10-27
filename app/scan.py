"""识别 + 预检。全部是确定性规则：看文件结构、字段名、数值统计，不用大模型。

流程：
  1. 遍历文件夹，找出“流”（视频文件、图片序列文件夹）
  2. 按目录和文件名把流和旁边的表格、标定、说明文件归成“录制”
  3. 逐个文件判断类型：时间戳表、IMU、位姿、关节、夹爪、手部关键点、标定、任务说明……
  4. 每条录制做检查：能否解码、时间戳、深度、彩色与深度是否同一次录制
  5. 算能力画像（A 动作 / T 时间 / S 标定 / V 视觉 / L 语义 / R 本体）和输入质量等级
"""
import csv
import io
import json
import os
import re
import time
from pathlib import Path

import numpy as np

from . import media
from .db import jd, work_dir

SKIP_DIRS = {"__macosx", ".git", ".venv", "venv", "__pycache__", "handcheck_result", "node_modules", ".idea", ".vscode"}
ROLE_TOKENS = {"rgb", "color", "colour", "depth", "d", "ir", "image", "images", "img", "video", "frames", "frame",
               "aligned", "raw", "stream", "png", "jpg"}
DOC_EXT = {".md", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".html", ".htm", ".url", ".py", ".bat", ".cfg", ".ini", ".zip", ".7z", ".rar"}
UNSUPPORTED = {".bag": "ROS1 bag", ".mcap": "MCAP", ".db3": "ROS2 bag", ".hdf5": "HDF5", ".h5": "HDF5", ".svo": "ZED SVO",
               ".svo2": "ZED SVO", ".bin": "二进制", ".dat": "二进制", ".pcd": "点云", ".ply": "点云", ".npz": "NumPy 压缩包"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".aac", ".m4a"}

HAND_NAMES = ["wrist", "thumb_cmc", "thumb_mcp", "thumb_ip", "thumb_tip", "index_mcp", "index_pip", "index_dip", "index_tip",
              "middle_mcp", "middle_pip", "middle_dip", "middle_tip", "ring_mcp", "ring_pip", "ring_dip", "ring_tip",
              "pinky_mcp", "pinky_pip", "pinky_dip", "pinky_tip"]


# ============================================================ 1. 遍历 ==========
def walk(root: Path):
    """返回 {目录: [文件名]}，跳过隐藏和无关目录"""
    tree = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x.lower() not in SKIP_DIRS and not x.startswith(".")]
        tree[Path(d)] = sorted(f for f in files if not f.startswith(".") and not f.startswith("~$"))
    return tree


def is_seq_dir(files):
    imgs = [f for f in files if Path(f).suffix.lower() in media.IMG_EXT]
    return len(imgs) >= 10 and len(imgs) >= 0.8 * len(files), sorted(imgs, key=_natkey)


def _natkey(s):
    return [float(t) if re.fullmatch(r"\d+(\.\d+)?", t) else t for t in re.split(r"(\d+(?:\.\d+)?)", s)]


def base_key(name):
    toks = [t for t in re.split(r"[_\-\.\s]+", Path(name).stem.lower()) if t]
    rest = [t for t in toks if t not in ROLE_TOKENS]
    return "_".join(rest) or "main"


# ============================================================ 2. 读各类文件 ======
def read_table(p: Path, max_rows=None):
    """读 csv / txt 表格。自动识别分隔符、'#' 开头的表头。返回 (列名, ndarray[float]) 或 None"""
    try:
        with open(p, "r", encoding="utf-8-sig", errors="ignore") as f:
            head = [f.readline() for _ in range(5)]
            text = "".join(head) + (f.read() if max_rows is None else "".join(f.readline() for _ in range(max_rows)))
    except Exception:
        return None
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    first = lines[0].lstrip("#").strip()

    def pick(line):
        return "," if line.count(",") >= 1 else ("\t" if "\t" in line else None)

    def mk(sep):
        return (lambda s: [t.strip() for t in s.split(sep)]) if sep else (lambda s: s.split())

    hsplit = mk(pick(first))
    split = mk(pick(lines[1]))  # 表头和数据的分隔符可能不同（例如表头用逗号、数据用空格）
    cols = hsplit(first)
    has_header = any(re.search(r"[A-Za-z_一-鿿]", c) and not _isnum(c) for c in cols)
    body = lines[1:] if has_header else lines
    if not has_header:
        cols = [f"c{i}" for i in range(len(split(lines[0])))]
    cols = [re.sub(r"\[.*?\]|\(.*?\)", "", c).strip().lower() for c in cols]
    rows = []
    for l in body:
        parts = split(l.lstrip("#"))
        if len(parts) != len(cols):
            continue
        rows.append([_num(v) for v in parts])
    if not rows:
        return None
    return cols, np.array(rows, dtype=float)


def _isnum(s):
    try:
        float(s)
        return True
    except Exception:
        return False


def _num(s):
    try:
        return float(s)
    except Exception:
        return np.nan


def ts_column(cols, arr):
    """找时间戳列，返回 (列号, 秒数组)。识别秒 / 毫秒 / 微秒 / 纳秒。"""
    cand = [i for i, c in enumerate(cols) if re.search(r"time|stamp|^t$|^ts$|^sec", c)]
    if not cand:
        v = arr[:, 0]
        if np.all(np.diff(v[~np.isnan(v)]) >= 0) and np.nanmax(v) > 1e5:
            cand = [0]
    if not cand:
        return None, None
    i = cand[0]
    v = arr[:, i].astype(float)
    name = cols[i]
    mx = np.nanmax(np.abs(v)) if len(v) else 0
    if "ns" in name or mx > 1e17:
        v = v / 1e9
    elif "us" in name or mx > 1e14:
        v = v / 1e6
    elif "ms" in name or mx > 1e11:
        v = v / 1e3
    return i, v


def classify_table(p: Path):
    t = read_table(p)
    if t is None:
        return None
    cols, arr = t
    ti, ts = ts_column(cols, arr)
    c = " ".join(cols)
    info = {"file": str(p), "cols": cols, "rows": int(arr.shape[0]), "ts_col": ti}
    if ts is not None and len(ts) > 2:
        dt = np.diff(ts)
        info.update(t0=float(ts[0]), t1=float(ts[-1]), rate=float(1 / np.median(dt)) if np.median(dt) > 0 else 0)
    others = [x for j, x in enumerate(cols) if j != ti]
    # 时间戳表：只有 帧号 / 时间 / 文件名
    if ti is not None and all(re.search(r"frame|index|idx|file|name|path|^c\d+$", x) for x in others):
        return dict(info, kind="timestamps", ts=ts.tolist())
    if re.search(r"acc", c) and re.search(r"gyr|gyro|ang", c):
        return dict(info, kind="imu", ts=ts.tolist() if ts is not None else None, data=_cols(arr, cols, r"acc|gyr|gyro"))
    if re.search(r"(^|_)q?w($|\s)|qw", c) and re.search(r"(^|_)t?x|tx|pos", c):
        prefix = sorted({re.sub(r"_?(tx|ty|tz|qx|qy|qz|qw|x|y|z)$", "", x) for x in others if re.search(r"(tx|ty|tz|qx|qy|qz|qw)$", x)})
        return dict(info, kind="pose", parts=[pp for pp in prefix if pp], ts=ts.tolist() if ts is not None else None,
                    data=_cols(arr, cols, r"(tx|ty|tz|qx|qy|qz|qw)$"))
    if re.search(r"thumb|index_tip|pinky|kp_?\d+|keypoint", c):
        return dict(info, kind="hand_kp_table")
    kinds = {}
    for j, x in enumerate(cols):
        if j == ti:
            continue
        if re.search(r"gripper|claw|jaw", x):
            kinds.setdefault("gripper", []).append(j)
        elif re.search(r"finger|hand_j|dex|tactile|touch", x):
            kinds.setdefault("dex", []).append(j)
        elif re.search(r"force|wrench|torque|^f[xyz]$|^t[xyz]$", x):
            kinds.setdefault("ft", []).append(j)
        elif re.search(r"action", x):
            kinds.setdefault("action", []).append(j)
        elif re.search(r"joint|^q_?\d|_q\d|^j\d|qpos|position|angle", x):
            kinds.setdefault("joints", []).append(j)
    if kinds:
        return dict(info, kind="robot", parts={k: [cols[j] for j in v] for k, v in kinds.items()},
                    ts=ts.tolist() if ts is not None else None, data={cols[j]: arr[:, j].tolist() for v in kinds.values() for j in v})
    return dict(info, kind="table")


def _cols(arr, cols, pat):
    return {c: arr[:, j].tolist() for j, c in enumerate(cols) if re.search(pat, c)}


def load_yaml(p):
    import yaml
    txt = Path(p).read_text(encoding="utf-8", errors="ignore")
    txt = "\n".join(l for l in txt.splitlines() if not l.startswith("%YAML"))  # 有些设备写的 '%YAML: 1.0' 是非法指令
    return yaml.safe_load(txt)


def find_calib(obj, source):
    """在任意嵌套结构里找相机内参、外参。返回 {'cams':[...], 'extr':[...], 'imu':{...}}"""
    out = {"cams": [], "extr": [], "imu": {}}

    def walk_(o, path):
        if isinstance(o, dict):
            keys = {k.lower(): k for k in o}
            intr = None
            if "intrinsics" in keys and isinstance(o[keys["intrinsics"]], list) and len(o[keys["intrinsics"]]) >= 4:
                fx, fy, cx, cy = o[keys["intrinsics"]][:4]
                intr = dict(fx=fx, fy=fy, cx=cx, cy=cy)
            elif "fx" in keys and "fy" in keys:
                g = lambda *n: next((o[keys[k]] for k in n if k in keys), None)
                intr = dict(fx=g("fx"), fy=g("fy"), cx=g("cx", "ppx", "u0"), cy=g("cy", "ppy", "v0"))
            elif "camera_matrix" in keys or "k" in keys:
                m = o.get(keys.get("camera_matrix") or keys.get("k"))
                if isinstance(m, dict):
                    m = m.get("data")
                if isinstance(m, list):
                    f = np.array(m, dtype=float).flatten()
                    if f.size == 9:
                        intr = dict(fx=f[0], fy=f[4], cx=f[2], cy=f[5])
            if intr and all(v is not None for v in intr.values()):
                dist = None
                for dk in ("distortion_coeffs", "dist", "distortion", "d"):
                    if dk in keys and isinstance(o[keys[dk]], list):
                        dist = o[keys[dk]]
                if dist is None and "k1" in keys:
                    g = lambda n: float(o.get(keys.get(n, ""), 0) or 0)
                    dist = [g("k1"), g("k2"), g("p1"), g("p2"), g("k3")]
                res = o.get(keys.get("resolution", ""), None)
                out["cams"].append(dict(name=path or "camera", source=source, dist=dist,
                                        w=res[0] if isinstance(res, list) else o.get(keys.get("width", ""), None),
                                        h=res[1] if isinstance(res, list) else o.get(keys.get("height", ""), None),
                                        **{k: float(v) for k, v in intr.items()}))
            for k, v in o.items():
                kl = k.lower()
                if re.match(r"^t_\w+|extrinsic", kl) and isinstance(v, (list, dict)):
                    out["extr"].append(dict(name=f"{path}.{k}" if path else k, source=source, value=v))
                if kl in ("rate_hz", "time_offset_s", "time_offset") and path.startswith("imu"):
                    out["imu"][kl] = v
                walk_(v, f"{path}.{k}" if path else k)
        elif isinstance(o, list):
            for i, v in enumerate(o[:50]):
                walk_(v, f"{path}[{i}]")

    walk_(obj, "")
    return out


def classify_json(p: Path):
    try:
        size = p.stat().st_size
        if size > 400e6:
            return {"kind": "json_big", "file": str(p)}
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {"kind": "bad", "file": str(p), "err": "JSON 解析失败"}
    info = {"file": str(p)}
    # 手部关键点：frames -> {时间戳: {hands:[{keypoints_3d...}]}}
    if isinstance(obj, dict) and isinstance(obj.get("frames"), (dict, list)):
        fr = obj["frames"]
        first = next(iter(fr.values())) if isinstance(fr, dict) and fr else (fr[0] if fr else None)
        txt = json.dumps(first)[:4000] if first is not None else ""
        if re.search(r"keypoints?_3d|thumb_tip|index_tip|mano", txt):
            return dict(info, kind="hand_kp", n=len(fr), meta=obj.get("metadata"), quality=obj.get("quality_summary"))
    cal = find_calib(obj, p.name)
    out = dict(info, kind="json")
    if cal["cams"] or cal["extr"]:
        out.update(kind="calib", calib=cal)
    if isinstance(obj, dict):
        name = next((obj[k] for k in ("task", "task_name", "instruction", "language_instruction", "name") if isinstance(obj.get(k), str) and obj.get(k).strip()), None)
        if name and re.search(r"task|info|meta|episode", p.stem.lower()):
            out["task"] = {"name": name, "steps": obj.get("steps") or [], "source": p.name}
        if obj.get("robot_type"):
            out["robot_type"] = obj["robot_type"]
        if isinstance(obj.get("items"), list) and any(isinstance(i, dict) and "fps" in i for i in obj["items"]):
            out["streams_meta"] = obj["items"]
    return out


# ============================================================ 3. 组装录制 ======
def build_recordings(root: Path, tree):
    """把流和旁边的文件归成录制。规则：
       - 图片序列文件夹本身是一个流，归到它的上级目录
       - 一个目录里的流按“去掉 rgb/depth 等角色词后的名字”分组；只有一组就整个目录是一条录制
       - 没有流的子目录（如 camera_params）归到最近的上级录制"""
    seq_dirs = {}
    for d, files in tree.items():
        ok, imgs = is_seq_dir(files)
        if ok and d != root:
            seq_dirs[d] = imgs
    rec_of_dir = {}
    recs = []
    for d, files in sorted(tree.items(), key=lambda kv: len(kv[0].parts)):
        if d in seq_dirs:
            continue
        streams = [("video", d / f) for f in files if Path(f).suffix.lower() in media.VIDEO_EXT]
        streams += [("seq", sd) for sd in seq_dirs if sd.parent == d]
        if not streams:
            continue
        groups = {}
        for s in streams:
            groups.setdefault(base_key(s[1].name), []).append(s)
        others = [f for f in files if Path(f).suffix.lower() not in media.VIDEO_EXT]
        if len(groups) == 1 or (others and len(groups) <= 3 and len(streams) <= 6):
            r = {"dir": d, "streams": streams, "files": [d / f for f in others], "name": d.name if d != root else root.name}
            recs.append(r)
            rec_of_dir[d] = r
        else:  # 一个目录里很多独立视频：每个是一条录制，同名前缀的文件跟着走
            for k, ss in groups.items():
                stem = ss[0][1].stem
                att = [d / f for f in others if Path(f).stem.lower().startswith(k.split("_")[0])]
                recs.append({"dir": d, "streams": ss, "files": att, "name": stem, "flat": True})
    attached = {f for r in recs for f in r["files"]} | {s[1] for r in recs for s in r["streams"]}
    batch_docs, unknown = [], []
    for d, files in tree.items():
        if d in seq_dirs:
            continue
        owner = None
        if d not in rec_of_dir:
            for anc in [d, *d.parents]:
                if anc in rec_of_dir:
                    owner = rec_of_dir[anc]
                    break
        for f in files:
            p = d / f
            if p in attached:
                continue
            if owner is not None:
                owner["files"].append(p)
            elif Path(f).suffix.lower() in DOC_EXT:
                batch_docs.append(p)
            else:
                unknown.append(p)
    return recs, seq_dirs, batch_docs, unknown


# ============================================================ 4. 分析一条录制 ====
def analyze(rec, seq_dirs, root):
    r = {"name": rec["name"], "rel": str(rec["dir"].relative_to(root)) if rec["dir"] != root else ".",
         "streams": [], "signals": [], "calib": {"cams": [], "extr": [], "imu": {}}, "task": None, "robot": {},
         "audio": [], "docs": [], "unknown": [], "issues": [], "checks": {}, "files_n": 0, "bytes": 0}
    ts_tables = {}
    meta_items = []
    for p in rec["files"]:
        ext = p.suffix.lower()
        r["files_n"] += 1
        r["bytes"] += _size(p)
        if ext in (".csv", ".txt", ".tsv"):
            t = classify_table(p)
            if t is None:
                if ext == ".txt":
                    r["docs"].append(p.name)
                continue
            if t["kind"] == "timestamps":
                ts_tables[p.stem.lower()] = t
            elif t["kind"] in ("imu", "pose", "robot", "hand_kp_table"):
                r["signals"].append(t)
            else:
                r["docs"].append(p.name)
        elif ext == ".json":
            j = classify_json(p)
            if j["kind"] == "hand_kp":
                r["signals"].append(j)
            elif j["kind"] == "bad":
                r["issues"].append(["warn", "文件损坏", f"{p.name}：JSON 无法解析。<br><b>处理：</b>该文件被忽略"])
            if j.get("calib"):
                _merge_calib(r["calib"], j["calib"])
            if j.get("task"):
                r["task"] = j["task"]
            if j.get("robot_type"):
                r["robot"]["type"] = j["robot_type"]
            if j.get("streams_meta"):
                meta_items = j["streams_meta"]
        elif ext in (".yaml", ".yml"):
            try:
                _merge_calib(r["calib"], find_calib(load_yaml(p), p.name))
            except Exception:
                r["issues"].append(["warn", "标定文件无法解析", p.name])
        elif ext == ".urdf":
            r["robot"]["urdf"] = str(p)
        elif ext in AUDIO_EXT:
            r["audio"].append(p.name)
        elif ext in UNSUPPORTED:
            r["unknown"].append(f"{p.name}（{UNSUPPORTED[ext]}）")
        elif ext in DOC_EXT or ext in media.IMG_EXT:
            r["docs"].append(p.name)
        else:
            r["unknown"].append(p.name)

    # ---- 流 ----
    for typ, p in rec["streams"]:
        s = {"name": p.stem if typ == "video" else p.name, "src": typ, "path": str(p)}
        if typ == "video":
            pr = media.probe_video(p)
            r["bytes"] += _size(p)
            r["files_n"] += 1
            if pr is None or not pr["ok"]:
                r["issues"].append(["block", "视频无法解码", f"{p.name}<br><b>处理：</b>检查文件是否完整，或转成 H.264 MP4"])
                continue
            s.update(n=pr["n"], fps=pr["fps"], w=pr["w"], h=pr["h"], codec=pr["codec"])
            s["kind"] = "depth" if re.search(r"depth|深度", p.name.lower()) or pr.get("gray") else "rgb"
            if s["kind"] == "depth" and media.is_depth16(p):
                s["dtype"] = "uint16"   # 16 位无损深度视频：毫米值
            s["ts_src"] = "fps"
        else:
            files = seq_dirs[p]
            s["files"] = files
            s["n"] = len(files)
            r["files_n"] += len(files)
            r["bytes"] += sum(_size(p / f) for f in files[:: max(1, len(files) // 20)]) * max(1, len(files) // 20)
            img = media.imread(p / files[0])
            if img is None:
                r["issues"].append(["block", "图片无法读取", f"{p.name}/{files[0]}"])
                continue
            s["h"], s["w"] = img.shape[:2]
            s["dtype"] = str(img.dtype)
            s["kind"] = "depth" if (img.dtype == np.uint16 and img.ndim == 2) or "depth" in p.name.lower() else "rgb"
            tsn = [_isnum(Path(f).stem) for f in files]
            if all(tsn):
                s["ts"] = [float(Path(f).stem) for f in files]
                s["ts_src"] = "filenames"
            s["fps"] = None
        # 时间戳表：rgb_head.mp4 ↔ rgb_head.csv
        t = ts_tables.get(s["name"].lower())
        if t is not None:
            if len(t["ts"]) == s["n"]:
                s["ts"] = t["ts"]
                s["ts_src"] = Path(t["file"]).name
            else:
                r["issues"].append(["warn", "时间戳条数与帧数不符", f"{Path(t['file']).name}：{len(t['ts'])} 行，{s['name']}：{s['n']} 帧。<br><b>处理：</b>按较短者对齐，多出的部分丢弃"])
                m = min(len(t["ts"]), s["n"])
                s["ts"] = t["ts"][:m]
                s["n"] = m
                s["ts_src"] = Path(t["file"]).name
        for it in meta_items:
            if isinstance(it, dict) and it.get("name", "").lower() == s["name"].lower() and it.get("fps") and not s.get("fps"):
                s["fps"] = float(it["fps"])
        if s.get("ts") and len(s["ts"]) > 2:
            dt = np.diff(s["ts"])
            s["fps"] = s.get("fps") or float(1 / np.median(dt))
        s["fps"] = s.get("fps") or 30.0
        s["duration"] = (s["n"] - 1) / s["fps"] if not s.get("ts") else float(s["ts"][-1] - s["ts"][0])
        r["streams"].append(s)
    r["duration"] = max([s["duration"] for s in r["streams"]] or [0])
    return r


def _merge_calib(dst, src):
    dst["cams"] += src["cams"]
    dst["extr"] += src["extr"]
    dst["imu"].update(src["imu"])


def _size(p):
    try:
        return p.stat().st_size
    except Exception:
        return 0


# ============================================================ 5. 检查 ===========
def check_time(r):
    """时间戳：单调、丢帧、是否等间隔（推算值）、两路速率差"""
    info = {}
    abs_ts = []
    for s in r["streams"]:
        if not s.get("ts"):
            s["sync"] = "none"
            continue
        t = np.asarray(s["ts"])
        dt = np.diff(t)
        med = float(np.median(dt))
        nonmono = int((dt <= 0).sum())
        drops = int((dt > 1.5 * med).sum()) if med > 0 else 0
        jitter = float(dt.std() * 1000)
        s["ts_stats"] = dict(interval_ms=med * 1000, jitter_ms=jitter, drops=drops, nonmono=nonmono)
        s["sync"] = "nominal" if jitter < 0.01 else "device"
        if nonmono:
            r["issues"].append(["warn", "时间戳倒退", f"{s['name']}：{nonmono} 处时间戳不递增。<br><b>处理：</b>按帧号重排"])
        if drops:
            r["issues"].append(["warn", f"{s['name']} 丢帧 {drops} 处", f"间隔超过正常 1.5 倍的有 {drops} 处，最长 {dt.max()*1000:.0f} ms。<br><b>处理：</b>时间轴上标出，导出时可跳过"])
        if t[0] > 1e8:
            abs_ts.append(s)
    noms = [s for s in r["streams"] if s.get("sync") == "nominal"]
    if noms:
        names = "、".join(s["name"] for s in noms)
        r["issues"].append(["warn", "时间戳为推算值", f"{names} 的帧间隔恒定（抖动 < 0.01 ms），说明时间戳是按帧率算出来的，不是真实采样时刻。<br><b>处理：</b>用画面运动互相关估计两路之间的偏移"])
    # 两路速率差
    if len(abs_ts) >= 2:
        a, b = abs_ts[0], abs_ts[1]
        ra, rb = a["ts_stats"]["interval_ms"], b["ts_stats"]["interval_ms"]
        ppm = abs(ra - rb) / ra * 1e6
        info["rate_ppm"] = ppm
        if ppm > 100:
            r["issues"].append(["info", f"两路速率差 {ppm:.0f} ppm",
                                f"{a['name']} 帧间隔 {ra:.3f} ms，{b['name']} {rb:.3f} ms。按帧号配对 {r['duration']:.0f} 秒后会差 {ppm*r['duration']/1000:.1f} ms。<br><b>处理：</b>按时间戳配对，不按帧号"])
    r["checks"]["time"] = info


def check_signals(r):
    for sg in r["signals"]:
        if sg["kind"] == "imu" and sg.get("data"):
            acc = [np.asarray(v) for k, v in sg["data"].items() if "acc" in k][:3]
            if len(acc) == 3:
                g = np.sqrt(sum(a ** 2 for a in acc))
                sg["gravity"] = float(np.nanmedian(g))
                if not 8.5 < sg["gravity"] < 11:
                    r["issues"].append(["warn", "IMU 量程/单位异常", f"静止时加速度模长 {sg['gravity']:.2f}，应约 9.8 m/s²"])
        if sg["kind"] == "pose" and sg.get("data"):
            qs = [np.asarray(sg["data"][k]) for k in sg["data"] if re.search(r"q[xyzw]$", k)][:4]
            if len(qs) == 4:
                nrm = np.sqrt(sum(q ** 2 for q in qs))
                bad = float(np.mean(np.abs(nrm - 1) > 0.01))
                if bad > 0.05:
                    r["issues"].append(["warn", "位姿四元数未归一化", f"{bad:.0%} 的四元数模长偏离 1。<br><b>处理：</b>自动归一化"])
        if sg["kind"] == "robot" and sg.get("data"):
            idle = [k for k, v in sg["data"].items() if np.nanstd(v) < 1e-6]
            sg["idle"] = idle
        if sg["kind"] == "hand_kp" and sg.get("quality"):
            qq = sg["quality"]
            n = qq.get("n_frames_total") or sg["n"]
            sg["coverage"] = (qq.get("n_frames_with_hands") or 0) / max(n, 1)
            sg["low_conf"] = qq.get("low_conf_ratio")
    # 厂商关键点里被剔除的帧
    for sg in r["signals"]:
        if sg["kind"] == "hand_kp":
            try:
                obj = json.loads(Path(sg["file"]).read_text(encoding="utf-8"))
                fr = obj["frames"]
                ex = [k for k, v in fr.items() if isinstance(v, dict) and v.get("excluded")]
                sg["excluded"] = len(ex)
                if ex:
                    r["issues"].append(["info", f"关键点剔除 {len(ex)} 帧", f"厂商在 {len(ex)} 帧上标了 excluded（{len(ex)/len(fr):.0%}）。<br><b>处理：</b>这些帧不参与轨迹，时间轴上标灰"])
            except Exception:
                pass


def check_calib(r):
    cams = r["calib"]["cams"]
    if not cams:
        return
    rgb = next((s for s in r["streams"] if s["kind"] == "rgb"), None)
    # 内参和分辨率是否匹配
    for c in cams:
        if rgb and c.get("w") and c.get("h") and (int(c["w"]), int(c["h"])) != (rgb["w"], rgb["h"]):
            c["res_mismatch"] = True
        if rgb and (abs(c["cx"] - rgb["w"] / 2) > rgb["w"] * 0.15 or abs(c["cy"] - rgb["h"] / 2) > rgb["h"] * 0.15):
            c["odd_center"] = True
    # 多个来源说法不一致
    groups = {}
    for c in cams:
        key = "depth" if re.search(r"depth|cam1", c["name"].lower()) else "rgb"
        groups.setdefault(key, []).append(c)
    for k, cs in groups.items():
        fxs = [c["fx"] for c in cs]
        if len(cs) >= 2 and (max(fxs) - min(fxs)) / max(fxs) > 0.01:
            desc = "；".join(f"{c['source']} 的 {c['name']}：fx={c['fx']:.1f}" for c in cs)
            r["issues"].append(["warn", "标定自相矛盾", f"{'深度' if k=='depth' else '彩色'}相机内参有 {len(cs)} 个来源，数值不一致：{desc}。<br><b>处理：</b>以投影实测贴合度为准，选用误差小的一组"])
    ex = r["calib"]["extr"]
    mats = []
    for e in ex:
        v = np.array(e["value"], dtype=float).flatten() if isinstance(e["value"], list) else None
        if v is not None and v.size == 16:
            R = v.reshape(4, 4)[:3, :3]
            if abs(np.linalg.det(R) - 1) > 0.01 or np.abs(R @ R.T - np.eye(3)).max() > 0.01:
                r["issues"].append(["warn", "外参不是合法旋转", f"{e['name']}：旋转矩阵不正交"])
            mats.append((e["name"], R))
    for i in range(len(mats)):
        for j in range(i + 1, len(mats)):
            if np.abs(mats[i][1] - mats[j][1]).max() < 1e-9:
                r["issues"].append(["info", "两组外参旋转完全相同", f"{mats[i][0]} 与 {mats[j][0]} 的旋转逐位相同，可能是复制的默认值。<br><b>处理：</b>仅作提醒"])


def motion_energy(s: "media.Stream", times):
    """在给定时刻（相对秒）取帧，算相邻帧差 → 画面运动能量曲线"""
    prev, out = None, []
    t0 = s.ts[0]
    for t in times:
        img = s.read(s.index_at(t0 + t))
        if img is None:
            out.append(np.nan)
            continue
        if s.kind == "depth":
            d = media.depth_to_m(img)
            g = media.small_gray(np.clip(d, 0, 4) * 60)
        else:
            g = media.small_gray(img)
        if prev is not None:
            if s.kind == "depth":
                m = (g > 0) & (prev > 0)
                out.append(float(np.abs(g - prev)[m].mean()) if m.any() else np.nan)
            else:
                out.append(float(np.abs(g - prev).mean()))
        prev = g
    return np.array(out)


def check_depth(r, wd):
    """深度可用性 + 彩色与深度是否同一次录制"""
    rgbs = [s for s in r["streams"] if s["kind"] == "rgb"]
    deps = [s for s in r["streams"] if s["kind"] == "depth"]
    for d in deps:
        st = media.Stream(d)
        holes, meds = [], []
        for i in np.linspace(0, d["n"] - 1, 6).astype(int):
            m = media.depth_to_m(st.read(i))
            if m is None:
                continue
            holes.append(float((m <= 0).mean()))
            v = m[m > 0]
            meds.append(float(np.median(v)) if v.size else 0)
        st.close()
        d["holes"] = float(np.mean(holes)) if holes else None
        d["median_m"] = float(np.median(meds)) if meds else None
        d["unit"] = "mm(uint16)" if d.get("dtype") == "uint16" else ("相对值" if d["src"] == "video" else d.get("dtype"))
        if d.get("dtype") == "uint16" and d["median_m"] is not None and not (0.1 < d["median_m"] < 6):
            r["issues"].append(["warn", "深度数值异常", f"{d['name']}：画面中位深度 {d['median_m']:.2f} m，不像正常距离（可能单位不是毫米）。<br><b>处理：</b>三维相关步骤改用二维"])
        if d["holes"] is not None and d["holes"] > 0.4:
            r["issues"].append(["warn", f"深度空洞 {d['holes']:.0%}", "无效像素比例偏高（反光、太近或太远）。<br><b>处理：</b>空洞处不参与三维计算"])
        if d["src"] == "video" and d.get("dtype") is None:
            r["issues"].append(["info", "深度是 8 位视频", "只有相对深度，没有真实距离。<br><b>处理：</b>三维相关步骤降级"])
    if not rgbs or not deps:
        return
    a, b = rgbs[0], deps[0]
    ev = pair_evidence(r, a, b, wd)
    r["checks"]["pair"] = ev
    if ev["mismatch"]:
        r["mismatch"] = {"rgb": a["name"], "depth": b["name"], **ev}
        r["issues"].insert(0, ["block", "彩色与深度不属于同一录制", "多项交叉核对异常，见下方对比"])


def energy_cache(r, s, wd, dur):
    """运动能量曲线：4 Hz 采样，最多 120 个点，缓存到工作区"""
    fn = wd / f"energy_{re.sub(r'[^A-Za-z0-9_]', '_', s['name'])}.npy"
    if fn.exists():
        return np.load(fn)
    st = media.Stream(s)
    rate = min(4.0, 120 / max(dur, 1))
    times = np.arange(0, max(dur, 0.5), 1 / rate)
    e = motion_energy(st, times)
    st.close()
    np.save(fn, e)
    return e


def corr_lag(a, b, max_lag):
    a, b = np.nan_to_num(a), np.nan_to_num(b)
    n = min(len(a), len(b))
    if n < 6:
        return 0.0, 0
    a, b = a[:n], b[:n]
    best, lag = -1.0, 0
    for L in range(-max_lag, max_lag + 1):
        x = a[max(0, L):n + min(0, L)]
        y = b[max(0, -L):n - max(0, L)]
        if len(x) < 6 or x.std() < 1e-9 or y.std() < 1e-9:
            continue
        c = float(np.corrcoef(x, y)[0, 1])
        if c > best:
            best, lag = c, L
    return best, lag


def pair_evidence(r, a, b, wd, other_wd=None):
    dur = min(a["duration"], b["duration"]) or max(a["duration"], b["duration"])
    ea = energy_cache(r, a, wd, max(a["duration"], 1))
    eb = energy_cache(r, b, other_wd or wd, max(b["duration"], 1))
    rate = min(4.0, 120 / max(a["duration"], 1))
    rate_b = min(4.0, 120 / max(b["duration"], 1))
    if abs(rate - rate_b) > 1e-6:  # 两条曲线采样率不同：插值到同一网格
        tb = np.arange(len(eb)) / rate_b
        eb = np.interp(np.arange(len(ea)) / rate, tb, np.nan_to_num(eb))
    c, lag = corr_lag(ea, eb, int(rate * 1.0))
    ev = {"corr": round(c, 3), "lag_s": round(lag / rate, 2), "dur_a": round(a["duration"], 2), "dur_b": round(b["duration"], 2),
          "dur_diff": round(abs(a["duration"] - b["duration"]), 2), "spark_a": _spark(ea), "spark_b": _spark(eb)}
    if a.get("ts") and b.get("ts") and a["ts"][0] > 1e8 and b["ts"][0] > 1e8:
        lo, hi = max(a["ts"][0], b["ts"][0]), min(a["ts"][-1], b["ts"][-1])
        ev["overlap"] = round(max(0.0, hi - lo) / max(1e-6, min(a["duration"], b["duration"])), 3)
    bad = []
    if ev.get("overlap") is not None and ev["overlap"] < 0.5:
        bad.append("overlap")
    if ev["dur_diff"] > max(1.0, 0.05 * max(ev["dur_a"], ev["dur_b"])):
        bad.append("dur")
    if c < 0.25:
        bad.append("corr")
    ev["bad"] = bad
    ev["mismatch"] = "overlap" in bad or len(bad) >= 2
    return ev


def _spark(e):
    e = np.nan_to_num(np.asarray(e, dtype=float))
    if e.size == 0 or e.max() <= 0:
        return []
    idx = np.linspace(0, len(e) - 1, min(40, len(e))).astype(int)
    v = e[idx] / e.max()
    return [round(float(x), 3) for x in v]


# ============================================================ 6. 只有视频：人手初判 ==
def thumbs(r, wd):
    rgb = next((s for s in r["streams"] if s["kind"] == "rgb"), None)
    if rgb is None:
        return
    st = media.Stream(rgb)
    for i, k in enumerate(np.linspace(0, rgb["n"] - 1, 4).astype(int)):
        img = st.read(k)
        if img is not None:
            h, w = img.shape[:2]
            media.imwrite(wd / f"pre_{i}.jpg", __import__("cv2").resize(img, (160, int(h * 160 / w))))
    st.close()


def hand_guess(r):
    rgb = next((s for s in r["streams"] if s["kind"] == "rgb"), None)
    if rgb is None:
        return None
    from . import hands
    st = media.Stream(rgb)
    idx = np.linspace(0, rgb["n"] - 1, min(24, rgb["n"])).astype(int)
    frames = [st.read(i) for i in idx]
    st.close()
    return hands.judge([f for f in frames if f is not None])


# ============================================================ 7. 画像 / 部件 / 分组 ==
DIMS = ["A", "T", "S", "V", "L", "R"]


def profile(r):
    kinds = {s["kind"] for s in r["signals"]}
    robot = [s for s in r["signals"] if s["kind"] == "robot"]
    parts = set().union(*[set(s["parts"]) for s in robot]) if robot else set()
    A = 3 if ("action" in parts and parts & {"joints", "gripper", "dex"}) else 2 if parts & {"joints", "gripper", "dex"} else 1 if kinds & {"hand_kp", "hand_kp_table"} else 0
    syncs = [s.get("sync", "none") for s in r["streams"]]
    if not syncs or all(x == "none" for x in syncs):
        T = 0
    elif all(x == "device" for x in syncs):
        T = 2
    else:
        T = 1
    cams = r["calib"]["cams"]
    S = 0
    if cams:
        S = 2
        if r["calib"]["extr"] and A >= 2:
            S = 3
    nrgb = sum(1 for s in r["streams"] if s["kind"] == "rgb")
    ndep = sum(1 for s in r["streams"] if s["kind"] == "depth")
    V = 0 if not nrgb and not ndep else 3 if (nrgb >= 2 and ndep) else 2 if ndep else 1
    t = r.get("task") or {}
    L = 2 if t.get("steps") else 1 if t.get("name") else 0
    R = 2 if r["robot"].get("urdf") else 1 if r["robot"].get("type") else 0
    return dict(A=A, T=T, S=S, V=V, L=L, R=R)


def components(r):
    body = []
    for sg in r["signals"]:
        fn = Path(sg["file"]).name
        if sg["kind"] == "hand_kp":
            body.append(dict(k="human", n="双手", d="已有逐帧三维手部关键点（21 点）", src=f"{fn} 的字段结构（keypoints_3d / mano）"))
        elif sg["kind"] == "hand_kp_table":
            body.append(dict(k="human", n="手部", d="表格形式的手部关键点", src=f"{fn} 的列名（thumb / index …）"))
        elif sg["kind"] == "robot":
            p = sg["parts"]
            idle = set(sg.get("idle") or [])
            if "joints" in p:
                act = [c for c in p["joints"] if c not in idle]
                body.append(dict(k="arm", n="机械臂", d=f"{len(p['joints'])} 个关节通道" + (f"，{len(p['joints'])-len(act)} 个全程不动" if len(act) < len(p["joints"]) else ""),
                                 src=f"{fn} 的列名：{', '.join(p['joints'][:4])}…", idle=not act))
            if "gripper" in p:
                body.append(dict(k="gripper", n="夹爪", d="开合读数", src=f"{fn} 的列名含 gripper"))
            if "dex" in p:
                body.append(dict(k="dex", n="灵巧手", d=f"{len(p['dex'])} 个手指 / 触觉通道", src=f"{fn} 的列名含 finger / tactile", confirm=True))
            if "ft" in p:
                body.append(dict(k="ft", n="力传感器", d="力 / 力矩", src=f"{fn} 的列名含 force / wrench"))
        elif sg["kind"] == "pose":
            nm = "、".join(sg.get("parts") or ["位姿"])
            body.append(dict(k="pose", n="头部位姿" if "head" in nm else "位姿", d=f"6 自由度轨迹（{nm}）", src=f"{fn} 的列名 tx…qw"))
        elif sg["kind"] == "imu":
            body.append(dict(k="imu", n="IMU", d=f"加速度 + 陀螺仪，约 {sg.get('rate', 0):.0f} Hz", src=f"{fn} 的列名 acc / gyr"))
    if not any(c["k"] in ("human", "arm", "gripper", "dex") for c in body):
        g = r.get("hand_guess")
        if g:
            if g["verdict"] == "human":
                body.insert(0, dict(k="human", n="人手", d="只在画面里，没有关键点", none=True, confirm=True,
                                    src=f"人手检测：检出率 {g['rate']:.0%}、平均置信度 {g['score']:.2f}。只是初判，需要你确认"))
            else:
                body.insert(0, dict(k="unknown", n="末端未知", d=g["label"], none=True, confirm=True,
                                    src=f"人手检测：检出率 {g['rate']:.0%}。可能是夹爪/灵巧手，或末端不在画面"))
    return body


def group_key(r):
    if r.get("mismatch"):
        return "mismatch:" + r["id"]
    ks = sorted({c["k"] for c in r["body"] if not c.get("idle")})
    v = r["profile"]["V"]
    return "|".join(ks) + f"|V{v}|" + ("pose" if any(c["k"] == "pose" for c in r["body"]) else "")


def group_name(r):
    ks = {c["k"] for c in r["body"] if not c.get("idle")}
    if ks & {"arm", "gripper", "dex"}:
        n = "机械臂" if "arm" in ks else "机器人"
        if "dex" in ks:
            n += " + 灵巧手"
        elif "gripper" in ks:
            n += " + 平行夹爪"
        return n
    has_kp = any(c["k"] == "human" and not c.get("none") for c in r["body"])
    pre = "头戴第一视角" if "pose" in ks or re.search(r"head|ego", " ".join(s["name"] for s in r["streams"])) else ""
    vis = {1: "彩色视频", 2: "彩色 + 深度", 3: "多视角 + 深度"}.get(r["profile"]["V"], "")
    if has_kp:
        return (pre or "人手") + " · 带手部关键点"
    tail = " · 画面中有人手" if any(c["k"] == "human" for c in r["body"]) else " · 末端未知" if any(c["k"] == "unknown" for c in r["body"]) else ""
    return (pre + " · " if pre else "") + "仅" + vis + tail


# ============================================================ 8. 总入口 ==========
def scan(batch_id, root: Path, report):
    t0 = time.time()
    report(0.02, "遍历文件夹")
    tree = walk(root)
    nfiles = sum(len(v) for v in tree.values())
    recs, seq_dirs, batch_docs, unknown = build_recordings(root, tree)
    report(0.08, f"找到 {nfiles} 个文件、{len(recs)} 条录制")
    out = []
    for i, rec in enumerate(recs):
        rid = f"{batch_id}_{i+1:03d}"
        report(0.08 + 0.8 * i / max(len(recs), 1), f"预检 {rec['name']}")
        r = analyze(rec, seq_dirs, root)
        r["id"] = rid
        wd = work_dir(batch_id, rid)
        try:
            check_time(r)
            check_signals(r)
            check_calib(r)
            check_depth(r, wd)
            thumbs(r, wd)
            if not any(sg["kind"] in ("hand_kp", "hand_kp_table", "robot") for sg in r["signals"]) and r["streams"]:
                r["hand_guess"] = hand_guess(r)
        except Exception as e:  # 单条录制出错不影响其他
            r["issues"].append(["warn", "预检出错", f"{type(e).__name__}: {e}"])
        if r["audio"]:
            r["issues"].append(["info", "含音频", f"{', '.join(r['audio'])}<br><b>处理：</b>暂不处理，导出时原样保留"])
        if r["unknown"]:
            r["issues"].append(["info", f"{len(r['unknown'])} 个文件未识别", "、".join(r["unknown"][:6]) + "<br><b>处理：</b>忽略；如需要请为该格式开发适配器"])
        if not r.get("task"):
            r["issues"].append(["info", "无任务描述", "<b>处理：</b>由本地大模型生成任务名与分段"])
        r["profile"] = profile(r)
        r["body"] = components(r)
        out.append(r)
    # ---- 错配：给出建议 ----
    report(0.9, "交叉核对彩色与深度")
    suggest_repair(batch_id, out)
    for r in out:
        r["group"] = group_key(r)
        r["group_name"] = group_name(r)
        (work_dir(batch_id, r["id"]) / "rec.json").write_text(jd(r), encoding="utf-8")
    info = {"files": nfiles, "recordings": len(out), "seconds": round(time.time() - t0, 1),
            "docs": [str(p.relative_to(root)) for p in batch_docs][:30],
            "unknown": [str(p.relative_to(root)) for p in unknown][:200], "unknown_n": len(unknown),
            "unknown_bytes": sum(_size(p) for p in unknown)}
    return out, info


def suggest_repair(batch_id, recs):
    bad = [r for r in recs if r.get("mismatch")]
    if not bad:
        return
    deps = [(r, s) for r in recs for s in r["streams"] if s["kind"] == "depth"]
    for r in bad:
        a = next(s for s in r["streams"] if s["kind"] == "rgb")
        best = None
        for r2, s2 in deps:
            if r2["id"] == r["id"]:
                continue
            ev = pair_evidence(r, a, s2, work_dir(batch_id, r["id"]), work_dir(batch_id, r2["id"]))
            score = ev["corr"] - (1 if ev.get("overlap") is not None and ev["overlap"] < 0.5 else 0) - 0.05 * ev["dur_diff"]
            if not ev["mismatch"] and (best is None or score > best[0]):
                best = (score, r2, s2, ev)
        if best:
            _, r2, s2, ev = best
            r["mismatch"]["suggest"] = {"rec": r2["id"], "rec_name": r2["name"], "stream": s2["name"], "path": s2["path"],
                                        "corr": ev["corr"], "overlap": ev.get("overlap"), "dur_diff": ev["dur_diff"]}


def repair(batch_id, r, depth_stream):
    """接受建议：把另一条录制的深度流换进来，重新检查"""
    r["streams"] = [s for s in r["streams"] if s["kind"] != "depth"] + [depth_stream]
    r["issues"] = [i for i in r["issues"] if i[1] != "彩色与深度不属于同一录制"]
    r.pop("mismatch", None)
    wd = work_dir(batch_id, r["id"])
    rgb = next(s for s in r["streams"] if s["kind"] == "rgb")
    ev = pair_evidence(r, rgb, depth_stream, wd)
    r["checks"]["pair"] = ev
    r["repaired"] = {"depth": depth_stream["path"]}
    r["profile"] = profile(r)
    r["group"] = group_key(r)
    r["group_name"] = group_name(r)
    (wd / "rec.json").write_text(jd(r), encoding="utf-8")
    return r
