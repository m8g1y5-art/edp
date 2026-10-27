"""环境自检与修复：找出 site-packages 里被写坏（含空字节）的 .py 文件，重装它们所属的包。
用法：python -m app.repair"""
import subprocess
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

MIRROR = ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"]


def broken_files():
    site = Path(sysconfig.get_paths()["purelib"])
    bad = []
    for p in site.rglob("*.py"):
        try:
            if b"\x00" in p.read_bytes():
                bad.append(p)
        except OSError:
            bad.append(p)
    return site, bad


def owners(site, files):
    want = {f.resolve() for f in files}
    out = set()
    for dist in metadata.distributions():
        try:
            name, ver = dist.metadata["Name"], dist.version
        except Exception:
            name, ver = None, None
        if not name or not ver:  # 连包的说明文件都坏了
            out.add(None)
            continue
        for f in dist.files or []:
            try:
                if (site / f).resolve() in want:
                    out.add(f"{name}=={ver}")
                    break
            except Exception:
                pass
    return out


def main():
    site, bad = broken_files()
    print(f"检查 {site}")
    if not bad:
        print("没有发现损坏的文件")
        return 0
    print(f"发现 {len(bad)} 个损坏的文件，例如：")
    for p in bad[:5]:
        print("   ", p.relative_to(site))
    own = owners(site, bad)
    if len(bad) > 20 or None in own:
        print("损坏范围较大（可能是安装时被打断，或文件被安全软件拦截），整个运行环境需要重建。")
        return 2
    pk = sorted(own)
    print("需要重装：", ", ".join(pk) or "（找不到所属的包）")
    if pk:
        cmd = [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", "--no-cache-dir", *pk, *MIRROR]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            subprocess.run(cmd[:-2])
    _, bad2 = broken_files()
    print("修复后仍损坏：", len(bad2))
    return 1 if bad2 else 0


if __name__ == "__main__":
    sys.exit(main())
