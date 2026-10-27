"""物体清单的“检测器”方案：大模型只报名词（很短），YOLOE 开放词汇检测模型画框，大模型再看一张标了号的图起中文名。

YOLOE 和 YOLO-World 是同一类模型（给名词 → 框出物体），由 ultralytics 提供，SAM 2 安装时已经装好。
选 YOLOE 是因为：可以直接用预先算好的名词向量（随平台附带，assets/yoloe11s_vocab_emb.pt，约 4600 个常见名词），
运行时不需要再下载 600 MB 的文字编码模型；只需下载 28 MB 的检测权重。
"""
import os
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from . import vlm
from .objects import model_dir, letterbox, COLORS, _same

WEIGHTS = "yoloe-11s-seg.pt"
URLS = [f"https://github.com/ultralytics/assets/releases/download/v8.4.0/{WEIGHTS}",
        f"https://ghfast.top/https://github.com/ultralytics/assets/releases/download/v8.4.0/{WEIGHTS}"]
VOCAB = Path(__file__).parent / "assets" / "yoloe11s_vocab_emb.pt"

NOUN_PROMPT = """这是一段手部操作视频的第一帧。列出画面里所有可能被手拿起、移动或操作的物体（包括电池、螺丝、笔、瓶盖这类小东西）。
不要包括桌子、墙、地面、窗户、手、手臂、衣服、腿。
每类物体给一个常用、简单的英文名词（一到两个词，单数，例如 bottle、cup、battery、tray、spoon、playing card），同类物体只写一次。
只输出 JSON：{"nouns": ["bottle", "cup"]}"""

NAME_PROMPT = """图上用彩色方框和数字标出了 {n} 个物体（检测模型给的类别依次是：{cats}）。
请给每个编号的物体起一个简短的中文名称，写出颜色、形状、标签等能和其他物体区分开的特征（例如“红盖矿泉水瓶”“深色大碗”）；
同类的几个物体名字必须不同，外观实在一样的加上位置（左边/中间/右边）。如果某个编号框的根本不是可操作的物体（比如桌面、阴影），名字写“无”。
只输出 JSON：{{"names": {{"1": "名称", "2": "名称"}}}}"""

_model = {"m": None, "vocab": None}


def available():
    try:
        import importlib.util
        return VOCAB.exists() and all(importlib.util.find_spec(m) for m in ("torch", "ultralytics"))
    except Exception:
        return False


def _weights(log=print):
    dst = model_dir() / WEIGHTS
    if dst.exists() and dst.stat().st_size > 20e6:
        return dst
    for url in URLS:
        try:
            log(f"下载检测模型：{url}")
            tmp = dst.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            if tmp.stat().st_size > 20e6:
                tmp.replace(dst)
                return dst
        except Exception as e:
            log(f"失败：{e}")
    raise RuntimeError("检测模型下载失败（需要能访问 github.com）")


def _load():
    if _model["m"] is None:
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        os.environ.setdefault("YOLO_VERBOSE", "false")
        import torch
        from ultralytics import YOLOE
        _model["m"] = YOLOE(str(_weights()))
        d = torch.load(VOCAB, map_location="cpu")
        _model["vocab"] = ([n.lower() for n in d["names"]], d["emb"].float())
    return _model["m"], _model["vocab"]


def unload():
    """检测完就放掉显存，给后面的大模型和 SAM 2 用"""
    if _model["m"] is not None:
        _model["m"] = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


def match_nouns(nouns, vocab_names):
    """大模型给的英文名词 → 词表里的词（完全一致 → 单复数 → 末尾词一致）"""
    idx = {n: i for i, n in enumerate(vocab_names)}
    out = {}
    for w in nouns:
        w0 = str(w).strip().lower().replace("_", " ")
        if not w0:
            continue
        cands = [w0, w0[:-1] if w0.endswith("s") else None, w0[:-2] if w0.endswith("es") else None]
        hit = next((c for c in cands if c and c in idx), None)
        if hit is None:  # 末尾词：red bottle cap → bottle cap / cap
            words = w0.split()
            for k in range(1, len(words)):
                tail = " ".join(words[k:])
                if tail in idx:
                    hit = tail
                    break
        if hit and hit not in out.values():
            out[w0] = hit
    return out


def detect(img, names, conf=0.2, imgsz=1280):
    m, (vn, ve) = _load()
    idx = {n: i for i, n in enumerate(vn)}
    import torch
    emb = ve[[idx[n] for n in names]].unsqueeze(0)
    m.set_classes(names, emb)
    r = m.predict(img, imgsz=imgsz, conf=conf, iou=0.5, verbose=False)[0]
    out = []
    for b, c, s in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist()):
        out.append({"cat": names[int(c)], "bbox": [round(float(v), 1) for v in b], "score": round(float(s), 3)})
    return sorted(out, key=lambda o: -o["score"])


def draw_numbered(img, boxes):
    out = img.copy()
    lw = max(2, img.shape[1] // 400)
    fs = max(0.6, img.shape[1] / 1100)
    for i, b in enumerate(boxes):
        x1, y1, x2, y2 = [int(v) for v in b]
        c = COLORS[i % len(COLORS)]
        cv2.rectangle(out, (x1, y1), (x2, y2), c, lw)
        t = str(i + 1)
        (tw, th), _ = cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, fs, 2)
        y = max(th + 6, y1)
        cv2.rectangle(out, (x1, y - th - 6), (x1 + tw + 8, y), c, -1)
        cv2.putText(out, t, (x1 + 4, y - 4), cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def inventory(model, img, log=print):
    """返回 (objects, info)。objects 与旧方案格式相同：[{id, label, bbox, found, cat, score}]"""
    h, w = img.shape[:2]
    info = {"mode": "检测模型", "calls": []}
    # 1) 大模型只报英文名词
    t0 = time.time()
    j, raw, _ = vlm.ask(model, NOUN_PROMPT, [img], max_side=768)
    nouns = [str(x) for x in (j.get("nouns") or []) if str(x).strip()]
    info["calls"].append({"what": "列名词", "sec": round(time.time() - t0, 1)})
    _, (vn, _) = _load()
    mp = match_nouns(nouns, vn)
    info.update(nouns=nouns, matched=mp, unmatched=[n for n in nouns if n.strip().lower() not in mp])
    if not mp:
        raise RuntimeError("大模型给的名词在检测词表里都找不到：" + "、".join(nouns))
    # 2) 检测模型画框
    t0 = time.time()
    dets = detect(img, sorted(set(mp.values())))
    info["calls"].append({"what": "检测", "sec": round(time.time() - t0, 2)})
    objs = []
    for d in dets:
        b = d["bbox"]
        area = (b[2] - b[0]) * (b[3] - b[1]) / (w * h)
        if not 0.00008 < area < 0.5 or any(_same(b, o["bbox"]) for o in objs):
            continue
        objs.append(d)
        if len(objs) >= 12:
            break
    unload()
    if not objs:
        return [], info
    # 3) 大模型看标号图起中文名
    t0 = time.time()
    names = {}
    try:
        marked = draw_numbered(img, [o["bbox"] for o in objs])
        j, raw, _ = vlm.ask(model, NAME_PROMPT.format(n=len(objs), cats="、".join(f"{i+1}={o['cat']}" for i, o in enumerate(objs))),
                            [marked], max_side=1000)
        names = {str(k): str(v) for k, v in (j.get("names") or {}).items()}
    except Exception as e:
        info["name_err"] = str(e)
    info["calls"].append({"what": "起名", "sec": round(time.time() - t0, 1)})
    out = []
    for i, o in enumerate(objs):
        nm = names.get(str(i + 1), "").strip()
        if nm in ("无", "none", "None"):
            continue
        out.append({"label": nm or o["cat"], "bbox": o["bbox"], "found": "检测", "cat": o["cat"], "score": o["score"]})
    for i, o in enumerate(out):
        o["id"] = f"O{i + 1}"
    return out, info
