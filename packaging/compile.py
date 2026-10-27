"""把核心模块编译成二进制扩展（Linux .so / Windows .pyd），发布包里不带这些模块的源码。

  python packaging/compile.py <源码目录> <输出目录>

不编译的：__init__.py、server.py（网页接口层，FastAPI 需要读取函数签名）、worker.py、manage.py、version.py（入口和版本号）。
许可证检查同时放在编译后的 license 和 pipeline 里，改动 server.py 绕不过去。
需要：Cython 3 + C 编译器（Linux：gcc；Windows：Visual Studio Build Tools 的“使用 C++ 的桌面开发”）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

KEEP_SOURCE = {"__init__.py", "server.py", "worker.py", "manage.py", "version.py"}


def main():
    src, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.c", "build"))
    mods = [p for p in (out / "app").glob("*.py") if p.name not in KEEP_SOURCE]
    work = out / "_cbuild"
    work.mkdir()
    setup = work / "setup.py"
    setup.write_text(
        "from setuptools import setup\n"
        "from setuptools.extension import Extension\n"
        "from Cython.Build import cythonize\n"
        f"mods = {[str(m) for m in mods]!r}\n"
        "exts = [Extension('app.' + __import__('pathlib').Path(m).stem, [m]) for m in mods]\n"
        "setup(ext_modules=cythonize(exts, compiler_directives={'language_level': '3', 'binding': True,\n"
        "      'embedsignature': False, 'always_allow_keywords': True}, quiet=True))\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(setup), "build_ext", "--inplace", "-j", "4"], cwd=out)
    if r.returncode:
        sys.exit("编译失败")
    for m in mods:  # 删掉源码和中间文件，只留扩展模块
        m.unlink()
        c = m.with_suffix(".c")
        if c.exists():
            c.unlink()
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(out / "build", ignore_errors=True)
    print("已编译：", ", ".join(sorted(m.stem for m in mods)))


if __name__ == "__main__":
    main()
