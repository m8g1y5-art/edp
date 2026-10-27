"""许可证：离线验签，不需要联网。

许可证文件（.lic）是一段 JSON：{"payload": {...}, "sig": "<base64>"}。
payload 由我方私钥签名，程序里只有公钥，客户改动任何一个字段签名就对不上。

payload 字段：
  lic_id     许可证编号
  customer   客户名称
  edition    trial（试用）/ standard（正式）
  issued     签发日期 YYYY-MM-DD
  expires    到期日期 YYYY-MM-DD；null = 永久（买断）
  max_gpus   最多可用几张显卡；0 = 不限
  machine    绑定的机器指纹；null = 不绑定
  modules    启用的功能模块，如 ["base", "fine", "lerobot"]

到期或无效时：不能开始新的处理；浏览、校对、导出已有结果都不受影响。
放置位置（按顺序查找）：数据目录/license.lic（网页上传的就存这里）→ 环境变量 EDP_LICENSE 指定的文件 → 程序目录/license.lic
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from .db import DATA, ROOT, SERVER, get_setting, set_setting

PUBLIC_KEY = "LpIH+dvZDvy8PSPg/oNrBmcw0bNEyufGtQs24u97DN8="
WARN_DAYS = 30
MODULES = {"base": "基础处理（入库、同步、质检、切点、导出）", "fine": "精细标注（物体跟踪、接触事件）",
           "semantic": "语义标注（视觉大模型）", "lerobot": "LeRobot 导出"}

_cache = {"t": 0, "v": None}


def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_sig(payload, sig_b64):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pk = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY))
    try:
        pk.verify(base64.b64decode(sig_b64), canonical(payload))
        return True
    except (InvalidSignature, ValueError):
        return False


def path():
    cands = [DATA / "license.lic", os.environ.get("EDP_LICENSE"), ROOT / "license.lic"]
    for p in cands:
        if p and Path(p).is_file():
            return Path(p)
    return None


def fingerprint():
    """这台机器的指纹（签发绑定机器的许可证时用）。Docker 里需要把宿主机的 /etc/machine-id 只读挂进容器"""
    raw = ""
    try:
        if sys.platform == "win32":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                               winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            raw = winreg.QueryValueEx(k, "MachineGuid")[0]
        else:
            for f in (os.environ.get("EDP_MACHINE_ID_FILE"), "/etc/machine-id", "/var/lib/dbus/machine-id"):
                if f and Path(f).is_file():
                    raw = Path(f).read_text().strip()
                    break
    except Exception:
        pass
    if not raw:
        return None
    return hashlib.sha256(("edp:" + raw).encode()).hexdigest()[:20]


def gpu_count():
    """这套部署能用到的显卡数：服务器模式按各 worker 登记的显卡合计，单机按本机"""
    if SERVER:
        try:
            from . import pipeline
            return sum(len(w["info"].get("gpus", [])) for w in pipeline.workers() if w["alive"])
        except Exception:
            return 0
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        return len([l for l in r.stdout.splitlines() if l.startswith("GPU ")]) if r.returncode == 0 else 0
    except Exception:
        return 0


def _clock_ok():
    """防止把系统时间往回调来绕过到期：记住见过的最晚日期，当前时间比它早一天以上就判无效"""
    now = time.time()
    seen = get_setting("license_seen") or 0
    if now + 86400 < seen:
        return False
    if now > seen:
        set_setting("license_seen", now)
    return True


def load(p=None):
    """读取并校验许可证文件，返回 (payload 或 None, 问题说明)"""
    p = Path(p) if p else path()
    if not p:
        return None, "没有找到许可证文件"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        payload, sig = d["payload"], d["sig"]
    except Exception:
        return None, "许可证文件格式不对"
    if not _verify_sig(payload, sig):
        return None, "许可证签名无效（文件被改动或不是我方签发）"
    return payload, ""


def status(force=False):
    if not force and _cache["v"] and time.time() - _cache["t"] < 60:
        return _cache["v"]
    payload, err = load()
    st = {"valid": False, "reason": err, "info": payload, "days_left": None, "warn": False,
          "fingerprint": fingerprint(), "path": str(path()) if path() else None}
    if payload:
        today = date.today()
        exp = payload.get("expires")
        if exp:
            left = (date.fromisoformat(exp) - today).days
            st["days_left"] = left
            st["warn"] = 0 <= left < WARN_DAYS
        if exp and st["days_left"] < 0:
            st["reason"] = f"许可证已于 {exp} 到期"
        elif payload.get("machine") and payload["machine"] != st["fingerprint"]:
            st["reason"] = "许可证绑定的是另一台机器"
        elif payload.get("max_gpus") and gpu_count() > payload["max_gpus"]:
            st["reason"] = f"显卡数量（{gpu_count()} 张）超过许可证允许的 {payload['max_gpus']} 张"
        elif not _clock_ok():
            st["reason"] = "系统时间比上次运行时早，请校正系统时间"
        else:
            st["valid"], st["reason"] = True, ""
    _cache.update(t=time.time(), v=st)
    return st


def can_process():
    return status()["valid"]


def has_module(m):
    st = status()
    return st["valid"] and m in (st["info"].get("modules") or [])


def install(text):
    """保存上传的许可证（先校验，不合格不覆盖旧的）"""
    try:
        d = json.loads(text)
        ok = _verify_sig(d["payload"], d["sig"])
    except Exception:
        ok = False
    if not ok:
        raise ValueError("许可证签名无效")
    dst = DATA / "license.lic"
    dst.write_text(text, encoding="utf-8")
    _cache["v"] = None
    return status(force=True)
