"""精细标注：物体清单（视觉大模型框物体）→ 物体跟踪（SAM 2）→ 接触事件（手部关键点 + 物体轮廓 + 深度）

思路参考 ECoT：能用信号算出来的（谁碰了什么、什么时候、移动了多远）都用规则算，
视觉大模型只负责“这是什么物体”和最后的语言描述。
"""
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from . import media, vlm
from .db import DATA, get_setting, jd, jl

FINGERTIPS = [4, 8, 12, 16, 20]          # 拇指、食指、中指、无名指、小指指尖
# 默认动作词表：词、定义、例子、近义词（近义词用于把模型写的说法映射回词表）
DEFAULT_VOCAB = [
    {"verb": "静止", "def": "手没有在操作任何物体，基本不动或不在画面里", "ex": "录制开始时双手放在桌边", "syn": "无操作 等待 停顿 不动 空闲"},
    {"verb": "伸向", "def": "手朝某个物体移动，还没有碰到", "ex": "右手伸向 O2", "syn": "接近 靠近 够向"},
    {"verb": "收回", "def": "松开物体后手离开，回到身边或准备下一步", "ex": "放下 O1 后左手收回", "syn": "撤回 离开 回位"},
    {"verb": "抓取", "def": "手指合拢握住物体，物体还没离开支撑面", "ex": "左手抓住 O4 瓶身", "syn": "握住 抓住 捏住"},
    {"verb": "拿起", "def": "握住物体并让它离开支撑面", "ex": "拿起 O1", "syn": "拾起 提起 举起 抬起 拿"},
    {"verb": "移动", "def": "拿着物体从一处移到另一处", "ex": "把 O4 移到 O2 右侧", "syn": "搬动 挪动 转移 运送"},
    {"verb": "放下", "def": "把手中物体放到某个表面上", "ex": "把 O3 放到托盘上", "syn": "放置 放回 摆放 放"},
    {"verb": "松开", "def": "手指张开，离开物体", "ex": "左手松开 O1", "syn": "释放 放手"},
    {"verb": "推", "def": "手推着物体在表面上滑动，不拿起", "ex": "把 O2 推到桌边", "syn": "推动 推开"},
    {"verb": "拉", "def": "手拉着物体朝自己方向滑动，不拿起", "ex": "把抽屉拉出", "syn": "拉动 拖 拖动"},
    {"verb": "旋转", "def": "绕物体自身轴转动（如拧瓶盖、转把手）", "ex": "旋转 O1 瓶盖", "syn": "拧 转动 扭"},
    {"verb": "翻转", "def": "把物体翻面或倒过来", "ex": "把 O2 翻过来", "syn": "翻面 倒置"},
    {"verb": "调整", "def": "小幅改变物体的位置或朝向，不明显搬走", "ex": "调整 O2 使其对齐", "syn": "微调 摆正 校正"},
    {"verb": "排列", "def": "把几个物体按顺序或对齐摆好", "ex": "把三个瓶子排成一排", "syn": "整理 码放 对齐"},
    {"verb": "叠放", "def": "把物体放在另一个物体上面", "ex": "把 O3 叠在 O1 上", "syn": "堆叠 摞"},
    {"verb": "换手", "def": "物体从一只手交到另一只手", "ex": "O1 从右手换到左手", "syn": "交接 递"},
    {"verb": "打开", "def": "打开盖子、门、抽屉等", "ex": "打开 O2 瓶盖", "syn": "开 掀开 拉开"},
    {"verb": "关闭", "def": "关上盖子、门、抽屉等", "ex": "盖上 O2", "syn": "关 盖上 合上"},
    {"verb": "倾倒", "def": "倾斜容器让里面的东西流出", "ex": "把 O1 里的水倒进杯子", "syn": "倒 倒出"},
    {"verb": "插入", "def": "把物体放进孔、槽或容器里", "ex": "把笔插进笔筒", "syn": "塞入 放入 装入"},
    {"verb": "取出", "def": "从孔、槽或容器里拿出物体", "ex": "从盒子里取出 O3", "syn": "拔出 拿出"},
    {"verb": "按压", "def": "用手指或手掌按下", "ex": "按下按钮", "syn": "按 压 摁"},
    {"verb": "其他", "def": "以上都不合适时使用，并在描述里写清楚", "ex": "", "syn": ""},
]


def vocab():
    v = get_setting("vocab")
    return v if isinstance(v, list) and v else DEFAULT_VOCAB


def verbs():
    return [x["verb"] for x in vocab()]


def vocab_text():
    """写进提示词：每个词一行，带定义"""
    return "\n".join(f"  {x['verb']}：{x.get('def','')}" + (f"（例：{x['ex']}）" if x.get("ex") else "") for x in vocab())


def normalize_verb(v):
    """模型写的词 → 词表里的词。返回 (词, 是否需要人工复核)"""
    v = (v or "").strip()
    voc = vocab()
    names = [x["verb"] for x in voc]
    if v in names:
        return v, False
    for x in voc:
        if v in (x.get("syn") or "").split():
            return x["verb"], False
    for x in voc:  # 模型写成“拿起来”“放下了”这类
        if x["verb"] != "其他" and x["verb"] in v:
            return x["verb"], False
    for x in voc:
        for s in (x.get("syn") or "").split():
            if s and s in v:
                return x["verb"], False
    return ("其他" if "其他" in names else names[-1]), bool(v)


VERBS = [x["verb"] for x in DEFAULT_VOCAB]  # 兼容旧代码


# ============================================================ 组件检查 ===========
def model_dir():
    """模型文件放在不含中文的路径（Windows 上很多库不支持中文路径）"""
    if os.environ.get("EDP_MODELS"):  # 安装包里自带的模型目录（Docker 镜像、Windows 安装包）
        p = Path(os.environ["EDP_MODELS"])
        p.mkdir(parents=True, exist_ok=True)
        return p
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    p = Path(base) / "edp" / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


SAM_WEIGHTS = "sam2.1_s.pt"
SAM_URLS = ["https://github.com/ultralytics/assets/releases/download/v8.3.0/sam2.1_s.pt",
            "https://ghfast.top/https://github.com/ultralytics/assets/releases/download/v8.3.0/sam2.1_s.pt"]


def sam_status():
    has = all(importlib.util.find_spec(m) is not None for m in ("torch", "ultralytics"))
    st = {"installed": has, "weights": (model_dir() / SAM_WEIGHTS).exists(), "cuda": False, "torch": None, "device": None}
    if has:
        try:
            import torch
            st.update(torch=torch.__version__, cuda=bool(torch.cuda.is_available()),
                      device=torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU")
        except Exception as e:
            st.update(installed=False, err=str(e))
    st["ready"] = st["installed"] and st["weights"]
    return st


def ensure_weights(log=print):
    dst = model_dir() / SAM_WEIGHTS
    if dst.exists() and dst.stat().st_size > 50e6:
        return dst
    import urllib.request
    for url in SAM_URLS:
        try:
            log(f"下载 SAM 2 权重：{url}")
            tmp = dst.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            if tmp.stat().st_size > 50e6:
                tmp.replace(dst)
                return dst
        except Exception as e:
            log(f"失败：{e}")
    raise RuntimeError("SAM 2 权重下载失败，请检查网络（需要能访问 github.com）")


# ============================================================ 1. 物体清单 =========
INVENTORY_PROMPT = """这是一段手部操作视频的第一帧。
找出画面里所有可能被手拿起、移动或操作的物体。不要包括桌子、墙、地面、手、手臂、衣服、腿。
不要把物体的零件单独列出（抽屉拉手、瓶盖、把手、按钮、拉链都算所属物体的一部分）。
每个物体给一个简短的中文名称，要能和其他物体区分开（写出颜色、形状、大小、标签等特征，例如“红盖矿泉水瓶”“深色大碗”）。
同一种东西有几个就列几个，每个单独给一个框，不要把几个合成一个框（例如桌上有 4 个罐子，就列 4 个）。
几个同类物体的名称必须写出它们的不同之处；外观实在一样的，加上位置（左边/中间/右边/前排/后排）。
优先列出手够得着的近处物体（桌面、台面上的）；远处背景里的（货架、冰柜、墙上的）放在最后。
【任务】给出每个物体的边界框，坐标用 0 到 1000 的相对坐标 [x1, y1, x2, y2]（左上角为原点）。最多 10 个。
只输出 JSON：{"task_objects": ["任务里提到的物体名称"], "objects": [{"label": "名称", "bbox_2d": [x1, y1, x2, y2]}]}"""

TASK_LINE = """这段视频的任务说明：“{task}”。
任务里提到的物体一定要找出来，名称沿用任务里的叫法（可以加上颜色、位置来区分），并把任务里提到的物体名称列在 task_objects 里。
"""

FIND_PROMPT = """这是一段手部操作视频的画面。请在图里找出“{name}”。它可能比较小、被部分遮挡，或者和别的物体挨在一起。
如果有几个都符合，每个单独给一个框。确实找不到就返回空列表。坐标用 0 到 1000 的相对坐标 [x1, y1, x2, y2]。
只输出 JSON：{{"objects": [{{"label": "{name}", "bbox_2d": [x1, y1, x2, y2]}}]}}"""


def letterbox(img, size=1000):
    """补成正方形：这样模型无论输出 0-1000 相对坐标还是像素坐标，换算结果都一致"""
    h, w = img.shape[:2]
    s = size / max(h, w)
    nw, nh = int(round(w * s)), int(round(h * s))
    canvas = np.full((size, size, 3), 114, np.uint8)
    ox, oy = (size - nw) // 2, (size - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return canvas, s, ox, oy


def _parse_boxes(j):
    items = j.get("objects") if isinstance(j, dict) else j
    if isinstance(items, dict):
        items = [items]
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        b = it.get("bbox_2d") or it.get("bbox") or it.get("box")
        lab = it.get("label") or it.get("name") or it.get("object")
        if isinstance(b, list) and len(b) == 4 and lab:
            try:
                out.append((str(lab), [float(v) for v in b]))
            except Exception:
                pass
    return out


def _iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0


TILE_PROMPT = """这是一段手部操作视频画面的局部放大图。{hint}
找出这张图里所有可能被手拿起、移动或操作的物体，特别注意小物体（电池、螺丝、笔、零件等）。不要包括桌子、墙、地面、手、手臂、衣服。
不要把大物体的零件单独列出（抽屉拉手、瓶盖、把手、按钮、拉链）；已经能看到完整的大物体不用重复列。
每个物体给一个简短的中文名称（写出颜色、形状等特征）。同一种东西有几个就列几个，每个单独一个框。只框完整出现在图里的物体，被图边缘切掉一大半的不要。
坐标用 0 到 1000 的相对坐标 [x1, y1, x2, y2]。
只输出 JSON：{{"objects": [{{"label": "名称", "bbox_2d": [x1, y1, x2, y2]}}]}}"""


def _ask_boxes(model, img, prompt, region, full=False):
    """在图像的某个区域（x0, y0, x1, y1，原图像素）里找物体，返回原图坐标的框"""
    x0, y0, x1, y1 = [int(v) for v in region]
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return [], ""
    sq, s, ox, oy = letterbox(crop, 1000)
    j, raw, sec = vlm.ask(model, prompt, [sq], max_side=1000)
    out = []
    for lab, b in _parse_boxes(j):
        bx1, by1, bx2, by2 = b
        if max(b) <= 1.5:
            bx1, by1, bx2, by2 = [v * 1000 for v in b]
        box = [(bx1 - ox) / s + x0, (by1 - oy) / s + y0, (bx2 - ox) / s + x0, (by2 - oy) / s + y0]
        out.append((lab, box))
    return (out, raw, j) if full else (out, raw)


def _same(a, b):
    """两个框是不是同一个物体：重叠很大，且大小相近（小物体在大托盘里不算重复）"""
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    aa = (a[2] - a[0]) * (a[3] - a[1])
    ab = (b[2] - b[0]) * (b[3] - b[1])
    if aa <= 0 or ab <= 0:
        return False
    return _iou(a, b) > 0.5 or (inter / min(aa, ab) > 0.8 and min(aa, ab) / max(aa, ab) > 0.4)


PART_WORDS = ("拉手", "把手", "瓶盖", "盖子", "按钮", "旋钮", "拉链", "手柄", "提手")


def _contain(a, b):
    """两个框的交叠占较小那个框的比例"""
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    m = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / m if m > 0 else 0.0


def _name_match(a, b):
    """两个中文物体名是不是指同一样东西：互相包含，或共用的字占较短者的六成以上"""
    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return len(set(a) & set(b)) / max(1, min(len(set(a)), len(set(b)))) >= 0.6


def inventory(model, img, zoom=True, max_n=20, task="", keep=None):
    """物体清单：先看整张图；再把画面分成 4 块放大看、把大的容器（托盘、盒子）放大看，补上小物体。
    task：任务说明，里面提到的物体是必找清单，第一遍没找到的单独再找一次（最多 3 个）。
    keep(box)：返回 False 的物体不要（用深度去掉手够不着的远处背景）；必找清单里的物体不受它影响。"""
    h, w = img.shape[:2]
    found, raws = [], []
    prompt = INVENTORY_PROMPT.replace("最多 10 个", f"最多 {int(max_n)} 个").replace("【任务】", TASK_LINE.format(task=task) if task else "")
    cand, raw, j = _ask_boxes(model, img, prompt, (0, 0, w, h), full=True)
    raws.append(raw)
    found += [(lab, b, "整图") for lab, b in cand]
    want = [str(x).strip() for x in ((j or {}).get("task_objects") or []) if str(x).strip()][:8] if task else []
    if zoom:
        regions = []
        ov = 0.15
        for gx in range(2):
            for gy in range(2):
                rx0 = max(0, (gx * 0.5 - ov / 2) * w); ry0 = max(0, (gy * 0.5 - ov / 2) * h)
                regions.append(((rx0, ry0, min(w, rx0 + (0.5 + ov) * w), min(h, ry0 + (0.5 + ov) * h)), "", "分块"))
        from . import batchq
        for lab, b in ([] if batchq.active() else cand):  # 大容器：托盘、盒子里常放着小物体（批量模式下不做：它依赖第一次的结果，会多等一轮）
            area = (b[2] - b[0]) * (b[3] - b[1]) / (w * h)
            if area > 0.05:
                pad = 0.05
                regions.append(((max(0, b[0] - pad * w), max(0, b[1] - pad * h), min(w, b[2] + pad * w), min(h, b[3] + pad * h)),
                                f"这是“{lab}”及其上面、里面的区域。", "容器"))
        for reg, hint, how in regions[:7]:
            try:
                more, raw = _ask_boxes(model, img, TILE_PROMPT.format(hint=hint), reg)
                raws.append(raw)
                rx0, ry0, rx1, ry1 = reg
                m = 0.01 * w
                for lab, b in more:
                    # 放大只为补小物体：大的（多半是被切开的大物体）、贴着分块内侧边缘的都不要
                    if (b[2] - b[0]) * (b[3] - b[1]) / (w * h) > 0.03:
                        continue
                    if (b[0] < rx0 + m and rx0 > 0) or (b[1] < ry0 + m and ry0 > 0) or (b[2] > rx1 - m and rx1 < w) or (b[3] > ry1 - m and ry1 < h):
                        continue
                    found.append((lab, b, how))
            except Exception as e:
                raws.append(f"放大识别失败：{e}")
    # 必找清单：任务里提到、但前面没找到的物体，单独再问一次
    from . import batchq
    missing = [] if batchq.active() else [n for n in want if not any(_name_match(n, lab) for lab, _, _ in found)]
    for name in missing[:3]:
        try:
            more, raw = _ask_boxes(model, img, FIND_PROMPT.format(name=name.replace('"', '')), (0, 0, w, h))
            raws.append(raw)
            found += [(lab or name, b, "任务") for lab, b in more]
        except Exception as e:
            raws.append(f"补找“{name}”失败：{e}")
    is_task = lambda lab: any(_name_match(n, lab) for n in want)
    # 任务物体排在前面，保证不被名额挤掉
    found.sort(key=lambda f: 0 if (f[2] == "任务" or is_task(f[0])) else 1)
    objs, far = [], 0
    for lab, b, how in found:
        box = [float(np.clip(b[0], 0, w)), float(np.clip(b[1], 0, h)), float(np.clip(b[2], 0, w)), float(np.clip(b[3], 0, h))]
        area = (box[2] - box[0]) * (box[3] - box[1]) / (w * h)
        if box[2] <= box[0] or box[3] <= box[1] or not 0.00008 < area < 0.5:
            continue
        if any(_same(box, o["bbox"]) for o in objs):
            continue
        if how in ("分块", "容器"):
            if any(_name_match(lab, o["label"]) and _contain(box, o["bbox"]) > 0.5 for o in objs):
                continue  # 放大后又找到一次同一个东西（框大小不同，几何去重没去掉）
            if any(p in lab for p in PART_WORDS) and any(_contain(box, o["bbox"]) > 0.6 for o in objs):
                continue  # 某个物体的零件（抽屉拉手、瓶盖……）
        t = how == "任务" or is_task(lab)
        if keep is not None and not t and not keep(box):
            far += 1
            continue
        o = {"label": lab, "bbox": [round(v, 1) for v in box], "found": how}
        if t:
            o["task"] = True
        objs.append(o)
    objs = objs[:int(max_n)]  # 上限在设置页“物体清单上限”里改
    for i, o in enumerate(objs):
        o["id"] = f"O{i + 1}"
    got = [n for n in want if any(_name_match(n, o["label"]) for o in objs)]
    stats = {"dropped_far": far, "task_objects": want, "task_found": got, "task_missing": [n for n in want if n not in got]}
    return objs, "\n---\n".join(r for r in raws if r), stats


COLORS = [(66, 135, 245), (80, 200, 120), (240, 160, 40), (200, 90, 220), (60, 200, 220), (230, 80, 80), (150, 150, 60), (120, 120, 240), (240, 120, 180), (100, 180, 100)]


def draw_objects(img, objs):
    out = img.copy()
    for k, o in enumerate(objs):
        x1, y1, x2, y2 = [int(v) for v in o["bbox"]]
        c = COLORS[k % len(COLORS)]
        cv2.rectangle(out, (x1, y1), (x2, y2), c, 2)
        cv2.rectangle(out, (x1, y1 - 18), (x1 + 34, y1), c, -1)
        cv2.putText(out, o["id"], (x1 + 3, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return out


# ============================================================ 2. 物体跟踪 =========
def make_clip(src_path, fps, width, out):
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src_path), "-an", "-vf", f"fps={fps},scale={width}:-2",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)]
    r = subprocess.run(cmd, capture_output=True, creationflags=media._NOWIN)
    if r.returncode != 0:
        raise RuntimeError("生成跟踪用视频失败：" + r.stderr.decode(errors="ignore")[-200:])


def _sub_clip(clip, out, start, fps, reverse=False):
    """从跟踪用视频里截一段：正向 = 第 start 帧到结尾；反向 = 第 0 到 start 帧倒着放"""
    vf = f"trim=end_frame={start + 1},setpts=PTS-STARTPTS,reverse" if reverse else f"trim=start_frame={start},setpts=PTS-STARTPTS"
    # -r 固定输出帧率：不写的话新版 ffmpeg 截取后会按 25 帧/秒补帧，帧号就对不上了
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(clip), "-an", "-vf", vf, "-r", f"{fps:g}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(out)]
    r = subprocess.run(cmd, capture_output=True, creationflags=media._NOWIN)
    if r.returncode != 0:
        raise RuntimeError("截取跟踪片段失败：" + r.stderr.decode(errors="ignore")[-200:])


def track(src_video, objs, src_w, report, fps=10.0, width=640, mask_w=640, device=None, tag="x"):
    """SAM 2 视频跟踪：用物体框初始化，之后逐帧传播。返回逐帧的框、面积、质心，和低分辨率掩码。
    大部分物体在第一帧给框；人工在中途某一帧补框的物体（带 t），从那一帧向后、再倒着向前各跟一遍"""
    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    os.environ.setdefault("YOLO_VERBOSE", "false")
    import torch
    from ultralytics.models.sam import SAM2VideoPredictor
    weights = ensure_weights()
    tmp = Path(tempfile.gettempdir()) / "edp_track" / f"{tag}_{os.getpid()}"  # 每条录制单独一个临时目录：几条同时跟踪时互不覆盖
    tmp.mkdir(parents=True, exist_ok=True)
    clip = tmp / "clip.mp4"
    make_clip(src_video, fps, width, clip)
    cap = cv2.VideoCapture(str(clip))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    ch = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    s = width / src_w
    gpu = torch.cuda.is_available()
    mw, mh = mask_w, int(round(ch * mask_w / width))
    K = len(objs)
    bb = [[None] * n for _ in range(K)]
    area = np.zeros((K, n), np.float32)
    cen = np.full((K, n, 2), np.nan, np.float32)
    masks = np.zeros((n, K, mh, mw), bool)
    start = [min(max(0, int(round(float(o.get("t") or 0) * fps))), max(0, n - 1)) for o in objs]
    # 一次跟踪 = 一个起点帧 + 一个方向 + 这一组物体
    runs = []
    for i0 in sorted(set(start)):
        ks = [k for k in range(K) if start[k] == i0]
        runs.append((i0, ks, False))
        if i0 > 0:
            runs.append((i0, ks, True))
    total = sum((n - i0) if not rev else (i0 + 1) for i0, _, rev in runs) or 1
    done = 0
    for j, (i0, ks, rev) in enumerate(runs):
        src = clip
        if i0 > 0 or rev:
            src = tmp / f"clip_{j}.mp4"
            _sub_clip(clip, src, i0, fps, reverse=rev)
        boxes = [[v * s for v in objs[k]["bbox"]] for k in ks]
        ov = {"device": f"cuda:{device}"} if (gpu and device is not None) else {}
        pred = SAM2VideoPredictor(overrides=dict(conf=0.25, task="segment", mode="predict", imgsz=1024 if gpu else 512, **ov,
                                                 model=str(weights), save=False, verbose=False, half=gpu))
        m_len = (i0 + 1) if rev else (n - i0)
        for i, r in enumerate(pred(source=str(src), bboxes=boxes, labels=[1] * len(ks), stream=True)):
            if i >= m_len:
                break
            fi = i0 - i if rev else i0 + i
            if r.masks is not None:
                m = r.masks.data.cpu().numpy() > 0.5
                for jj, k in enumerate(ks[:m.shape[0]]):
                    mk = m[jj]
                    if mk.any():
                        ys, xs = np.nonzero(mk)
                        sx, sy = width / mk.shape[1], ch / mk.shape[0]
                        bb[k][fi] = [round(float(xs.min() * sx / s), 1), round(float(ys.min() * sy / s), 1),
                                     round(float(xs.max() * sx / s), 1), round(float(ys.max() * sy / s), 1)]
                        area[k, fi] = mk.mean()
                        cen[k, fi] = [xs.mean() * sx / s, ys.mean() * sy / s]
                        masks[fi, k] = cv2.resize(mk.astype(np.uint8), (mw, mh), interpolation=cv2.INTER_NEAREST) > 0
            done += 1
            if done % 5 == 0:
                report(done / total, f"第 {done}/{total} 帧" + (f"（第 {j + 1}/{len(runs)} 遍）" if len(runs) > 1 else ""))
        del pred
        if gpu:
            torch.cuda.empty_cache()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    t = (np.arange(n) / fps).round(3)
    return {"fps": fps, "t": t.tolist(), "mask_size": [mw, mh], "src_size": [src_w, int(round(ch / s))],
            "objects": [{"id": o["id"], "label": o["label"], "bbox": bb[k], "area": area[k].round(5).tolist(),
                         "centroid": [None if np.isnan(c[0]) else [round(float(c[0]), 1), round(float(c[1]), 1)] for c in cen[k]]}
                        for k, o in enumerate(objs)]}, masks


def apply_manual(objs, man):
    """把人工对物体清单的修改叠加到自动识别的清单上：改名、重新框选（带时刻 t）、删除、新增（编号 M1、M2…）。
    重复调用结果不变；自动清单重算后再叠一次即可"""
    out = [dict(o) for o in objs]
    for oid, e in ((man or {}).get("objects") or {}).items():
        if e.get("deleted"):
            out = [o for o in out if o["id"] != oid]
            continue
        o = next((x for x in out if x["id"] == oid), None)
        if o is None:
            if not e.get("bbox"):
                continue
            o = {"id": oid, "label": e.get("label") or oid, "bbox": e["bbox"], "found": e.get("found") or "人工"}
            if e.get("seg") is not None:
                o["seg"] = e["seg"]
            out.append(o)
        if e.get("label"):
            o["label"] = e["label"]
        if e.get("bbox"):
            o["bbox"] = [round(float(v), 1) for v in e["bbox"]]
            o["t"] = round(float(e.get("t") or 0), 3)
            o["found"] = e.get("found") or "人工"
    return out


# ============================================================ 3. 接触事件 =========
def _runs(b, fill=2, min_len=3):
    """布尔序列 → 连续区间，先填补短缺口，再去掉太短的段"""
    b = b.copy()
    idx = np.nonzero(b)[0]
    for a, c in zip(idx[:-1], idx[1:]):
        if 1 < c - a <= fill + 1:
            b[a:c] = True
    out, st = [], None
    for i, v in enumerate(b):
        if v and st is None:
            st = i
        if not v and st is not None:
            out.append((st, i - 1))
            st = None
    if st is not None:
        out.append((st, len(b) - 1))
    return [(a, c) for a, c in out if c - a + 1 >= min_len]


def _backproject(us, vs, z, cam, W, H, dw, dh):
    """深度图像素 (us, vs) + 深度 z（米） → 相机坐标三维点。内参按 标定分辨率 → 彩色分辨率 → 深度分辨率 换算"""
    ax = W / float(cam["w"]) if cam.get("w") else 1.0
    ay = H / float(cam["h"]) if cam.get("h") else ax
    sx, sy = W / dw, H / dh  # 深度像素 → 彩色像素
    u, v = us * sx, vs * sy
    return np.stack([(u - cam["cx"] * ax) * z / (cam["fx"] * ax), (v - cam["cy"] * ay) * z / (cam["fy"] * ay), z], -1)


def tips_from_depth(pts2d, dm, cam, W, H):
    """没有三维关键点时：指尖像素附近取最近的深度（手在最前面），反投影成三维"""
    dh, dw = dm.shape
    out = []
    for j in FINGERTIPS:
        p = pts2d[j] if j < len(pts2d) else None
        if p is None or p[0] is None:
            out.append(None)
            continue
        u, v = int(p[0] * dw / W), int(p[1] * dh / H)
        win = dm[max(0, v - 2):v + 3, max(0, u - 2):u + 3]
        z = win[win > 0]
        if not 0 <= u < dw or not 0 <= v < dh or z.size == 0:
            out.append(None)
            continue
        out.append(_backproject(np.array(u, float), np.array(v, float), float(z.min()), cam, W, H, dw, dh).tolist())
    return out


FINGER_R = 0.008  # 指尖关键点到手指表面的距离（米），约为手指半径

HAND_BONES = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12),
              (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (0, 17), (17, 18), (18, 19), (19, 20)]


def hand_mask(hs, shape, W, H):
    """画面里所有手占的像素（给定分辨率）：手掌多边形 + 每根手指按手指粗细画线，再外扩一圈。
    用来把物体轮廓里被手挡住的部分扣掉：手挡在物体前面时，那些像素的深度是手的，不是物体的"""
    h, w = shape
    sx, sy = w / W, h / H
    m = np.zeros((h, w), np.uint8)
    for side, conf, pts, ex in hs or []:
        P = [(p[0] * sx, p[1] * sy) if p and p[0] is not None else None for p in pts]
        if sum(p is not None for p in P) < 10:
            continue
        palm_ids = [i for i in (0, 1, 5, 9, 13, 17) if P[i] is not None]
        pw = np.hypot(P[5][0] - P[17][0], P[5][1] - P[17][1]) if P[5] and P[17] else 20 * sx * W / 640
        th = max(3, int(round(pw * 0.24)))  # 手指粗细约为手掌宽度（食指根到小指根）的四分之一
        if len(palm_ids) >= 3:
            cv2.fillConvexPoly(m, cv2.convexHull(np.array([P[i] for i in palm_ids], np.int32)), 1)
        for a, b in HAND_BONES:
            if P[a] is not None and P[b] is not None:
                cv2.line(m, (int(P[a][0]), int(P[a][1])), (int(P[b][0]), int(P[b][1])), 1, th)
        for p in P:
            if p is not None:
                cv2.circle(m, (int(p[0]), int(p[1])), th // 2 + 1, 1, -1)
    pad = max(2, int(round(w / 320)))  # 再外扩一点，抵消彩色与深度的配准误差
    return cv2.dilate(m, np.ones((2 * pad + 1, 2 * pad + 1), np.uint8)) > 0


EDGE = {"on": True, "tol": 0.01, "tip_max": None}  # 交界规则的参数（开关、深度容差、指尖门槛），对比实验和设置用


def edge_touch(dm, hm_side, hm_all, obj_mask, tol=0.025):
    """手和物体在交界处是否贴在一起：取物体轮廓里紧挨着这只手的一圈像素，
    比较它们的深度和旁边手的深度。真握着时两者几乎一样；手悬在物体前面时，物体明显更远。
    返回 (贴合的像素数, 交界处有效像素数)"""
    h, w = dm.shape
    r = max(3, int(round(w / 80)))
    k = np.ones((2 * r + 1, 2 * r + 1), np.uint8)
    ring = (cv2.dilate(hm_side.astype(np.uint8), k) > 0) & ~hm_all & obj_mask & (dm > 0)
    if not ring.any():
        return 0, 0
    hd = np.where(hm_side & (dm > 0), dm, 9.0).astype(np.float32)
    near = cv2.erode(hd, k)  # 每个像素附近最近的手的深度
    ok = ring & (near < 9.0)
    n = int(ok.sum())
    if not n:
        return 0, 0
    return int((np.abs(dm[ok] - near[ok]) < tol).sum()), n


def hand_resting(dm, hm_side, hm_all, obj_union, cam, W, H, rng, rest_h=0.05):
    """这只手是不是平放在支撑面（桌面）上：在手周围找桌面像素拟合一个平面，看手离平面有多高。
    手平放时整只手都贴着桌面（九成像素离桌面不到 5 cm，含手腕厚度）；握住、拿起物体时手会立起来或抬高（实测 8–10 cm）。
    手旁边找不到足够的桌面（货架、悬空操作、深度缺失）就返回 None：不确定，不做限制。"""
    h, w = dm.shape
    r1 = max(3, int(round(w / 64)))
    r2 = max(r1 + 4, int(round(w / 16)))
    k1, k2 = np.ones((2 * r1 + 1,) * 2, np.uint8), np.ones((2 * r2 + 1,) * 2, np.uint8)
    hs8 = hm_side.astype(np.uint8)
    band = (cv2.dilate(hs8, k2) > 0) & ~(cv2.dilate(hs8, k1) > 0) & ~hm_all & ~obj_union & (dm > 0)
    vs, us = np.nonzero(band)
    if vs.size < 300:
        return None
    if vs.size > 3000:
        sel = rng.choice(vs.size, 3000, replace=False)
        vs, us = vs[sel], us[sel]
    P = _backproject(us.astype(float), vs.astype(float), dm[vs, us].astype(float), cam, W, H, w, h)
    best, bn = None, 0
    for _ in range(60):  # RANSAC：容忍旁边的杂物和背景
        a, b, c = P[rng.choice(len(P), 3, replace=False)]
        n = np.cross(b - a, c - a)
        nn = np.linalg.norm(n)
        if nn < 1e-9:
            continue
        n = n / nn
        cnt = int((np.abs((P - a) @ n) < 0.01).sum())
        if cnt > bn:
            best, bn = (a, n), cnt
    if best is None or bn < 0.5 * len(P):
        return None
    a, n = best
    if -a @ n < 0:  # 法向朝相机一侧：离桌面的高度为正
        n = -n
    hv, hu = np.nonzero(hm_side & (dm > 0))
    if hv.size < 50:
        return None
    if hv.size > 2000:
        sel = rng.choice(hv.size, 2000, replace=False)
        hv, hu = hv[sel], hu[sel]
    Q = _backproject(hu.astype(float), hv.astype(float), dm[hv, hu].astype(float), cam, W, H, w, h)
    ht = (Q - a) @ n
    return float(np.quantile(ht, 0.9)) < rest_h


def hand_still(hands_frames, fi, side, rgb_fps, W, win=0.25, tol=0.015):
    """这只手前后 win 秒内基本没动（手腕和食指尖在画面里移动不到画面宽度的 1.5%）。
    推着物体在桌面上滑、或手正伸过去时都在动；头转动时整个画面在动，也会判成“在动”，从而不做限制"""
    k = max(1, int(round(win * rgb_fps)))
    def pt(f, j):
        if not (0 <= f < len(hands_frames)):
            return None
        for h in hands_frames[f] or []:
            if h[0] == side and not h[3]:
                p = h[2][j] if j < len(h[2]) else None
                return p if p is not None and p[0] is not None else None
        return None
    for j in (0, 8):
        a, b = pt(fi - k, j), pt(fi + k, j)
        if a is None or b is None:
            return False
        if ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 > tol * W:
            return False
    return True


def contacts(trk, masks, hands_frames, rgb_fps, depth_fn=None, cam=None, gap=0.8, frame_fn=None, obj3d=None, min_dur=0.4,
             tips3d=None, dist_thr=0.02, mode3d=True, tool_min=4.0, debug=None, edge_tol=None):
    """接触判断。
    有深度和内参时：把物体掩码内的像素还原成三维点，计算指尖到物体表面的三维距离；
      ≥2 个指尖在 dist_thr（默认 2 cm）以内，或 1 个指尖在一半距离以内，算接触。一只手可以同时接触多个物体。
      指尖三维位置优先用厂商三维关键点（tips3d），没有就用指尖处的深度反投影。
    物体点云先扣掉手占的像素（手挡在物体前面时，那些像素测到的是手的深度，会造成“手悬在物体前面也算接触”）。
    某一帧某个物体扣掉手之后有效深度点太少（透明、反光、被手几乎全挡住），这一帧对它单独用二维规则，并记下来、降低把握。
    没有深度时：整条退回二维规则（≥2 个指尖落在外扩的物体轮廓里）。
    之后识别“工具”：一只手稳定握住同一个物体超过 tool_min 秒，这期间这只手对其他物体的接触记为“通过工具作用”，不再单独切段。"""
    n = len(trk["t"])
    K = len(trk["objects"])
    mw, mh = trk["mask_size"]
    W, H = trk["src_size"]
    sx, sy = mw / W, mh / H
    ks = max(3, int(round(15 * mw / 320)) | 1)  # 二维外扩容差：320 像素宽时 15 像素
    kern = np.ones((ks, ks), np.uint8)
    on = {(side, k): np.zeros(n, bool) for side in ("left", "right") for k in range(K)}
    near = np.zeros((K, n), bool)  # 有指尖落在物体附近（二维），用于判断遮挡
    mind = {(side, k): np.full(n, np.nan) for side in ("left", "right") for k in range(K)}
    by2d = {(side, k): np.zeros(n, bool) for side in ("left", "right") for k in range(K)}  # 这一帧是用二维规则判的
    use3d = mode3d and depth_fn is not None and cam is not None
    rng = np.random.default_rng(0)
    for i, t in enumerate(trk["t"]):
        fi = min(int(round(t * rgb_fps)), len(hands_frames) - 1)
        hs = hands_frames[fi] if fi >= 0 else []
        if not hs:
            continue
        dm = depth_fn(t) if use3d else None
        hmask = hand_mask(hs, dm.shape, W, H) if dm is not None else None
        hside = {}
        rest = {}  # 这只手是否平放在桌面上（用来排除“手放在物体旁边”被交界规则当成接触）
        ounion = None
        for k in range(K):
            mk = masks[i, k]
            if not mk.any():
                continue
            md = cv2.dilate(mk.astype(np.uint8), kern) > 0
            cloud = None
            for side, conf, pts, ex in hs:
                if ex:
                    continue
                inside = 0
                for j in FINGERTIPS:
                    p = pts[j]
                    if p is None or p[0] is None:
                        continue
                    u, v = int(p[0] * sx), int(p[1] * sy)
                    if 0 <= u < mw and 0 <= v < mh and md[v, u]:
                        inside += 1
                if inside:
                    near[k, i] = True
                if dm is None:  # 二维规则
                    if inside >= 2:
                        on[(side, k)][i] = True
                    continue
                if not inside:  # 指尖离物体轮廓都很远，不必算三维
                    continue
                if cloud is None:
                    dh, dw = dm.shape
                    mkd = cv2.resize(mk.astype(np.uint8), (dw, dh), interpolation=cv2.INTER_NEAREST) > 0
                    vs, us = np.nonzero(mkd & (dm > 0) & ~hmask)
                    if vs.size < max(10, 0.05 * mkd.sum()):  # 扣掉手以后有效深度太少：这一帧改用二维判断
                        cloud = np.zeros((0, 3))
                    else:
                        if vs.size > 2000:
                            sel = rng.choice(vs.size, 2000, replace=False)
                            vs, us = vs[sel], us[sel]
                        cloud = _backproject(us.astype(float), vs.astype(float), dm[vs, us].astype(float), cam, W, H, dw, dh)
                # 交界处深度贴合：手把物体接触面挡住了（握住时常见），指尖关键点量不到，就看手和物体边缘是否贴在一起
                if side not in hside:
                    hside[side] = hand_mask([[side, conf, pts, ex]], dm.shape, W, H)
                tch, ring_n = edge_touch(dm, hside[side], hmask, mkd, tol=edge_tol if edge_tol else EDGE["tol"])
                # 贴合像素要够多（按深度图分辨率换算，640 宽时 40 个），且占交界的 8% 以上；交界圈只伸出手外约 2 厘米，旁边隔开的物体进不来
                edge_ok = EDGE["on"] and inside >= 1 and tch >= 40 * (dm.shape[1] / 640) ** 2 and tch >= 0.08 * ring_n
                if edge_ok and EDGE["tip_max"] and cloud.shape[0]:
                    # 指尖门槛：真握住时指尖哪怕被挡住、位置是估的，也在物体表面几厘米以内；离得更远说明交界“贴合”是深度边缘的假象
                    tp0 = (tips3d[fi].get(side) if tips3d is not None and fi < len(tips3d) and tips3d[fi] else None) or tips_from_depth(pts, dm, cam, W, H)
                    d0 = [float(np.min(np.linalg.norm(cloud - np.array(qq), axis=1))) for qq in tp0 if qq is not None]
                    if d0 and min(d0) > EDGE["tip_max"]:
                        edge_ok = False
                        if debug is not None:
                            debug.append((round(t, 2), side, trk["objects"][k]["id"], "gate", round(min(d0) * 100, 1)))
                if edge_ok:
                    # 手平放在桌上、紧挨着物体底部、而且没在动时，手和物体在交界处深度也连着，但并没有在操作它：
                    # 这种情况交界规则不算，只看指尖三维距离（推着物体走的手在动，不受影响）
                    if side not in rest:
                        if ounion is None:
                            ounion = np.zeros(dm.shape, bool)
                            for kk in range(K):
                                if masks[i, kk].any():
                                    ounion |= cv2.resize(masks[i, kk].astype(np.uint8), (dm.shape[1], dm.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
                        rest[side] = hand_still(hands_frames, fi, side, rgb_fps, W) and bool(hand_resting(dm, hside[side], hmask, ounion, cam, W, H, rng))
                    if rest[side]:
                        edge_ok = False
                        if debug is not None:
                            debug.append((round(t, 2), side, trk["objects"][k]["id"], "rest", tch, ring_n))
                if edge_ok:
                    on[(side, k)][i] = True
                    if debug is not None:
                        debug.append((round(t, 2), side, trk["objects"][k]["id"], "edge", tch, ring_n))
                    continue
                if cloud.shape[0] == 0:
                    if inside >= 2:
                        on[(side, k)][i] = True
                        by2d[(side, k)][i] = True
                    continue
                tp = (tips3d[fi].get(side) if tips3d is not None and fi < len(tips3d) and tips3d[fi] else None) or tips_from_depth(pts, dm, cam, W, H)
                ds = [float(np.min(np.linalg.norm(cloud - np.array(q), axis=1))) for q in tp if q is not None]
                if not ds:
                    continue
                ds.sort()
                if debug is not None:
                    debug.append((round(t, 2), side, trk["objects"][k]["id"], [round(d * 100, 1) for d in ds[:2]], int(cloud.shape[0])))
                mind[(side, k)][i] = ds[0]
                # 指尖关键点在手指中心，手指本身有约 8 毫米厚：扣掉手的像素后，真接触时最近的物体点离关键点约一个手指半径。
                # 所以“接触距离”按手指表面到物体表面算，判断时加上这段手指厚度
                if (len(ds) >= 2 and ds[1] < dist_thr + FINGER_R) or ds[0] < dist_thr / 2 + FINGER_R:
                    on[(side, k)][i] = True
    ev = []
    fps = trk["fps"]
    touched = np.zeros((K, n), bool)
    for (side, k), b in on.items():
        touched[k] |= b
    for (side, k), b in on.items():
        for a, c in _runs(b, fill=int(fps * gap), min_len=max(2, int(round(fps * min_dur)))):
            o = trk["objects"][k]
            mv = measure_move(trk, k, a, c, near, touched, depth_fn, cam, frame_fn, obj3d)
            dmin = mind[(side, k)][a:c + 1]
            ap = _aperture(hands_frames, tips3d, side, trk["t"][a], trk["t"][c], rgb_fps, W)
            closed = ap is not None and ap < 0.06
            kind = "抓取并移动" if mv["moved"] else ("抓住" if closed else "触碰")
            seg_on = b[a:c + 1]
            p2d = float(by2d[(side, k)][a:c + 1][seg_on].mean()) if use3d and seg_on.any() else (1.0 if not use3d else 0.0)
            method = "二维轮廓" if not use3d else ("三维距离" if p2d == 0 else "二维轮廓（深度缺失）" if p2d > 0.7 else "三维距离（部分帧二维）")
            if use3d and p2d > 0.3:  # 一大半帧没有深度可用：把握降一级
                mv["conf"] = {"高": "中", "中": "低"}.get(mv.get("conf"), mv.get("conf"))
            ev.append({"hand": side, "obj": o["id"], "label": o["label"], "t0": trk["t"][a], "t1": trk["t"][c], **mv,
                       "kind": kind, "aperture_m": None if ap is None else round(ap, 3),
                       "min_dist_cm": None if np.all(np.isnan(dmin)) else round(float(np.nanmin(dmin)) * 100, 1),
                       "method": method, "frames_2d_pct": round(p2d * 100)})
    ev.sort(key=lambda e: e["t0"])
    mark_tools(ev, tool_min)
    return ev


def mark_tools(ev, tool_min=4.0):
    """工具：一只手合拢手指（或明显拿着走）、连续握住同一个物体超过 tool_min 秒（例如掸子、刷子、勺子）。
    这期间这只手对其他物体的接触，多半是手顺带擦过、或工具碰到的东西，记为 via_tool（作用对象），不再单独产生切点"""
    if not tool_min or tool_min <= 0:
        return ev
    for e in ev:
        if e["kind"] in ("抓住", "抓取并移动") and e["t1"] - e["t0"] >= tool_min:
            # 还要确认真的握在手里：手指合拢，或者物体明显被带着走了（10 cm 以上）。
            # 手只是张开搭在物体旁边、物体被碰得晃了几厘米，不算工具，免得它“碰到”的邻近物体都被连带算成接触
            ap, disp = e.get("aperture_m"), e.get("disp_cm") or 0
            if (ap is not None and ap < 0.07) or (e.get("moved") and disp >= 10):
                e["tool"] = True
    for e in ev:
        if e.get("tool"):
            continue
        for t in ev:
            if t.get("tool") and t["hand"] == e["hand"] and t["obj"] != e["obj"] and e["t0"] >= t["t0"] - 0.2 and e["t1"] <= t["t1"] + 0.2:
                e["via_tool"] = t["obj"]
                e["via_tool_label"] = t["label"]
                break
    return ev


def _aperture(hands_frames, tips3d, side, t0, t1, fps, W):
    """拇指尖到食指尖的距离（米）在接触期间的中位数：小于约 6 cm 说明手指合拢、在捏 / 握。
    有三维关键点时直接算；只有二维时按手掌宽度（食指根到小指根约 7 cm）换算"""
    vals = []
    for fi in range(int(t0 * fps), int(t1 * fps) + 1, max(1, int(fps / 10))):
        if tips3d is not None and fi < len(tips3d) and tips3d[fi] and tips3d[fi].get(side):
            tp = tips3d[fi][side]
            if tp[0] is not None and tp[1] is not None:
                vals.append(float(np.linalg.norm(np.array(tp[0]) - np.array(tp[1]))))
                continue
        if fi < len(hands_frames):
            for sd, conf, pts, ex in hands_frames[fi] or []:
                if sd != side or ex:
                    continue
                try:
                    th, ix, i5, p17 = pts[4], pts[8], pts[5], pts[17]
                    palm = np.hypot(i5[0] - p17[0], i5[1] - p17[1])
                    if palm > 5:
                        vals.append(float(np.hypot(th[0] - ix[0], th[1] - ix[1]) / palm * 0.07))
                except Exception:
                    pass
    return float(np.median(vals)) if vals else None


def _cam_shift(frame_fn, t0, t1):
    """整幅画面的平移（头动造成的），用相位相关估计。返回像素 (dx, dy)，原图尺度"""
    try:
        f0, f1 = frame_fn(t0), frame_fn(t1)
        if f0 is None or f1 is None:
            return None
        sc = 320 / f0.shape[1]
        g0 = cv2.cvtColor(cv2.resize(f0, None, fx=sc, fy=sc), cv2.COLOR_BGR2GRAY).astype(np.float32)
        g1 = cv2.cvtColor(cv2.resize(f1, None, fx=sc, fy=sc), cv2.COLOR_BGR2GRAY).astype(np.float32)
        win = cv2.createHanningWindow(g0.shape[::-1], cv2.CV_32F)
        (dx, dy), resp = cv2.phaseCorrelate(g0, g1, win)
        return (dx / sc, dy / sc) if resp > 0.05 else None
    except Exception:
        return None


def measure_move(trk, k, a, c, near, touched, depth_fn, cam, frame_fn, obj3d=None):
    """物体在这次接触里到底动了没有、动了多少。
    1) 比较“手伸过来之前”和“手离开之后”物体完整露出的样子，避免手的遮挡让轮廓中心偏移；
    2) 减去头部运动：用这段时间里没人碰的其他物体（或整幅画面）的平移作为参照；
    3) 大物体按自身尺寸的比例判断，抖动不算移动。"""
    n = len(trk["t"])
    W, H = trk["src_size"]
    o = trk["objects"][k]
    fps = trk["fps"]
    lim = int(fps * 2)
    pre = next((j for j in range(a - 1, max(-1, a - lim), -1) if not near[k, j] and o["centroid"][j]), None)
    post = next((j for j in range(c + 1, min(n, c + lim)) if not near[k, j] and o["centroid"][j]), None)
    clean = pre is not None and post is not None
    if pre is None:
        pre = next((j for j in range(max(0, a - 3), a + 1)[::-1] if o["centroid"][j]), None)
    if post is None:
        post = next((j for j in range(c, min(n, c + 4)) if o["centroid"][j]), None)
    if pre is None or post is None:
        return {"p0": None, "p1": None, "tp0": trk["t"][a], "tp1": trk["t"][c], "disp_px": 0.0, "disp_cm": None,
                "moved": False, "conf": "低", "ref": "无"}
    p0, p1 = o["centroid"][pre], o["centroid"][post]
    raw = np.array(p1) - np.array(p0)
    # 有三维位置、且至少 3 个没被碰过的物体可作参照：用它们求出相机（头）在两个时刻之间的刚体运动，
    # 把物体的前后三维位置放到同一坐标系里比较。这种方法连头的转动也能扣除
    if obj3d is not None:
        P0 = obj3d[k][pre] if obj3d[k][pre] is not None else None
        P1 = obj3d[k][post] if obj3d[k][post] is not None else None
        refs = [(obj3d[k2][pre], obj3d[k2][post]) for k2 in range(len(trk["objects"]))
                if k2 != k and not touched[k2, pre:post + 1].any() and obj3d[k2][pre] is not None and obj3d[k2][post] is not None]
        if P0 is not None and P1 is not None and len(refs) >= 3:
            A = np.array([r[0] for r in refs]); B = np.array([r[1] for r in refs])
            ca, cb = A.mean(0), B.mean(0)
            U, _, Vt = np.linalg.svd((A - ca).T @ (B - cb))
            D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
            R = Vt.T @ D @ U.T
            tvec = cb - R @ ca
            resid = float(np.median(np.linalg.norm((A @ R.T + tvec) - B, axis=1)))
            if resid < 0.03:  # 参照物体彼此一致（误差 < 3 cm），这个估计可信
                dv = np.array(P1) - (R @ np.array(P0) + tvec)   # 放下后的相机坐标系：x 向右，z 向远处
                d3 = float(np.linalg.norm(dv))
                b = o["bbox"][pre] or o["bbox"][a]
                size_cm = None
                if b and cam is not None:
                    size_cm = float(np.hypot(b[2] - b[0], b[3] - b[1])) * P0[2] / cam["fx"] * 100
                thr = max(3.0, 0.15 * (size_cm or 0))
                disp_cm = round(d3 * 100, 1)
                margin = disp_cm / thr
                return {"p0": o["centroid"][pre], "p1": o["centroid"][post], "tp0": trk["t"][pre], "tp1": trk["t"][post],
                        "disp_px": None, "disp_cm": disp_cm, "moved": bool(disp_cm > thr),
                        "conf": "高" if (clean and (margin > 2 or margin < 0.5)) else "中", "ref": "其他物体（三维）",
                        "dir_v": [round(float(dv[0]) * 100, 1), round(float(dv[2]) * 100, 1)],
                        "ref_err_cm": round(resid * 100, 1)}
    # 参照：这段时间没被碰过的其他物体
    shifts = []
    for k2, o2 in enumerate(trk["objects"]):
        if k2 == k or touched[k2, pre:post + 1].any():
            continue
        q0, q1 = o2["centroid"][pre], o2["centroid"][post]
        if q0 and q1:
            shifts.append(np.array(q1) - np.array(q0))
    ref = "其他物体"
    if shifts:
        cs = np.median(np.array(shifts), axis=0)
    else:
        cs = _cam_shift(frame_fn, trk["t"][pre], trk["t"][post]) if frame_fn else None
        ref = "整幅画面" if cs is not None else "无"
        cs = np.array(cs) if cs is not None else np.zeros(2)
    d = raw - cs
    disp_px = float(np.hypot(*d))
    b = o["bbox"][pre] or o["bbox"][a]
    size_px = float(np.hypot(b[2] - b[0], b[3] - b[1])) if b else 0.0
    disp_cm = size_cm = None
    if cam is not None and depth_fn is not None:
        dm = depth_fn(trk["t"][pre])
        if dm is not None:
            u, v = int(p0[0] * dm.shape[1] / W), int(p0[1] * dm.shape[0] / H)
            win = dm[max(0, v - 8):v + 9, max(0, u - 8):u + 9]
            z = np.median(win[win > 0]) if (win > 0).any() else 0
            if z > 0:
                disp_cm = round(disp_px * z / cam["fx"] * 100, 1)
                size_cm = size_px * z / cam["fx"] * 100
    if disp_cm is not None:
        thr = max(3.0, 0.15 * (size_cm or 0))
        moved = disp_cm > thr
        margin = disp_cm / thr
    else:
        thr = max(0.04 * W, 0.15 * size_px)
        moved = disp_px > thr
        margin = disp_px / thr
    conf = "高" if (clean and ref != "无" and (margin > 2 or margin < 0.5)) else "中" if (clean or ref != "无") else "低"
    return {"p0": p0, "p1": p1, "tp0": trk["t"][pre], "tp1": trk["t"][post], "disp_px": round(disp_px, 1), "disp_cm": disp_cm,
            "cam_shift_px": [round(float(cs[0]), 1), round(float(cs[1]), 1)], "moved": bool(moved), "conf": conf, "ref": ref,
            "dir_v": [round(float(d[0]), 1), round(float(-d[1]), 1)]}  # 画面：向右为正；画面上方 = 离人更远，为正


def segments_from_contacts(ev, dur, min_len=1.0):
    """接触区间 → 原子动作段：接触开始和结束都是切点。
    切点离得太近时按重要性取舍，不再“先到先得”：
      拿起/放下被移动的物体 = 3 分，抓住没移动 = 2 分，只是碰一下（比如放东西时碰到碗）= 1 分。
    从高分到低分依次收下切点，和已收下的切点距离不够 min_len 的就丢掉；
    两个都是 3 分的切点（放下一支笔、紧接着拿起另一支）允许靠得更近（min_len 的一半）。"""
    dur = float(dur)
    w_of = lambda e: 3 if e.get("moved") else (2 if e.get("kind") == "抓住" else 1)
    cand = {}
    for e in ev:
        if e.get("via_tool"):  # 握着工具时这只手碰到的其他物体：只记录，不切段
            continue
        for t in (round(e["t0"], 3), round(e["t1"], 3)):
            if 0 < t < dur:
                cand[t] = max(cand.get(t, 0), w_of(e))
    kept = {0.0: 9, dur: 9}
    for t, w in sorted(cand.items(), key=lambda x: (-x[1], x[0])):
        if all(abs(t - k) >= (min_len / 2 if w >= 3 and kw >= 3 else min_len) for k, kw in kept.items()):
            kept[t] = w
    merged = sorted(kept)
    segs = []
    for a, b in zip(merged[:-1], merged[1:]):
        facts = [e for e in ev if e["t0"] < b - 0.05 and e["t1"] > a + 0.05]
        segs.append({"t0": round(a, 3), "t1": round(b, 3), "facts": facts, "cut": "接触变化" if a > 0 else "开始"})
    # 相邻两段接触的是同一批（同一只手、同一个物体的同一次接触）→ 合并
    key = lambda sg: tuple(sorted((e["hand"], e["obj"], e["t0"]) for e in sg["facts"]))
    out = []
    for sg in segs:
        if out and sg["facts"] and key(sg) == key(out[-1]):
            out[-1]["t1"] = sg["t1"]
        else:
            out.append(sg)
    return out[:60]


# ============================================================ 3a. 像素掩码的用途 ====
def objects_3d(trk, masks, depth_fn, cam, W, H):
    """每个物体每个跟踪帧的三维位置（相机坐标系，米）：掩码内像素深度的中位数 + 掩码中心反投影"""
    n, K = masks.shape[0], masks.shape[1]
    out = [[None] * n for _ in range(K)]
    if depth_fn is None or cam is None:
        return out
    for i, t in enumerate(trk["t"]):
        dm = depth_fn(t)
        if dm is None:
            continue
        for k in range(K):
            mk = masks[i, k]
            if not mk.any():
                continue
            md = cv2.resize(mk.astype(np.uint8), (dm.shape[1], dm.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
            md = cv2.erode(md.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0  # 去掉边缘，边缘深度常混到背景
            z = dm[md & (dm > 0)]
            c = trk["objects"][k]["centroid"][i]
            if z.size < 10 or not c:
                continue
            zz = float(np.median(z))
            ax = W / float(cam["w"]) if cam.get("w") else 1.0  # 标定分辨率和画面分辨率不同时等比换算
            ay = H / float(cam["h"]) if cam.get("h") else ax
            x = (c[0] - cam["cx"] * ax) * zz / (cam["fx"] * ax)
            y = (c[1] - cam["cy"] * ay) * zz / (cam["fy"] * ay)
            out[k][i] = [round(x, 4), round(y, 4), round(zz, 4)]
    return out


def mask_polygons(trk, masks, eps=1.0):
    """把每帧每个物体的掩码转成多边形（原图像素坐标），给浏览页画半透明轮廓用，比传掩码图小得多"""
    mw, mh = trk["mask_size"]
    W, H = trk["src_size"]
    sx, sy = W / mw, H / mh
    out = []
    for i in range(masks.shape[0]):
        fr = []
        for k in range(masks.shape[1]):
            mk = masks[i, k]
            if not mk.any():
                fr.append(None)
                continue
            cs, _ = cv2.findContours(mk.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            polys = []
            for c in cs:
                if cv2.contourArea(c) < 4:
                    continue
                c = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
                polys.append([[round(float(x * sx), 1), round(float(y * sy), 1)] for x, y in c])
            fr.append(polys or None)
        out.append(fr)
    return out


def rle_encode(mk):
    """COCO 格式的未压缩 RLE：按列优先展开，从 0 的游程开始。pycocotools 可直接解码"""
    flat = np.asfortranarray(mk.astype(np.uint8)).flatten(order="F")
    if flat.size == 0:
        return {"size": list(mk.shape), "counts": []}
    change = np.nonzero(np.diff(flat))[0] + 1
    runs = np.diff(np.concatenate([[0], change, [flat.size]])).tolist()
    if flat[0] == 1:
        runs = [0] + runs
    return {"size": [int(mk.shape[0]), int(mk.shape[1])], "counts": runs}


# ============================================================ 3b. 空间关系 =========
def _center_at(trk, k, ti):
    cs = trk["objects"][k]["centroid"]
    for d in range(0, 8):
        for j in (ti - d, ti + d):
            if 0 <= j < len(cs) and cs[j]:
                return cs[j]
    return None


def _box_at(trk, k, ti):
    bs = trk["objects"][k]["bbox"]
    for d in range(0, 8):
        for j in (ti - d, ti + d):
            if 0 <= j < len(bs) and bs[j]:
                return bs[j]
    return None


def relation(trk, oid, p, t):
    """物体在位置 p 时，相对最近的另一个物体在哪：返回 (另一个物体编号, 关系词)。
    第一视角画面：画面上方 = 离人更远（后方），下方 = 更近（前方）"""
    if not p:
        return None, ""
    ti = min(max(0, int(round(t * trk["fps"]))), len(trk["t"]) - 1)
    best = None
    for k, o in enumerate(trk["objects"]):
        if o["id"] == oid:
            continue
        b = _box_at(trk, k, ti)
        c = _center_at(trk, k, ti)
        if not c or not b:
            continue
        inside = b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3]
        d = 0 if inside else np.hypot(p[0] - c[0], p[1] - c[1])
        if best is None or d < best[0]:
            best = (d, o["id"], c, inside)
    if best is None:
        return None, ""
    _, other, c, inside = best
    if inside:
        return other, "上面或里面"
    dx, dy = p[0] - c[0], p[1] - c[1]
    if abs(dx) >= abs(dy):
        return other, "右侧" if dx > 0 else "左侧"
    return other, "前方（靠近自己）" if dy > 0 else "后方（远离自己）"


def move_text(trk, e):
    """把一次“移动了物体”的接触翻译成一句空间描述和一个去向（规则算出来的）"""
    if not e.get("moved") or not trk:
        return "", ""
    o0, r0 = relation(trk, e["obj"], e.get("p0"), e.get("tp0", e["t0"]))
    o1, r1 = relation(trk, e["obj"], e.get("p1"), e.get("tp1", e["t1"]))
    dist = f"约 {e['disp_cm']} 厘米" if e.get("disp_cm") else ""
    if not o1:
        return (f"{e['obj']} 被移动{dist}" if dist else ""), ""
    target = f"放到 {o1} {r1}"
    if o0 and (o0 != o1 or r0 != r1):
        return f"{e['obj']} 从 {o0} {r0} 移到 {o1} {r1}" + (f"（{dist}）" if dist else ""), target
    return f"{e['obj']} 在 {o1} {r1} 附近调整位置" + (f"（{dist}）" if dist else ""), target


def overlay_marks(img, trk, masks, t, ids):
    """Set-of-Mark：在画面上用 SAM 2 轮廓描出物体并标上编号，让模型“看着编号说话”"""
    if trk is None or masks is None or not ids:
        return img
    out = img.copy()
    h, w = out.shape[:2]
    ti = min(max(0, int(round(t * trk["fps"]))), len(trk["t"]) - 1)
    lw = max(2, w // 400)
    fs = max(0.5, w / 1400)
    for k, o in enumerate(trk["objects"]):
        if o["id"] not in ids:
            continue
        mk = masks[ti, k]
        if not mk.any():
            continue
        m = cv2.resize(mk.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        col = COLORS[k % len(COLORS)]
        cv2.drawContours(out, cs, -1, col, lw, cv2.LINE_AA)
        ys, xs = np.nonzero(m)
        x, y = int(xs.min()), int(ys.min())
        (tw, th), _ = cv2.getTextSize(o["id"], cv2.FONT_HERSHEY_SIMPLEX, fs, 2)
        y = max(th + 6, y)
        cv2.rectangle(out, (x, y - th - 6), (x + tw + 8, y), col, -1)
        cv2.putText(out, o["id"], (x + 4, y - 4), cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def near_ids(trk, ids, t, n=2):
    """和这些物体离得最近的另外 n 个物体（一起标出来，方便模型说“放到 O3 旁边”）"""
    if not trk or not ids:
        return set(ids)
    ti = min(max(0, int(round(t * trk["fps"]))), len(trk["t"]) - 1)
    cen = {o["id"]: _center_at(trk, k, ti) for k, o in enumerate(trk["objects"])}
    ref = [cen[i] for i in ids if cen.get(i)]
    if not ref:
        return set(ids)
    dist = sorted((min(np.hypot(c[0] - r[0], c[1] - r[1]) for r in ref), i) for i, c in cen.items() if c and i not in ids)
    return set(ids) | {i for _, i in dist[:n]}


# ============================================================ 4. 逐段语义 =========
SEG2_PROMPT = """你在为机器人训练数据做动作标注。下面是一段第一视角操作视频中的一个片段（{t0:.1f}–{t1:.1f} 秒），按时间顺序给了 {n} 帧，每帧左下角标了时刻{crop}。{marks}
任务：{task}
这段时间画面里出现过的物体（编号 · 名称）：
{inv}
系统根据手部关键点、物体轮廓和深度测得的事实（是测量值，标了把握程度；把握高的一般可信，如果画面明显不支持，以画面为准，并在 evidence 里写明）：
{facts}
移动方向以上面测得的事实为准（左右指戴相机的人自己的左右，已扣除头部转动）；事实里没写方向，就不要在描述里写“向左/向右”。
前面几段已标注为：
{prev}
动作词表（verb 只能从这里选一个）：
{verbs}
如果这段时间手没有在做事，verb 填“静止”。只输出 JSON：
手里操作的物体如果不在上面的物体清单里（例如中途从抽屉里拿出来的剪刀），object 留空，不要借用别的编号，只在 object_name 里写它的名称。
{{"hand": "左手/右手/双手/无", "verb": "词表里的一个词", "object": "物体编号，如 O2；不在清单里就空", "object_name": "这个物体的名称，照画面里看到的写（例如 红柄剪刀）", "target": "放到哪里或相对哪个物体（例如 放到 O3 右侧）；没有就空", "description": "一句不超过 25 字的具体描述，写清哪只手、哪个物体、从哪到哪", "success": true 或 false 或 null, "evidence": "判断是否完成的依据，不超过 15 字"}}"""

SUMMARY_PROMPT = """下面是一段操作视频按时间顺序的动作标注。
{task_line}
{lines}
请完成四件事：
1. 把这些动作归纳成 3 到 8 个高层步骤（每步一句话，写清做了什么）；
2. 用一句不超过 20 字的中文祈使句写出这段视频完成的任务（像给机器人下的指令，例如“把托盘里的电池拿出来”）；
3. 判断整个任务是否完成{verify_ask}；
4. 把编号连续、属于同一件事的段合并成“子任务”（例如 #9 到 #14 都是在用掸子扫同一层书架，就合成一个）。
   每个子任务给出起止段号和一句不超过 20 字的描述；必须按顺序覆盖全部段号，不能跳号、不能重叠；不该合并的段单独成组。
只输出 JSON：{{"steps": ["步骤1", "步骤2"], "task_seen": "任务指令", "task_success": true 或 false 或 null, "summary": "一句话总结",
"groups": [{{"from": 1, "to": 3, "title": "子任务描述"}}]{verify_keys}}}"""

VERIFY_ASK = "；并对照登记的任务名给出一致性：一致 = 说的是同一件事；大体一致 = 方向相同，只是说法或粗细不同（例如登记“整理桌面”，实际是把瓶子排好）；不一致 = 做的明显是另一件事"
VERIFY_KEYS = ', "consistency": "一致 / 大体一致 / 不一致", "reason": "判断依据，不超过 25 字"'


def summary_prompt(registered, lines):
    """通读全部分段：归纳步骤、写出任务指令、（有登记任务名时）核对是否一致"""
    if registered:
        return SUMMARY_PROMPT.format(task_line=f"数据里登记的任务名是：“{registered}”（可能是英文或文件名式的写法）。",
                                     lines=lines, verify_ask=VERIFY_ASK, verify_keys=VERIFY_KEYS)
    return SUMMARY_PROMPT.format(task_line="（数据里没有登记任务名）", lines=lines, verify_ask="", verify_keys="")



def move_dir(e):
    """规则算出的移动方向（已扣除头动）。左右 = 戴相机的人自己的左右；远近 = 离人更远 / 更近。
    只写占主导的分量（不到最大分量 40% 的忽略）"""
    v = e.get("dir_v")
    if not e.get("moved") or not v:
        return ""
    h, f = v
    m = max(abs(h), abs(f))
    if m <= 0:
        return ""
    out = []
    if abs(h) >= 0.4 * m:
        out.append("向右" if h > 0 else "向左")
    if abs(f) >= 0.4 * m:
        out.append("向远处" if f > 0 else "向自己这边")
    return "、".join(out)


def fix_dir(desc, facts, oid=""):
    """模型写的左右方向和规则测得的不一致时，以规则为准改掉；规则判断没有左右分量时，删掉模型写的左右"""
    import re as _re
    e = next((x for x in facts if x.get("moved") and x.get("obj") == oid and move_dir(x)), None) or \
        next((x for x in facts if x.get("moved") and move_dir(x)), None)
    if not desc or e is None:
        return desc
    d = move_dir(e)
    lr = "右" if "向右" in d else ("左" if "向左" in d else "")
    desc = _re.sub(r"(向|往|朝)(左|右)", (lambda m: m.group(1) + lr) if lr else "", desc)
    desc = _re.sub(r"(左|右)(移|挪)", (lambda m: lr + m.group(2)) if lr else (lambda m: m.group(2)), desc)
    return desc


def fact_text(facts, objs_by_id, trk=None):
    if not facts:
        return "  （这段时间手没有接触任何已识别的物体）"
    hand = {"left": "左手", "right": "右手"}
    out = []
    for e in facts:
        cf = f"（把握{e['conf']}）" if e.get("conf") else ""
        dr = move_dir(e)
        mv = ((f"，{e['obj']} 被{dr}移动约 {e['disp_cm']} 厘米" if e.get("disp_cm") else f"，物体位置有明显变化{'（' + dr + '）' if dr else ''}")
              if e["moved"] else "，物体位置基本没变") + cf
        kd = {"抓取并移动": "抓住并移动了", "抓住": "抓住（手指合拢）", "触碰": "触碰（手指未合拢）"}.get(e.get("kind"), "接触")
        if e.get("via_tool"):
            out.append(f"  - {hand[e['hand']]} 拿着 {e['via_tool']}（{e.get('via_tool_label', '')}）期间，在 {e['t0']:.1f}–{e['t1']:.1f} 秒碰到 {e['obj']}（{e['label']}）"
                       f"（可能是工具作用的对象，也可能只是手擦过）{mv}")
            continue
        tl = "，一直握着，像是在使用它当工具" if e.get("tool") else ""
        if "二维" in (e.get("method") or ""):
            cf += "（深度缺失，按画面二维位置判断）"
        out.append(f"  - {hand[e['hand']]} 在 {e['t0']:.1f}–{e['t1']:.1f} 秒{kd} {e['obj']}（{e['label']}）{tl}{mv}")
        sp, _ = move_text(trk, e)
        if sp:
            out.append(f"    位置变化：{sp}")
    return "\n".join(out)


def crop_for(img, hands_row, facts, track_obj_box, pad=0.25):
    """把手和接触物体框在一起，裁一张放大图"""
    h, w = img.shape[:2]
    xs, ys = [], []
    for side, conf, pts, ex in hands_row or []:
        for p in pts:
            if p and p[0] is not None:
                xs.append(p[0]); ys.append(p[1])
    for b in track_obj_box:
        if b:
            xs += [b[0], b[2]]; ys += [b[1], b[3]]
    if not xs:
        return None
    x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    dx, dy = (x2 - x1) * pad + 20, (y2 - y1) * pad + 20
    x1, x2, y1, y2 = int(max(0, x1 - dx)), int(min(w, x2 + dx)), int(max(0, y1 - dy)), int(min(h, y2 + dy))
    if x2 - x1 < 40 or y2 - y1 < 40:
        return None
    return img[y1:y2, x1:x2]
