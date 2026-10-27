"""处理 worker：服务器模式下单独运行的处理进程（网页服务只负责提交任务和显示进度）。

用法：  EDP_MODE=server python -m app.worker
多台服务器或多组显卡：每个 worker 设不同的 EDP_WORKER_ID，用 CUDA_VISIBLE_DEVICES 分配显卡，连同一个数据目录。
任务状态都在数据库里，worker 重启后会把自己没跑完的步骤重新排队；别的 worker 停了超过 90 秒，它的步骤也会被接手。
"""
import signal
import sys
import time

from . import pipeline
from .db import migrate


def main():
    migrate()
    print(f"[worker {pipeline.WORKER_ID}] 启动", flush=True)
    pipeline.start_worker()
    stop = {"v": False}

    def _bye(*_):
        stop["v"] = True

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    while not stop["v"]:
        time.sleep(1)
    # 收到停止信号：不再派新活，把手上的步骤放回队列（下次启动或其他 worker 会接着跑）
    pipeline.STATE["stop"] = True
    pipeline._requeue_orphans(pipeline.WORKER_ID)
    from .db import x
    x("DELETE FROM workers WHERE id=?", (pipeline.WORKER_ID,))
    print(f"[worker {pipeline.WORKER_ID}] 已停止，未完成的步骤已放回队列", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
