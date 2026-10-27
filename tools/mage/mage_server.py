"""Mage-VL 本机语义服务：把 Mage-VL 包成 OpenAI 兼容接口，给具身数据平台调用。

启动：双击 3_双击启动语义服务.bat（用本文件夹的 venv 和 hf_cache，不联网）
地址：http://127.0.0.1:30000/v1   模型名：mage-vl   API Key：随便填（例如 local）

接口：
  GET  /v1/models
  POST /v1/chat/completions   messages 里的图片用 image_url（data:image/jpeg;base64,...）
       1 张图 → 按图片理解；2 张及以上 → 按时间顺序当成一小段视频（Mage-VL 的强项）
       response_format = json_object 时，在问题末尾追加“只输出 JSON”的要求
只用标准库起 HTTP 服务；显卡一次只跑一个请求（加锁排队）。
"""
import base64
import io
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(ROOT / "hf_cache"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
HOST, PORT = "127.0.0.1", int(os.environ.get("MAGE_PORT", "30000"))
MODEL_ID = os.environ.get("MAGE_MODEL", "microsoft/Mage-VL")
NAME = "mage-vl"
LOG = ROOT / "logs" / "mage_server.log"
LOG.parent.mkdir(exist_ok=True)

_lock = threading.Lock()
M = {}


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load():
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor
    t0 = time.time()
    log(f"加载模型 {MODEL_ID} ...")
    M["proc"] = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
    M["model"] = AutoModelForCausalLM.from_pretrained(
        MODEL_ID, trust_remote_code=True, torch_dtype="auto", device_map="auto").eval()
    M["torch"] = torch
    dev = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    log(f"模型就绪（{time.time() - t0:.0f} 秒，{dev}）")


def _img(url):
    from PIL import Image
    if not url.startswith("data:"):
        raise ValueError("只支持 base64 data URL 图片")
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")


def _parse(messages):
    """把 OpenAI 格式的 messages 压成：图片列表 + 一段文字（system 和各轮 user 文字按顺序拼起来）"""
    imgs, texts = [], []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            texts.append(c)
            continue
        for part in c or []:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                u = part.get("image_url")
                imgs.append(_img(u["url"] if isinstance(u, dict) else u))
    return imgs, "\n".join(t for t in texts if t)


def _inputs(kind, imgs, text):
    proc = M["proc"]
    content = ([{"type": kind}] if kind else []) + [{"type": "text", "text": text}]
    prompt = proc.apply_chat_template([{"role": "user", "content": content}], tokenize=False, add_generation_prompt=True)
    if kind == "image":
        return proc(text=[prompt], images=imgs, return_tensors="pt")
    if kind == "video":
        return proc(text=[prompt], videos=[imgs], return_tensors="pt", padding=True)
    return proc(text=[prompt], return_tensors="pt")


def generate(imgs, text, max_new):
    torch, model, proc = M["torch"], M["model"], M["proc"]
    # 多张图按视频送；处理器不接受时退回只看中间那一张
    plans = [("image", imgs[:1])] if len(imgs) == 1 else [("video", imgs), ("image", imgs[len(imgs) // 2:len(imgs) // 2 + 1])] if imgs else [(None, [])]
    err = None
    for kind, ims in plans:
        try:
            inputs = _inputs(kind, ims, text)
            break
        except Exception as e:
            err = e
            log(f"按 {kind} 处理失败，换一种方式：{e}")
    else:
        raise err
    inputs = {k: (v.to(model.device) if hasattr(v, "to") else v) for k, v in inputs.items()}
    if "pixel_values" in inputs:
        inputs["pixel_values"] = inputs["pixel_values"].to(model.dtype)
    n_in = int(inputs["input_ids"].shape[1])
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max_new, do_sample=False)
    ans = proc.tokenizer.decode(out[0, n_in:], skip_special_tokens=True).strip()
    return ans, n_in, int(out.shape[1]) - n_in, kind


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            return self._send(200, {"object": "list", "data": [{"id": NAME, "object": "model", "owned_by": "local"}]})
        if self.path in ("/", "/health"):
            return self._send(200, {"ok": "model" in M, "model": NAME})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            return self._send(404, {"error": {"message": "not found"}})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            imgs, text = _parse(body.get("messages") or [])
            if (body.get("response_format") or {}).get("type") == "json_object":
                text += "\n\n只输出一个 JSON 对象，不要输出任何其他文字，也不要用 ``` 包起来。"
            max_new = int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)
            t0 = time.time()
            with _lock:
                ans, n_in, n_out, kind = generate(imgs, text, max_new)
            log(f"{len(imgs)} 张图（按{'视频' if kind == 'video' else '图片' if kind == 'image' else '纯文字'}）"
                f" · 输入 {n_in} / 输出 {n_out} token · {time.time() - t0:.1f} 秒")
            self._send(200, {
                "id": f"chatcmpl-{int(time.time() * 1000)}", "object": "chat.completion", "created": int(time.time()),
                "model": NAME,
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": ans}}],
                "usage": {"prompt_tokens": n_in, "completion_tokens": n_out, "total_tokens": n_in + n_out}})
        except Exception as e:
            log("请求失败：" + traceback.format_exc())
            self._send(500, {"error": {"message": f"{type(e).__name__}: {e}"}})


def main():
    load()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    log(f"语义服务已启动：http://{HOST}:{PORT}/v1  模型名 {NAME}。关掉这个窗口就停止。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
