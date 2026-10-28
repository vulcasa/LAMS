"""独立 worker 进程（生产推荐）。

    python -m scripts.worker

应用内默认也会起一个守护线程；如果你用 gunicorn 多 worker，建议关掉内置线程
（AVLIB_WORKER=0），单独跑这个进程，任务队列是同一张表，原子领取不会重复。
"""
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.services import jobs  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
)
log = logging.getLogger("worker")


def main():
    app = create_app({"WORKER_ENABLED": False})
    poll = float(app.config.get("WORKER_POLL_SECONDS", 1.5))
    log.info("worker 启动，poll=%.1fs", poll)
    while True:
        try:
            with app.app_context():
                job = jobs._claim_next()
                if job is not None:
                    log.info("执行 job #%s kind=%s", job.id, job.kind)
                    jobs.run_job(job, app)
                    log.info("完成 job #%s", job.id)
                    continue
        except KeyboardInterrupt:
            log.info("收到中断，退出")
            return 0
        except Exception:  # noqa: BLE001
            log.exception("worker 循环异常")
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
