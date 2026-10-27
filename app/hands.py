"""人手关键点检测（MediaPipe Hands，模型随库自带，离线可用）"""
import os
import threading
import warnings

os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
warnings.filterwarnings("ignore")

import cv2
import numpy as np

_lock = threading.Lock()
_det = {}

TH = {"human_rate": 0.5, "human_score": 0.85, "human_cont": 0.6, "some_rate": 0.10}


def _get(static=True):
    import mediapipe as mp
    if os.name == "nt" and not os.path.abspath(mp.__file__).isascii():
        raise RuntimeError("MediaPipe 装在含中文的路径下无法加载模型（" + os.path.dirname(mp.__file__) + "）。"
                           "请关闭窗口后重新双击 run.bat，新版会把运行环境装到不含中文的位置")
    k = "s" if static else "v"
    if k not in _det:
        _det[k] = mp.solutions.hands.Hands(static_image_mode=static, max_num_hands=2, model_complexity=1,
                                           min_detection_confidence=0.3, min_tracking_confidence=0.3)
    return _det[k]


def detect(frame, static=True, max_side=1280):
    """返回 [{side, score, pts: 21×2 像素坐标}]"""
    if frame is None:
        return []
    h, w = frame.shape[:2]
    s = 1.0
    if max(h, w) > max_side:
        s = max_side / max(h, w)
        frame = cv2.resize(frame, (int(w * s), int(h * s)))
    with _lock:
        res = _get(static).process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    out = []
    if res.multi_hand_landmarks:
        for lm, hd in zip(res.multi_hand_landmarks, res.multi_handedness):
            pts = np.array([[p.x * w, p.y * h] for p in lm.landmark])
            # MediaPipe 的左右是按“镜像自拍”约定，第一视角画面要反过来
            side = "right" if hd.classification[0].label == "Left" else "left"
            out.append({"side": side, "score": float(hd.classification[0].score), "pts": pts})
    return out


def judge(frames):
    """只看画面判断末端是不是人手：检出率、平均置信度、连续性三个数，按阈值判"""
    hits, scores = [], []
    for f in frames:
        d = detect(f, max_side=640)
        hits.append(bool(d))
        if d:
            scores.append(max(x["score"] for x in d))
    n, k = len(hits), sum(hits)
    rate = k / n if n else 0
    score = float(np.mean(scores)) if scores else 0
    cont = sum(1 for i, x in enumerate(hits) if x and ((i > 0 and hits[i - 1]) or (i < n - 1 and hits[i + 1]))) / k if k else 0
    if rate >= TH["human_rate"] and score >= TH["human_score"] and cont >= TH["human_cont"]:
        v, label = "human", "人手"
    elif rate >= TH["some_rate"] and score >= TH["human_score"] and cont >= TH["human_cont"]:
        v, label = "human", "人手（只在部分时段入画）"
    elif rate >= TH["some_rate"]:
        v, label = "maybe_robot", "疑似机械手，或人手常被遮挡"
    else:
        v, label = "none", "未检出人手：可能是夹爪，或末端不在画面"
    return {"verdict": v, "label": label, "rate": rate, "score": score, "continuity": cont, "sampled": n}
