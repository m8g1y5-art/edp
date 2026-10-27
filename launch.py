"""程序入口（源码版和编译版都用它启动，编译后的模块不能用 python -m 直接运行）。

  python launch.py server     网页服务（单机模式下同时跑处理）
  python launch.py worker     处理 worker（服务器模式）
  python launch.py manage ... 运维命令，参数同 python -m app.manage
"""
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "server"
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    if cmd == "server":
        from app.server import main as run
    elif cmd == "worker":
        from app.worker import main as run
    elif cmd == "manage":
        from app.manage import main as run
    else:
        sys.exit("用法：python launch.py server|worker|manage ...")
    run()


if __name__ == "__main__":
    main()
