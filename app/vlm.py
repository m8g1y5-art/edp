"""视觉大模型：本地 Ollama，或外部 OpenAI 兼容接口（如阿里云百炼 DashScope）。
设置项 vlm_backend = "ollama" | "api"。上层代码只调用 status / need_model / ask / unload，不关心用的是哪种。"""
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import cv2

from .db import get_setting

VISION_HINT = re.compile(r"vl|vision|llava|bakllava|minicpm-v|moondream|gemma3|gemma4|llama3\.2-vision|llama4|qwen2\.5vl|qwen3-vl|qwen3\.[5-9]|granite3\.2-vision|mistral-small3", re.I)
_caps = {}  # 模型名 -> 能力列表（来自 /api/show，缓存）


def capabilities(name):
    """问 Ollama 这个模型支持什么：新版本会返回 ["completion", "vision", "thinking", ...]"""
    if name not in _caps:
        try:
            _caps[name] = _req("/api/show", {"model": name}, timeout=5).get("capabilities") or []
        except Exception:
            return None  # 连不上或老版本 Ollama：退回按名字判断，下次再问
    return _caps[name]


def _url(path):
    return get_setting("ollama_url").rstrip("/") + path


def _req(path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_url(path), data=data, headers={"Content-Type": "application/json"})
    # 本机地址不走系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


_last = {"t": 0, "v": None}


def backend():
    return get_setting("vlm_backend") or "ollama"


def api_status():
    """外部接口：只检查有没有填 Key 和模型名，不每次都联网（真正能不能用，点“测试模型”或跑任务时就知道）"""
    key, model = get_setting("api_key") or "", get_setting("api_model") or ""
    if not key:
        return {"ok": False, "err": "外部接口还没有填 API Key（设置页）", "models": [], "model": None, "backend": "api"}
    if not model:
        return {"ok": False, "err": "外部接口还没有填模型名（设置页）", "models": [], "model": None, "backend": "api"}
    from . import batchq
    return {"ok": True, "models": [{"name": model, "size": 0, "vision": True, "params": "外部接口"}], "model": model, "backend": "api",
            "batch": batchq.supports_batch(model)}


def status(retries=2):
    if backend() == "api":
        return api_status()
    return ollama_status(retries)


def ollama_status(retries=2):
    """返回 {ok, models:[{name,size,vision}], model(当前选用)}。
    Ollama 忙（正在加载模型）时偶尔响应慢：多试几次，并沿用 2 分钟内最后一次成功的结果。"""
    err = None
    for i in range(retries + 1):
        try:
            tags = _req("/api/tags", timeout=10)
            break
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1) if i < retries else 0)
    else:
        if _last["v"] and time.time() - _last["t"] < 120:
            return _last["v"]
        return {"ok": False, "err": f"连不上 Ollama（{get_setting('ollama_url')}）：{err}", "models": [], "model": None}
    ms = []
    for m in tags.get("models", []):
        fam = " ".join((m.get("details") or {}).get("families") or [])
        caps = capabilities(m["name"])
        hint = bool(VISION_HINT.search(m["name"]) or "clip" in fam or "mllama" in fam)
        vision = ("vision" in caps) if caps else hint
        ms.append({"name": m["name"], "size": m.get("size", 0), "vision": vision,
                   "params": (m.get("details") or {}).get("parameter_size")})
    want = get_setting("vlm_model")
    model = want if want and any(m["name"] == want for m in ms) else next((m["name"] for m in ms if m["vision"]), None)
    v = {"ok": True, "models": ms, "model": model}
    _last.update(t=time.time(), v=v)
    return v


def need_model():
    """处理步骤里用：连不上就报错（步骤记为失败、可重跑），连上但没有视觉模型返回 None（步骤跳过）"""
    s = status(retries=3)
    if not s["ok"]:
        raise RuntimeError(s["err"] + ("。请在设置页填好外部接口，然后重跑这一步" if backend() == "api" else "。请确认 Ollama 正在运行，然后重跑这一步"))
    return s["model"], s


def _b64(img, max_side=640):
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf.tobytes()).decode()


def _is_timeout(e):
    import socket
    return isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(e).lower()


def ask(model, prompt, images, timeout=180, max_side=None):
    from . import batchq
    if batchq.active() and backend() == "api":  # 批量模式：查缓存，没有就登记，稍后统一提交
        return batchq.ask(model, prompt, images, max_side)
    if backend() == "api":
        return api_ask(model, prompt, images, timeout, max_side)
    return ollama_ask(model, prompt, images, timeout, max_side)


def ollama_ask(model, prompt, images, timeout=180, max_side=None):
    """发图 + 提示词，要求返回 JSON。返回 (dict, 原始文本, 耗时秒)。
    不同模型 / 不同 Ollama 版本对 format、think 参数的支持不一样，失败时逐级放宽再试；
    超时说明这台机器算不动这么多图，不再原样重试，直接降到“只看一张小图”。"""
    # 所有尝试都关掉“思考”（推理过程很耗时间，对标注没有帮助）。
    # 只有 Ollama 明确报“不支持 think 参数”（很老的版本）时，才去掉这个参数再试。
    tries = [
        {"format": "json", "think": False, "max_side": 640},
        {"think": False, "max_side": 640},
        {"max_side": 448, "one": True, "think": False},
    ]
    no_think_param = False
    ctx = int(get_setting("vlm_ctx") or 8192)
    errs = []
    t0 = time.time()
    i = 0
    while i < len(tries):
        tr = tries[i]
        i += 1
        imgs = images[len(images) // 2:len(images) // 2 + 1] if tr.get("one") else images
        ms = max_side if (max_side and not tr.get("one")) else tr["max_side"]
        body = {"model": model, "stream": False, "options": {"temperature": 0.2, "num_ctx": ctx},
                "messages": [{"role": "user", "content": prompt, "images": [_b64(im, ms) for im in imgs]}]}
        if not imgs:
            body["messages"][0].pop("images")
        for k in ("format", "think"):
            if k in tr and not (k == "think" and no_think_param):
                body[k] = tr[k]
        try:
            res = _req("/api/chat", body, timeout=timeout if not tr.get("one") else max(60, timeout // 2))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode(errors="ignore")).get("error", "")
            except Exception:
                msg = ""
            errs.append(f"HTTP {e.code} {msg}".strip())
            if "think" in msg.lower() and not no_think_param:  # 老版本 Ollama 不认 think 参数：去掉它重试这一档
                no_think_param = True
                i -= 1
            continue
        except Exception as e:
            if _is_timeout(e):
                errs.append(f"超过 {timeout} 秒没有返回")
                if not tr.get("one") and len(images) > 1:
                    i = len(tries) - 1  # 直接跳到少图模式
                    continue
                break
            errs.append(str(e))
            continue
        msg = res.get("message") or {}
        txt = msg.get("content", "") or ""
        txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)  # 万一模型仍输出了思考过程，去掉
        if not txt.strip() and msg.get("thinking"):  # 有些思考模型把内容放在 thinking 里
            txt = msg["thinking"]
        j = parse_json(txt, strict=True)
        if j is not None:
            return j, txt, time.time() - t0
        errs.append("返回内容不是 JSON：" + txt[:120])
    hint = "。模型迟迟不返回，多半是显存不够、模型被挪到了 CPU 上：在命令行运行 ollama ps 看 PROCESSOR 一列，不是 100% GPU 就关掉其他占显卡的程序，或在设置里把上下文长度调小" if any("秒没有返回" in e for e in errs) else ""
    raise RuntimeError("Ollama 调用失败：" + " | ".join(dict.fromkeys(errs)) + hint)


def unload(model):
    """让 Ollama 立刻把模型移出显存，给 SAM 腾地方（外部接口不占本机显存，什么都不做）"""
    if backend() == "api":
        return
    try:
        _req("/api/generate", {"model": model, "keep_alive": 0}, timeout=20)
    except Exception:
        pass


# ============================================================ 外部接口（OpenAI 兼容） ====
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 阿里云百炼（北京地域）


def _api_post(path, body, timeout):
    base = (get_setting("api_base") or DASHSCOPE_BASE).rstrip("/")
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + (get_setting("api_key") or "")})
    host = urllib.parse.urlparse(base).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):   # 本机服务（如 Mage-VL）：不走系统代理
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:   # 外部地址：按系统代理设置走
        return json.loads(r.read().decode())


def api_ask(model, prompt, images, timeout=180, max_side=None):
    """OpenAI 兼容的 /chat/completions。图片用 base64 data URL 放进 content。
    关掉思考（enable_thinking=false，百炼的 Qwen 混合思考模型认这个参数），要求返回 JSON 对象。
    参数不被支持时去掉再试；超时时降到只发一张小图。"""
    tries = [{"json": True}, {"json": False}, {"json": False, "one": True}]
    errs, t0, i = [], time.time(), 0
    drop_think = False
    while i < len(tries):
        tr = tries[i]
        i += 1
        imgs = images[len(images) // 2:len(images) // 2 + 1] if tr.get("one") else images
        ms = 448 if tr.get("one") else (max_side or 768)
        content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(im, ms)}} for im in imgs]
        content.append({"type": "text", "text": prompt})
        body = {"model": model, "temperature": 0.2, "messages": [{"role": "user", "content": content}]}
        if not drop_think:
            body["enable_thinking"] = False
        if tr.get("json"):
            body["response_format"] = {"type": "json_object"}
        try:
            res = _api_post("/chat/completions", body, timeout if not tr.get("one") else max(60, timeout // 2))
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode(errors="ignore")
                msg = (json.loads(raw).get("error") or {}).get("message", raw[:200])
            except Exception:
                msg = ""
            if e.code in (401, 403):
                raise RuntimeError(f"外部接口拒绝访问（HTTP {e.code}）：API Key 不对或没有这个模型的权限。{msg}")
            if e.code == 404:
                raise RuntimeError(f"外部接口找不到（HTTP 404）：检查接口地址和模型名。{msg}")
            if e.code == 429:  # 限流：等一会儿重试同一档，最多 3 次
                n429 = sum(1 for x in errs if "429" in x)
                errs.append("请求太频繁或额度不足（HTTP 429）")
                if n429 < 3:
                    time.sleep(5 * (n429 + 1))
                    i -= 1
                continue
            if "thinking" in msg.lower() and not drop_think:
                drop_think = True
                i -= 1
            errs.append(f"HTTP {e.code} {msg}".strip())
            continue
        except Exception as e:
            if _is_timeout(e):
                errs.append(f"超过 {timeout} 秒没有返回")
                if not tr.get("one"):
                    i = len(tries) - 1
                    continue
                break
            errs.append(str(e))
            continue
        try:
            txt = res["choices"][0]["message"].get("content") or ""
        except Exception:
            txt = ""
        if isinstance(txt, list):  # 个别实现把 content 拆成片段
            txt = "".join(x.get("text", "") for x in txt if isinstance(x, dict))
        txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)
        j = parse_json(txt, strict=True)
        if j is not None:
            return j, txt, time.time() - t0
        errs.append("返回内容不是 JSON：" + txt[:120])
    raise RuntimeError("外部接口调用失败：" + " | ".join(dict.fromkeys(errs)))




def parse_json(txt, strict=False):
    """解析模型输出里的 JSON 对象。strict=True 时解析失败返回 None（空对象 {} 也算解析成功）"""
    try:
        j = json.loads(txt)
        if isinstance(j, dict):
            return j
    except Exception:
        m = re.search(r"\{.*\}", txt or "", re.S)
        if m:
            try:
                j = json.loads(m.group(0))
                if isinstance(j, dict):
                    return j
            except Exception:
                pass
    return None if strict else {}


SEG_PROMPT = """你在看一段操作视频中的一个片段（第 {t0:.1f}–{t1:.1f} 秒），按时间顺序给了 {n} 帧画面，每帧左下角标了时刻。
{ctx}只描述这个片段里双手（或机械手）在做的那一个动作。
只输出 JSON，不要其他文字：
{{"action": "动词短语，例如 拿起碗", "object": "被操作的物体", "success": true 或 false 或 null（看不出来）, "confidence": 0 到 1 的数}}"""

TASK_PROMPT = """这是一段操作视频按时间顺序抽出的 {n} 帧。
用一句不超过 15 个字的中文短语概括整段视频在完成的任务。
只输出 JSON：{{"task": "任务名", "confidence": 0 到 1 的数}}"""

VERIFY_PROMPT = """这是一段操作视频按时间顺序抽出的 {n} 帧。数据里登记的任务名是：“{name}”。
判断画面内容和这个任务名是否一致，并给出你看到的任务。
只输出 JSON：{{"consistent": true 或 false, "task_seen": "你看到的任务（中文短语）", "confidence": 0 到 1 的数}}"""
