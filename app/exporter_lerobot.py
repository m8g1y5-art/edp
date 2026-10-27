"""导出为 LeRobot v3.0 数据集（+ 平台扩展目录 edp/）

目录结构
  meta/info.json                     数据集结构：特征名、形状、类型、帧率、路径模板（LeRobot 标准）
  meta/stats.json                    各特征的统计量（归一化用）
  meta/tasks.parquet                 任务文本 → task_index
  meta/subtasks.parquet              分段描述 → subtask_index（逐帧的 subtask_index 指向它）
  meta/episodes/chunk-000/file-000.parquet   每条录制一行：长度、任务、在 data / videos 文件里的位置
  data/chunk-000/file-XXX.parquet    逐帧表：手部关键点（左右手分列）、状态、动作、分段、接触、原始帧号
  videos/observation.images.head/chunk-000/file-XXX.mp4   头戴彩色视频（恒定帧率，逐帧与 data 对齐）
  edp/                               LeRobot 标准里没有位置的内容：分段与接触详情、物体掩码、溯源、字段说明
  raw/                               （可选）原始文件副本

对齐方式：data 里每一帧都有 edp.src_rgb_frame（原始彩色视频帧号）、edp.src_timestamp（原始时间戳，秒）、
edp.depth_frame（对应的原始深度帧号，没有为 -1）。任何一帧都能追回原始文件里的确切位置。
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from . import media
from . import objects as OB
from . import plan as P
from .db import all_settings, jd, jl, work_dir

CODEBASE = "v3.0"
VIDEO_KEY = "observation.images.head"
JOINTS = ["wrist", "thumb_cmc", "thumb_mcp", "thumb_ip", "thumb_tip", "index_mcp", "index_pip", "index_dip", "index_tip",
          "middle_mcp", "middle_pip", "middle_dip", "middle_tip", "ring_mcp", "ring_pip", "ring_dip", "ring_tip",
          "pinky_mcp", "pinky_pip", "pinky_dip", "pinky_tip"]
STATE_J = [0, 4, 8, 12, 16, 20]  # 状态向量里每只手取：手腕 + 5 个指尖
SIDES = ("left", "right")
QS = [0.01, 0.10, 0.50, 0.90, 0.99]


def _rj(p):
    return jl(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else None


# ------------------------------------------------------------------ 依赖 ----
PARQUET_PKGS = ["pyarrow<20", "pandas<3", "numpy<2"]  # 平台环境的 numpy 是 1.x（MediaPipe 需要）；pyarrow 20 起要求 numpy 2


def _parquet_ok():
    """在子进程里试导入，避免一个装坏的 pyarrow 先被加载进平台进程"""
    r = subprocess.run([sys.executable, "-c", "import pandas, pyarrow, pyarrow.parquet"], capture_output=True,
                       creationflags=0x08000000 if sys.platform == "win32" else 0)
    return r.returncode == 0


def _need_parquet(log):
    if not _parquet_ok():
        log("安装导出 LeRobot 需要的 pandas / pyarrow（与本机 numpy 兼容的版本，约 1 分钟）")
        py = [sys.executable, "-m", "pip", "install", "-q"] + PARQUET_PKGS
        if subprocess.run(py + ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"]).returncode != 0:
            subprocess.run(py, check=True)
        if not _parquet_ok():
            raise RuntimeError("pandas / pyarrow 安装后仍无法导入，请把导出目录里的 export_error.txt 发给开发者")
    try:
        import pandas  # noqa
        import pyarrow.parquet  # noqa
    except ImportError:
        raise RuntimeError("已自动换成兼容版本的 pyarrow，但平台进程里还留着旧版本。请关掉平台窗口，重新双击 run.bat，再导出一次")


# ------------------------------------------------------------------ 统计量 ----
def _stats(a):
    """LeRobot 的统计量格式：min/max/mean/std/count + 分位数，逐维"""
    a = np.asarray(a, dtype=np.float64)
    if a.ndim == 1:
        a = a[:, None]
    out = {"min": a.min(0), "max": a.max(0), "mean": a.mean(0), "std": a.std(0), "count": np.array([len(a)])}
    for q in QS:
        out[f"q{int(round(q * 100)):02d}"] = np.quantile(a, q, axis=0)
    return {k: v.tolist() for k, v in out.items()}


def _img_stats(px):
    """图像统计：px 为 N×3 的 [0,1] RGB 像素，结果形状 (3,1,1)"""
    s = _stats(px)
    return {k: (v if k == "count" else [[[x]] for x in v]) for k, v in s.items()}


# ------------------------------------------------------------------ 每条录制的逐帧数据 ----
def _hands_per_frame(r, wd, ts_abs, rgb_ts):
    """每个导出帧、每只手：21 点三维（相机坐标系，米）、21 点二维（像素）、是否有效。
    三维优先用厂商关键点；没有时用平台估计的手腕三维位置（其他关节置 0）"""
    from .pipeline import load_vendor_kp
    n = len(ts_abs)
    out = {s: {"k3": np.zeros((n, 21, 3), np.float32), "k2": np.zeros((n, 21, 2), np.float32), "valid": np.zeros(n, np.int64)} for s in SIDES}
    src3 = "无"
    sg = next((s for s in r["signals"] if s["kind"] == "hand_kp"), None)
    vend = None
    if sg is not None:
        try:
            vend = load_vendor_kp(sg["file"])
        except Exception:
            vend = None
    fps = 1.0 / np.median(np.diff(rgb_ts)) if len(rgb_ts) > 1 else 30.0
    if vend:
        ft = np.array([f["t"] for f in vend])
        for i, t in enumerate(ts_abs):
            j = int(np.argmin(np.abs(ft - t)))
            if abs(ft[j] - t) > 0.5 / fps + 1e-3 or vend[j]["excluded"]:
                continue
            for h in vend[j]["hands"]:
                out[h["side"]]["k3"][i] = h["p3"]
                out[h["side"]]["valid"][i] = 1
        src3 = "厂商三维关键点"
    else:
        h3 = _rj(wd / "hands3d.json")
        if h3 and h3.get("t"):
            ht = np.array(h3["t"], float) + rgb_ts[0]
            for i, t in enumerate(ts_abs):
                j = int(np.argmin(np.abs(ht - t)))
                if abs(ht[j] - t) > 0.2:
                    continue
                for s in SIDES:
                    p = h3[s][j] if j < len(h3[s]) else None
                    if p:
                        out[s]["k3"][i, 0] = p
                        out[s]["valid"][i] = 1
            src3 = "平台估计（只有手腕）"
    h2 = _rj(wd / "hands2d.json")
    if h2:
        F = h2["frames"]
        for i, t in enumerate(ts_abs):
            k = int(np.argmin(np.abs(rgb_ts - t)))
            for side, conf, pts, ex in (F[k] if k < len(F) else []):
                if ex:
                    continue
                arr = np.array([[p[0] if p and p[0] is not None else 0, p[1] if p and p[1] is not None else 0] for p in pts], np.float32)
                out[side]["k2"][i] = arr
    return out, src3, (h2 or {}).get("src", "无")


def _pose_per_frame(r, ts_abs):
    sg = next((s for s in r["signals"] if s["kind"] == "pose"), None)
    if not sg or not sg.get("data") or not sg.get("ts"):
        return None
    d = sg["data"]
    ks = list(d.keys())
    try:
        cols = [np.array(d[next(k for k in ks if k.endswith(suf))], float) for suf in ("tx", "ty", "tz", "qx", "qy", "qz", "qw")]
    except StopIteration:
        return None
    pt = np.array(sg["ts"], float)
    idx = np.clip(np.searchsorted(pt, ts_abs), 0, len(pt) - 1)
    prev = np.clip(idx - 1, 0, len(pt) - 1)
    idx = np.where(np.abs(pt[prev] - ts_abs) < np.abs(pt[idx] - ts_abs), prev, idx)
    return np.stack([c[idx] for c in cols], 1).astype(np.float32)


def _hand_side(h):
    h = str(h or "")
    return "both" if "双" in h else "left" if "左" in h else "right" if "右" in h else "none"


# ------------------------------------------------------------------ 主流程 ----
def export(out: Path, items, opts, log=print):
    """items: [(batch_id, rec dict)]。opts: fps(0 = 自动), action('next'|'none'), raw(bool), depth(bool)"""
    out.mkdir(parents=True, exist_ok=True)
    man = {"name": out.name, "created": time.strftime("%Y-%m-%d %H:%M"), "status": "running", "episodes": [],
           "format": "lerobot-v3.0+edp", "progress": 0}
    (out / "manifest.json").write_text(jd(man), encoding="utf-8")
    try:
        _export(out, items, opts, man, log)
        man["status"] = "done"
    except Exception as e:
        import traceback
        man["status"] = "error"
        man["error"] = f"{type(e).__name__}: {e}"
        (out / "export_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    (out / "manifest.json").write_text(jd(man), encoding="utf-8")


def _export(out, items, opts, man, log):
    _need_parquet(log)
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    rgbs = [next(s for s in r["streams"] if s["kind"] == "rgb") for _, r in items]
    fps = int(opts.get("fps") or 0) or int(round(float(np.median([s.get("fps") or 30 for s in rgbs]))))
    W, H = int(rgbs[0].get("w") or 1280), int(rgbs[0].get("h") or 720)
    act_mode = opts.get("action", "next")
    # 特征集合必须每条录制都一样：只要有一条带头部位姿，就都带上，没有的填 0
    want_pose = any(_pose_per_frame(r, np.array([0.0])) is not None for _, r in items)
    settings = all_settings()

    tasks, subtasks = {}, {}
    ep_rows, all_num, img_px = [], {}, []
    gidx = 0
    for e, (bid, r) in enumerate(items):
        wd = work_dir(bid, r["id"])
        rgb = rgbs[e]
        st = media.Stream(rgb)
        rgb_ts = st.ts.copy()
        dur = float(rgb_ts[-1] - rgb_ts[0]) if len(rgb_ts) > 1 else r["duration"]
        n = int(np.floor(dur * fps + 1e-6)) + 1
        t_rel = np.arange(n) / fps
        ts_abs = rgb_ts[0] + t_rel
        src_idx = np.array([st.index_at(t) for t in ts_abs], np.int64)

        # ---- 视频：按导出帧逐帧取原始帧编码，保证 data 第 k 行 = 视频第 k 帧
        vpath = out / "videos" / VIDEO_KEY / "chunk-000" / f"file-{e:03d}.mp4"
        vpath.parent.mkdir(parents=True, exist_ok=True)
        cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(fps),
               "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "2", "-pix_fmt", "yuv420p", str(vpath)]
        pr = subprocess.Popen(cmd, stdin=subprocess.PIPE, creationflags=0x08000000 if sys.platform == "win32" else 0)
        sample = set(np.linspace(0, n - 1, min(n, 24)).astype(int).tolist())
        for k, i in enumerate(src_idx):
            fr = st.read(int(i))
            if fr is None:
                fr = np.zeros((H, W, 3), np.uint8)
            if fr.shape[1] != W or fr.shape[0] != H:
                fr = cv2.resize(fr, (W, H))
            pr.stdin.write(np.ascontiguousarray(fr).tobytes())
            if k in sample:
                sm = cv2.resize(fr, (64, int(64 * H / W)))[:, :, ::-1].reshape(-1, 3) / 255.0
                img_px.append(sm)
        pr.stdin.close()
        if pr.wait() != 0:
            raise RuntimeError("视频编码失败（ffmpeg）")
        st.close()

        # ---- 逐帧特征
        hands, src3, src2 = _hands_per_frame(r, wd, ts_abs, rgb_ts)
        state = np.concatenate([hands[s]["k3"][:, STATE_J].reshape(n, -1) for s in SIDES], 1).astype(np.float32)
        cols = {
            "observation.state": state,
            "observation.hand.left.keypoints_3d": hands["left"]["k3"].reshape(n, -1),
            "observation.hand.right.keypoints_3d": hands["right"]["k3"].reshape(n, -1),
            "observation.hand.left.keypoints_2d": hands["left"]["k2"].reshape(n, -1),
            "observation.hand.right.keypoints_2d": hands["right"]["k2"].reshape(n, -1),
            "observation.hand.left.valid": hands["left"]["valid"],
            "observation.hand.right.valid": hands["right"]["valid"],
        }
        if want_pose:
            pose = _pose_per_frame(r, ts_abs)
            cols["observation.head_pose"] = pose if pose is not None else np.zeros((n, 7), np.float32)
        if act_mode == "next":
            cols["action"] = np.concatenate([state[1:], state[-1:]], 0)

        # 任务、分段、接触
        seg = _rj(wd / "segments.json") or {}
        task = _rj(wd / "task.json") or {}
        tname = task.get("user_name") or (r.get("task") or {}).get("name") or task.get("name") or task.get("seen") or "未命名任务"
        tasks.setdefault(tname, len(tasks))
        segs = seg.get("segments") or []
        groups = seg.get("groups") or []
        seg_idx = np.full(n, -1, np.int64)
        grp_idx = np.full(n, -1, np.int64)
        sub_idx = np.full(n, -1, np.int64)
        for si, s in enumerate(segs):
            gi = s.get("group") if isinstance(s.get("group"), int) and s.get("group") < len(groups) else None
            # 子任务文本：有大模型分组时用组的描述（更高一层），否则用这一段自己的描述
            txt = (groups[gi].get("title") if gi is not None else None) or s.get("description") or s.get("action") or s.get("verb") or "未标注"
            sub = subtasks.setdefault(txt, len(subtasks))
            m = (t_rel >= s["t0"] - 1e-6) & (t_rel < s["t1"] + (1e-6 if si == len(segs) - 1 else -1e-6))
            seg_idx[m] = si
            grp_idx[m] = gi if gi is not None else si
            sub_idx[m] = sub
        objs = (_rj(wd / "objects.json") or {}).get("objects", [])
        oid = {o["id"]: k for k, o in enumerate(objs)}
        events = (_rj(wd / "contact.json") or {}).get("events", [])
        contact = {s: np.full(n, -1, np.int64) for s in SIDES}
        for ev in sorted(events, key=lambda x: (x.get("moved", False), x["t1"] - x["t0"])):  # 重叠时移动了的、时间长的优先
            if ev["hand"] in contact and ev["obj"] in oid:
                contact[ev["hand"]][(t_rel >= ev["t0"]) & (t_rel <= ev["t1"])] = oid[ev["obj"]]
        if (sub_idx < 0).any():  # 没有分段覆盖的帧：归到“未标注”，保证 subtask_index 总能查到文本
            sub_idx[sub_idx < 0] = subtasks.setdefault("未标注", len(subtasks))
        cols["annotation.segment_index"] = seg_idx
        cols["annotation.group_index"] = grp_idx
        cols["subtask_index"] = sub_idx
        cols["annotation.contact.left"] = contact["left"]
        cols["annotation.contact.right"] = contact["right"]

        # 溯源
        dep = next((s for s in r["streams"] if s["kind"] == "depth"), None)
        sync = _rj(wd / "sync.json") or {}
        if dep:
            dst = media.Stream(dep)
            off = sync.get("depth_t_offset", 0.0)
            dfr = np.array([dst.index_at(dst.ts[0] + t + off) for t in t_rel], np.int64)
        else:
            dfr = np.full(n, -1, np.int64)
        cols["edp.src_rgb_frame"] = src_idx
        cols["edp.src_timestamp"] = rgb_ts[src_idx].astype(np.float64)
        cols["edp.depth_frame"] = dfr

        for k, v in cols.items():
            all_num.setdefault(k, []).append(np.asarray(v))

        # ---- 写 data parquet（一条录制一个文件）
        base = {"timestamp": t_rel.astype(np.float32), "frame_index": np.arange(n, dtype=np.int64),
                "episode_index": np.full(n, e, np.int64), "index": np.arange(gidx, gidx + n, dtype=np.int64),
                "task_index": np.full(n, tasks[tname], np.int64)}
        arrs = {}
        for k, v in {**cols, **base}.items():
            v = np.asarray(v)
            if v.ndim == 2:
                arrs[k] = pa.FixedSizeListArray.from_arrays(pa.array(v.reshape(-1).astype(np.float32)), v.shape[1])
            else:
                arrs[k] = pa.array(v)
        dpath = out / "data" / "chunk-000" / f"file-{e:03d}.parquet"
        dpath.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(arrs), dpath)

        # ---- 平台扩展：分段、接触（左右手分开）、物体、溯源
        ed = out / "edp" / "episodes" / f"episode_{e:06d}"
        ed.mkdir(parents=True, exist_ok=True)
        q = _rj(wd / "quality.json") or {}
        kp = _rj(wd / "kp_verify.json") or {}
        sgs = []
        for si, s in enumerate(segs):
            d = {k: s.get(k) for k in ("t0", "t1", "verb", "action", "object_id", "object", "target", "description",
                                       "success", "evidence", "src", "review", "cut", "group", "off_list") if s.get(k) not in (None, "")}
            d["index"] = si
            d["hand"] = _hand_side(s.get("hand"))
            d["hand_zh"] = s.get("hand") or ""
            gi = s.get("group") if isinstance(s.get("group"), int) and s.get("group") < len(groups) else None
            d["subtask_index"] = subtasks.get((groups[gi].get("title") if gi is not None else None) or s.get("description") or s.get("action") or s.get("verb") or "未标注")
            d["facts"] = [{k: f.get(k) for k in ("hand", "obj", "label", "t0", "t1", "kind", "moved", "disp_cm", "conf", "min_dist_cm")} for f in s.get("facts", [])]
            sgs.append(d)
        ann = {
            "episode_index": e, "task": tname, "task_index": tasks[tname],
            "task_check": {k: task.get(k) for k in ("seen", "verdict", "reason") if task.get(k)},
            "segments": sgs,
            "groups": [{"index": gi, "from_segment": g["from"] - 1, "to_segment": g["to"] - 1, "t0": g.get("t0"), "t1": g.get("t1"), "title": g.get("title"),
                        "subtask_index": subtasks.get(g.get("title"))} for gi, g in enumerate(groups)],
            "contacts": {s: [{k: ev.get(k) for k in ("obj", "label", "t0", "t1", "kind", "moved", "disp_cm", "conf", "min_dist_cm", "aperture_m", "method",
                                                         "frames_2d_pct", "tool", "via_tool") if ev.get(k) is not None}
                             for ev in events if ev["hand"] == s] for s in SIDES},
            "objects": [{"index": k, "id": o["id"], "label": o["label"], "bbox_first_frame": o.get("bbox")} for k, o in enumerate(objs)],
            "quality_spans": q.get("spans", []),
            "calibration": {"cameras": r["calib"]["cams"], "chosen_by_check": kp.get("chosen")},
            "sync": {k: sync.get(k) for k in ("depth_t_offset", "used", "corr") if k in sync},
            "hands_source": {"keypoints_3d": src3, "keypoints_2d": src2},
            "time": "t0/t1 是相对本条录制第一帧的秒数，等于 data 里的 timestamp",
        }
        (ed / "annotations.json").write_text(jd(ann), encoding="utf-8")
        trk = _rj(wd / "track.json")
        if trk and (wd / "masks.npz").exists():
            from .pipeline import load_masks
            mk = load_masks(wd)
            (ed / "object_masks.json").write_text(jd({
                "format": "COCO RLE（未压缩 counts，列优先）；pycocotools.mask.frPyObjects + decode 可还原",
                "mask_size_hw": [int(mk.shape[2]), int(mk.shape[3])], "image_size_wh": trk["src_size"],
                "fps": trk["fps"], "t": trk["t"],
                "objects": {o["id"]: {"label": o["label"], "masks": [OB.rle_encode(mk[i, k]) if mk[i, k].any() else None for i in range(mk.shape[0])]}
                            for k, o in enumerate(trk["objects"])}}), encoding="utf-8")
        if (wd / "objects_3d.json").exists():
            shutil.copy2(wd / "objects_3d.json", ed / "objects_3d.json")
        files = []
        for s in r["streams"]:
            p = Path(s["path"])
            files.append({"role": s["kind"], "name": s["name"], "path": str(p), "kind": "文件夹（图片序列）" if s["src"] == "seq" else "视频",
                          "frames": s["n"], "fps": s.get("fps"), "dtype": s.get("dtype")})
        for s in r["signals"]:
            files.append({"role": s["kind"], "path": s.get("file")})
        (ed / "source.json").write_text(jd({
            "batch": bid, "recording": r["id"], "name": r["name"], "relative_path": r["rel"], "files": files,
            "frame_map": "data 的 edp.src_rgb_frame / edp.src_timestamp / edp.depth_frame 列：导出帧 → 原始彩色帧号、原始时间戳、原始深度帧号",
            "export_fps": fps, "source_rgb_fps": rgb.get("fps"), "resized": [rgb.get("w"), rgb.get("h")] != [W, H]}), encoding="utf-8")
        if opts.get("depth") and dep:
            dd = out / "edp" / "depth" / f"episode_{e:06d}"
            dd.mkdir(parents=True, exist_ok=True)
            if dep["src"] == "seq":
                for f in dep["files"]:
                    shutil.copy2(Path(dep["path"]) / f, dd / f)
            else:
                shutil.copy2(dep["path"], dd / Path(dep["path"]).name)
        if opts.get("raw"):
            rd = out / "raw" / f"episode_{e:06d}"
            for f in files:
                p = Path(f["path"] or "")
                if p.is_file():
                    rd.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, rd / p.name)
                elif p.is_dir():
                    shutil.copytree(p, rd / p.name, dirs_exist_ok=True)

        # ---- 每条录制的元数据（meta/episodes）
        row = {"episode_index": e, "tasks": [tname], "length": n,
               "data/chunk_index": 0, "data/file_index": e, "dataset_from_index": gidx, "dataset_to_index": gidx + n,
               f"videos/{VIDEO_KEY}/chunk_index": 0, f"videos/{VIDEO_KEY}/file_index": e,
               f"videos/{VIDEO_KEY}/from_timestamp": 0.0, f"videos/{VIDEO_KEY}/to_timestamp": n / fps,
               "meta/episodes/chunk_index": 0, "meta/episodes/file_index": 0}
        for k, v in cols.items():
            for sk, sv in _stats(np.asarray(v).reshape(n, -1)).items():
                row[f"stats/{k}/{sk}"] = sv
        ep_rows.append(row)
        gidx += n
        man["episodes"].append({"dir": f"episode_{e:06d}", "name": r["name"], "batch": bid, "task": tname, "frames": n,
                                "duration_s": round(n / fps, 3), "grade": P.GRADE[P.reach(r, r["profile"])], "segments": len(segs)})
        man["progress"] = (e + 1) / len(items)
        (out / "manifest.json").write_text(jd(man), encoding="utf-8")
        log(f"{e + 1}/{len(items)} {r['name']}")

    # ---- meta
    meta = out / "meta"
    (meta / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(ep_rows).to_parquet(meta / "episodes" / "chunk-000" / "file-000.parquet")
    pd.DataFrame({"task_index": list(tasks.values())}, index=pd.Index(list(tasks.keys()), name="task")).to_parquet(meta / "tasks.parquet")
    pd.DataFrame({"subtask_index": list(subtasks.values())}, index=pd.Index(list(subtasks.keys()), name="subtask")).to_parquet(meta / "subtasks.parquet")

    names3 = lambda s: [f"{s}.{j}.{a}" for j in JOINTS for a in "xyz"]
    names2 = lambda s: [f"{s}.{j}.{a}" for j in JOINTS for a in "uv"]
    state_names = [f"{s}.{JOINTS[j]}.{a}" for s in SIDES for j in STATE_J for a in "xyz"]
    f32 = lambda shape, names: {"dtype": "float32", "shape": list(shape), "names": names}
    i64 = {"dtype": "int64", "shape": [1], "names": None}
    feats = {
        VIDEO_KEY: {"dtype": "video", "shape": [H, W, 3], "names": ["height", "width", "channels"],
                    "info": {"video.height": H, "video.width": W, "video.codec": "h264", "video.pix_fmt": "yuv420p",
                             "video.is_depth_map": False, "video.fps": fps, "video.channels": 3, "has_audio": False}},
        "observation.state": f32([len(state_names)], state_names),
        "observation.hand.left.keypoints_3d": f32([63], names3("left")),
        "observation.hand.right.keypoints_3d": f32([63], names3("right")),
        "observation.hand.left.keypoints_2d": f32([42], names2("left")),
        "observation.hand.right.keypoints_2d": f32([42], names2("right")),
        "observation.hand.left.valid": i64, "observation.hand.right.valid": i64,
    }
    if "observation.head_pose" in all_num:
        feats["observation.head_pose"] = f32([7], ["tx", "ty", "tz", "qx", "qy", "qz", "qw"])
    if "action" in all_num:
        feats["action"] = f32([len(state_names)], state_names)
    feats.update({"annotation.segment_index": i64, "annotation.group_index": i64, "subtask_index": i64, "annotation.contact.left": i64, "annotation.contact.right": i64,
                  "edp.src_rgb_frame": i64, "edp.src_timestamp": {"dtype": "float64", "shape": [1], "names": None}, "edp.depth_frame": i64,
                  "timestamp": {"dtype": "float32", "shape": [1], "names": None}, "frame_index": i64, "episode_index": i64, "index": i64, "task_index": i64})
    info = {"codebase_version": CODEBASE, "robot_type": "human_hands_egocentric", "total_episodes": len(items), "total_frames": gidx,
            "total_tasks": len(tasks), "chunks_size": 1000, "data_files_size_in_mb": 100, "video_files_size_in_mb": 200, "fps": fps,
            "splits": {"train": f"0:{len(items)}"}, "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4", "features": feats}
    (meta / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = {k: _stats(np.concatenate([a.reshape(len(a), -1) for a in v], 0)) for k, v in all_num.items()}
    n_all = np.arange(gidx)
    stats["index"] = _stats(n_all)
    stats["frame_index"] = _stats(np.concatenate([np.arange(r["length"]) for r in ep_rows]))
    stats["episode_index"] = _stats(np.concatenate([np.full(r["length"], r["episode_index"]) for r in ep_rows]))
    stats["timestamp"] = _stats(np.concatenate([np.arange(r["length"]) / fps for r in ep_rows]))
    stats["task_index"] = _stats(np.concatenate([np.full(r["length"], tasks[r["tasks"][0]]) for r in ep_rows]))
    if img_px:
        stats[VIDEO_KEY] = _img_stats(np.concatenate(img_px, 0))
    (meta / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    _write_docs(out, feats, settings, fps, act_mode)


FIELD_DOC = {
    VIDEO_KEY: ("原始", "头戴彩色视频，按导出帧率逐帧重新编码；第 k 帧对应 data 第 k 行", "入库"),
    "observation.state": ("派生", "左手、右手各取手腕 + 5 个指尖的三维位置（相机坐标系，米），共 36 维；先左后右", "关键点核查"),
    "action": ("派生", "下一帧的 observation.state（人手数据没有机器人动作，这是常用约定）；最后一帧重复自身", "导出时计算"),
    "observation.hand.left.keypoints_3d": ("原始/派生", "左手 21 个关节三维位置（相机坐标系，米）。有厂商三维关键点时为原始值，否则只有手腕（平台估计）", "关键点核查 / 手部估计"),
    "observation.hand.right.keypoints_3d": ("原始/派生", "右手 21 个关节三维位置，同上", "关键点核查 / 手部估计"),
    "observation.hand.left.keypoints_2d": ("派生", "左手 21 个关节在彩色画面上的像素坐标（u, v）", "关键点核查"),
    "observation.hand.right.keypoints_2d": ("派生", "右手 21 个关节在彩色画面上的像素坐标（u, v）", "关键点核查"),
    "observation.hand.left.valid": ("派生", "这一帧左手数据是否有效（1 有效，0 缺失，缺失时坐标填 0）", "关键点核查"),
    "observation.hand.right.valid": ("派生", "这一帧右手数据是否有效", "关键点核查"),
    "observation.head_pose": ("原始", "设备记录的头部位姿（平移 + 四元数），按时间戳取最近一条", "入库"),
    "annotation.segment_index": ("派生", "这一帧属于本条录制的第几段（对应 edp/episodes/*/annotations.json 的 segments）", "事件切点"),
    "annotation.group_index": ("派生", "这一帧属于第几个子任务（大模型把连续、属于同一件事的段合成一组；没有分组时等于段号）", "语义标注"),
    "subtask_index": ("派生", "这一帧所在子任务的描述，在 meta/subtasks.parquet 中查文本（LeRobot 子任务字段）；有分组时是组的描述，否则是段的描述", "语义标注"),
    "annotation.contact.left": ("派生", "左手此刻接触的物体编号（annotations.json 的 objects[index]），没有接触为 -1；握着工具时填工具", "接触事件"),
    "annotation.contact.right": ("派生", "右手此刻接触的物体编号，没有接触为 -1", "接触事件"),
    "edp.src_rgb_frame": ("溯源", "这一帧来自原始彩色视频的第几帧（从 0 开始）", "导出时计算"),
    "edp.src_timestamp": ("溯源", "这一帧在原始时间戳文件里的时间（秒，原始单位）", "导出时计算"),
    "edp.depth_frame": ("溯源", "对齐后对应的原始深度帧号（已按时间同步结果校正），没有深度为 -1", "时间同步"),
}


def _write_docs(out, feats, settings, fps, act_mode):
    rows = []
    for k, f in feats.items():
        o, d, step = FIELD_DOC.get(k, ("LeRobot 标准", {"timestamp": "相对本条录制第一帧的秒数（= frame_index / fps）", "frame_index": "本条录制内的帧号",
                                                       "episode_index": "录制编号", "index": "全数据集连续帧号", "task_index": "任务编号，查 meta/tasks.parquet"}.get(k, ""), ""))
        rows.append({"field": k, "dtype": f["dtype"], "shape": f["shape"], "origin": o, "meaning": d, "step": step})
    keep = ["vlm_backend", "api_model", "vlm_model", "vlm_fps", "vlm_max_frames", "contact_mode", "contact_dist_cm", "contact_gap",
            "contact_min", "min_seg", "track_fps", "vocab_version"]
    (out / "edp").mkdir(exist_ok=True)
    (out / "edp" / "fields.json").write_text(jd({
        "fields": rows,
        "files": {"edp/episodes/episode_NNNNNN/annotations.json": "分段（hand 字段为 left / right / both / none）、按左右手分开的接触事件、物体表、质量问题时段、标定、同步",
                  "edp/episodes/episode_NNNNNN/object_masks.json": "每个物体逐跟踪帧的像素掩码（COCO RLE）",
                  "edp/episodes/episode_NNNNNN/objects_3d.json": "每个物体逐跟踪帧的三维位置（相机坐标系，米）",
                  "edp/episodes/episode_NNNNNN/source.json": "这条录制的原始文件清单和对齐说明",
                  "edp/depth/episode_NNNNNN/": "（可选）原始深度文件，帧号对应 data 的 edp.depth_frame",
                  "raw/episode_NNNNNN/": "（可选）原始文件副本"},
        "pipeline_settings": {k: settings.get(k) for k in keep}, "fps": fps, "action": act_mode,
        "verb_vocab": OB.verbs()}), encoding="utf-8")
    lines = ["# 数据集说明", "", "LeRobot v3.0 格式（meta / data / videos 三个目录可直接用 LeRobotDataset 读取），另加平台扩展目录 edp/。", "",
             "## 左右手", "", "data 里左右手分列：observation.hand.left.* 与 observation.hand.right.*；observation.state 先左手 18 维、后右手 18 维；",
             "接触为 annotation.contact.left / annotation.contact.right；annotations.json 里分段的 hand 字段为 left / right / both / none，接触事件按 left / right 分组。", "",
             "## 原始与派生", "", "| 字段 | 来源 | 含义 |", "|---|---|---|"]
    lines += [f"| {r['field']} | {r['origin']} | {r['meaning']} |" for r in rows]
    lines += ["", "## 对齐", "", "data 第 k 行 = 视频第 k 帧 = timestamp k / fps 秒。", "edp.src_rgb_frame、edp.src_timestamp、edp.depth_frame 三列把每一帧对应回原始文件；",
              "原始文件位置见 edp/episodes/episode_NNNNNN/source.json。", "",
              "## 读取示例", "", "```python", "from lerobot.datasets.lerobot_dataset import LeRobotDataset",
              f"ds = LeRobotDataset('本地名字', root=r'{out}')", "x = ds[0]", "x['observation.hand.left.keypoints_3d'], x['subtask']", "```"]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")
