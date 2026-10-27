"""许可证签发工具（只在我方内部使用，不随安装包发给客户）。

  python tools/license_tool.py keygen  --out 私钥.pem          # 生成新密钥对（一次即可；换密钥后旧许可证全部失效）
  python tools/license_tool.py issue   --key 私钥.pem --customer "TCL" --days 365 --gpus 4 \
                                       [--machine 指纹] [--edition standard] [--modules base,fine,semantic,lerobot] \
                                       [--perpetual] --out TCL.lic
  python tools/license_tool.py verify  TCL.lic                 # 用程序内置公钥检查一个许可证
客户机器的指纹：在客户机器上运行 `python -m app.manage license fingerprint`，或在网页“设置 → 授权”里看。
"""
import argparse
import base64
import json
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def keygen(a):
    from cryptography.hazmat.primitives import serialization as s
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    Path(a.out).write_bytes(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    print(f"私钥已写入 {a.out}（妥善保管，不要放进代码仓库）")
    print(f"把下面这行替换到 app/license.py 的 PUBLIC_KEY：\nPUBLIC_KEY = \"{pub}\"")


def issue(a):
    from cryptography.hazmat.primitives import serialization as s
    k = s.load_pem_private_key(Path(a.key).read_bytes(), password=None)
    today = date.today()
    payload = {
        "lic_id": uuid.uuid4().hex[:12],
        "customer": a.customer,
        "edition": a.edition,
        "issued": today.isoformat(),
        "expires": None if a.perpetual else (today + timedelta(days=a.days)).isoformat(),
        "max_gpus": a.gpus,
        "machine": a.machine or None,
        "modules": [m.strip() for m in a.modules.split(",") if m.strip()],
    }
    sig = base64.b64encode(k.sign(canonical(payload))).decode()
    Path(a.out).write_text(json.dumps({"payload": payload, "sig": sig}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"已写入 {a.out}")


def verify(a):
    from app import license as L
    payload, err = L.load(a.file)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if payload else err)
    sys.exit(0 if payload else 1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("keygen"); g.add_argument("--out", required=True); g.set_defaults(fn=keygen)
    i = sub.add_parser("issue")
    i.add_argument("--key", required=True); i.add_argument("--customer", required=True)
    i.add_argument("--days", type=int, default=365); i.add_argument("--perpetual", action="store_true")
    i.add_argument("--gpus", type=int, default=0); i.add_argument("--machine", default="")
    i.add_argument("--edition", default="standard", choices=["trial", "standard"])
    i.add_argument("--modules", default="base,fine,semantic,lerobot"); i.add_argument("--out", required=True)
    i.set_defaults(fn=issue)
    v = sub.add_parser("verify"); v.add_argument("file"); v.set_defaults(fn=verify)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
