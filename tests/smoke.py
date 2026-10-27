"""冒烟测试：在临时数据目录里起服务器模式的网页服务和 worker，检查登录、权限、许可证、数据库升级、任务领取。

  python tests/smoke.py                       # 用源码
  python tests/smoke.py --root <发布目录>      # 用编译后的发布目录
需要一个开发用许可证：python tools/license_tool.py issue --key <私钥> --customer 测试 --days 7 --out tests/dev.lic
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
ap.add_argument("--lic", default=str(Path(__file__).resolve().parent / "dev.lic"))
ap.add_argument("--port", type=int, default=8799)
a = ap.parse_args()
root = Path(a.root)
data = Path(tempfile.mkdtemp(prefix="edp_smoke_"))
env = dict(os.environ, EDP_DATA=str(data), EDP_MODE="server", EDP_PORT=str(a.port), EDP_WORKER_ID="smoke")
B = f"http://127.0.0.1:{a.port}"
ok = True


def check(name, cond, extra=""):
    global ok
    print(("  通过  " if cond else "  失败  ") + name + (f"  {extra}" if extra and not cond else ""))
    ok = ok and bool(cond)


def run(*args):
    return subprocess.run([sys.executable, "launch.py", *args], cwd=root, env=env, capture_output=True, text=True)


def client():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(op, method, path, body=None):
    req = urllib.request.Request(B + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with op.open(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


print(f"数据目录：{data}")
r = run("manage", "create-admin", "admin", "--password", "admin12345")
check("建管理员 + 数据库升级", r.returncode == 0, r.stderr[-300:])
r = run("manage", "db", "status")
check("数据库版本记录", "数据库版本：" in r.stdout, r.stdout + r.stderr)
web = subprocess.Popen([sys.executable, "launch.py", "server"], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
wk = subprocess.Popen([sys.executable, "launch.py", "worker"], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(B + "/api/me", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    anon = client()
    check("未登录访问被拒", call(anon, "GET", "/api/batches")[0] == 401)
    check("错误密码被拒", call(anon, "POST", "/api/login", {"username": "admin", "password": "x"})[0] == 401)
    adm = client()
    check("管理员登录", call(adm, "POST", "/api/login", {"username": "admin", "password": "admin12345"})[0] == 200)
    check("新建标注员", call(adm, "POST", "/api/users", {"username": "ann", "password": "ann12345678", "role": "annotator"})[0] == 200)
    ann = client()
    call(ann, "POST", "/api/login", {"username": "ann", "password": "ann12345678"})
    check("标注员不能改设置", call(ann, "PUT", "/api/settings", {"max_jobs": 1})[0] == 403)
    check("标注员能看项目", call(ann, "GET", "/api/projects")[0] == 200)
    call(adm, "PUT", "/api/settings", {"api_key": "sk-test-123456"})
    st = call(adm, "GET", "/api/settings")[1]
    check("API Key 不明文返回", st["api_key"].endswith("3456") and "sk-test" not in st["api_key"])
    check("没有许可证不能开始处理", call(adm, "GET", "/api/me")[1]["license"]["valid"] is False)
    if Path(a.lic).exists():
        shutil.copy(a.lic, data / "license.lic")
        time.sleep(0.1)
        check("许可证有效", call(adm, "GET", "/api/license")[1]["valid"])
    code, _ = call(adm, "GET", "/api/media/x/y/..%2F..%2Fapp.db")
    check("路径穿越被拒", code in (400, 404))
    time.sleep(6)
    ws = call(adm, "GET", "/api/workers")[1]
    check("worker 在线", any(w["alive"] for w in ws), json.dumps(ws)[:200])
    logs = call(adm, "GET", "/api/audit")[1]
    check("操作日志有记录", any(l["status"] == 403 for l in logs))
finally:
    web.terminate(); wk.terminate()
    web.wait(10); wk.wait(10)
shutil.rmtree(data, ignore_errors=True)
print("\n全部通过" if ok else "\n有失败项")
sys.exit(0 if ok else 1)
