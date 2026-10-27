"""导出训练数据集：每条录制一个文件夹，统一结构 + manifest.json"""
import json
import shutil
import time
from pathlib import Path

from . import plan as P
from . import objects as OB
from .db import jd, jl, work_dir


def _rj(p):
    return jl(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else None


def export(out: Path, items, opts):
    out.mkdir(parents=True, exist_ok=True)
    man = {"name": out.name, "created": time.strftime("%Y-%m-%d %H:%M"), "status": "running", "episodes": [], "format": "edp-v1"}
    (out / "manifest.json").write_text(jd(man), encoding="utf-8")
    for i, (bid, r) in enumerate(items):
        wd = work_dir(bid, r["id"])
        ep = out / f"episode_{i:05d}"
        ep.mkdir(exist_ok=True)
        files = {}
        rgb = next((s for s in r["streams"] if s["kind"] == "rgb"), None)
        dep = next((s for s in r["streams"] if s["kind"] == "depth"), None)
        ing = _rj(wd / "ingest.json") or {}
        if rgb:
            src = Path(rgb["path"]) if ing.get("rgb_preview") == "original" else wd / (ing.get("rgb_preview") or "preview_rgb.mp4")
            if src.exists():
                shutil.copy2(src, ep / "rgb.mp4")
                files["rgb"] = "rgb.mp4"
            if rgb.get("ts"):
                (ep / "rgb_timestamps.csv").write_text("frame,timestamp_s\n" + "\n".join(f"{k},{t:.6f}" for k, t in enumerate(rgb["ts"])), encoding="utf-8")
                files["rgb_timestamps"] = "rgb_timestamps.csv"
        if dep:
            if opts.get("depth_raw") and dep["src"] == "seq":
                dd = ep / "depth"
                dd.mkdir(exist_ok=True)
                for f in dep["files"]:
                    shutil.copy2(Path(dep["path"]) / f, dd / f)
                files["depth"] = "depth/"
            elif (wd / "preview_depth.mp4").exists():
                shutil.copy2(wd / "preview_depth.mp4", ep / "depth_preview.mp4")
                files["depth_preview"] = "depth_preview.mp4"
            if dep.get("ts"):
                (ep / "depth_timestamps.csv").write_text("frame,timestamp_s\n" + "\n".join(f"{k},{t:.6f}" for k, t in enumerate(dep["ts"])), encoding="utf-8")
                files["depth_timestamps"] = "depth_timestamps.csv"
        for sg in r["signals"]:
            src = Path(sg["file"])
            if src.exists() and src.stat().st_size < 500e6:
                shutil.copy2(src, ep / src.name)
                files.setdefault("signals", []).append(src.name)
        if (wd / "hands3d.json").exists():
            shutil.copy2(wd / "hands3d.json", ep / "hand_wrists.json")
            files["hand_wrists"] = "hand_wrists.json"
        # 物体像素掩码（COCO RLE，逐跟踪帧）和三维位置
        trk = _rj(wd / "track.json")
        if trk and (wd / "masks.npz").exists():
            from .pipeline import load_masks
            mk = load_masks(wd)
            (ep / "object_masks.json").write_text(jd({
                "format": "COCO RLE（未压缩 counts，列优先）；pycocotools.mask.frPyObjects + decode 可还原",
                "mask_size_hw": [int(mk.shape[2]), int(mk.shape[3])], "image_size_wh": trk["src_size"],
                "fps": trk["fps"], "t": trk["t"],
                "objects": {o["id"]: {"label": o["label"], "masks": [OB.rle_encode(mk[i, k]) if mk[i, k].any() else None for i in range(mk.shape[0])]}
                            for k, o in enumerate(trk["objects"])}}), encoding="utf-8")
            files["object_masks"] = "object_masks.json"
        if (wd / "objects_3d.json").exists():
            shutil.copy2(wd / "objects_3d.json", ep / "objects_3d.json")
            files["objects_3d"] = "objects_3d.json"
        seg = _rj(wd / "segments.json") or {}
        task = _rj(wd / "task.json") or {}
        q = _rj(wd / "quality.json") or {}
        sync = _rj(wd / "sync.json") or {}
        kp = _rj(wd / "kp_verify.json") or {}
        tname = task.get("user_name") or (r.get("task") or {}).get("name") or task.get("name") or ""
        ann = {
            "source": {"batch": bid, "recording": r["id"], "path": r["rel"], "name": r["name"]},
            "task": {"name": tname, "vlm_seen": task.get("seen"), "consistent": task.get("consistent")},
            "segments": [{k: s.get(k) for k in ("t0", "t1", "hand", "verb", "action", "object_id", "object", "target", "description",
                                                  "success", "evidence", "conf", "src", "review", "facts") if s.get(k) not in (None, "", False)}
                         | {"hand_side": "both" if "双" in str(s.get("hand") or "") else "left" if "左" in str(s.get("hand") or "") else "right" if "右" in str(s.get("hand") or "") else "none"}
                         for s in seg.get("segments", [])],
            "steps": (seg.get("summary") or {}).get("steps", []),
            "verb_vocab": {"version": seg.get("vocab_version") or 1, "verbs": OB.verbs()},
            "objects": (_rj(wd / "objects.json") or {}).get("objects", []),
            "contacts": (_rj(wd / "contact.json") or {}).get("events", []),
            "contacts_by_hand": {h: [e for e in (_rj(wd / "contact.json") or {}).get("events", []) if e.get("hand") == h] for h in ("left", "right")},
            "quality_spans": q.get("spans", []),
            "sync": {k: sync.get(k) for k in ("depth_t_offset", "used", "corr", "est_shift_ms", "pair_err_ms")},
            "calibration": {"cameras": r["calib"]["cams"], "chosen_by_check": kp.get("chosen")},
            "profile": r["profile"], "grade": P.GRADE[P.reach(r, r["profile"])], "duration_s": r["duration"],
            "issues": [{"level": a, "title": b} for a, b, _ in r["issues"]], "files": files,
            "time_note": "segments 的 t0/t1 是相对彩色第一帧的秒数；深度时间 = 彩色时间 + sync.depth_t_offset",
        }
        (ep / "annotations.json").write_text(jd(ann), encoding="utf-8")
        man["episodes"].append({"dir": ep.name, "name": r["name"], "task": tname, "duration_s": r["duration"], "grade": ann["grade"], "segments": len(ann["segments"])})
        man["progress"] = (i + 1) / len(items)
        (out / "manifest.json").write_text(jd(man), encoding="utf-8")
    man["status"] = "done"
    (out / "manifest.json").write_text(jd(man), encoding="utf-8")
    (out / "README.txt").write_text(
        "每个 episode_xxxxx 文件夹是一条录制：\n"
        "  rgb.mp4 / rgb_timestamps.csv      彩色视频与逐帧时间戳\n"
        "  depth/ 或 depth_preview.mp4       深度原始帧（勾选时）或上色预览\n"
        "  annotations.json                  任务名、分段（动作/物体/是否完成）、质量问题时段、同步结果、标定\n"
        "  object_masks.json                 每个物体逐帧的像素掩码（COCO RLE）\n"
        "  objects_3d.json                   每个物体逐帧的三维位置（相机坐标系，米）\n"
        "  其余文件                           原始信号（关键点、位姿、IMU 等）原样复制\n", encoding="utf-8")
