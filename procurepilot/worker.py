"""有界串行 Worker。可内嵌开发服务或以 python -m procurepilot.worker 独立运行。

每个进程最多一个活跃任务，多进程通过数据库租约协调。停机时不再领取新任务；
进程被强制终止的未决调用由下一次扫描标记为不确定，不能假装安全重试。
"""

import logging
import time
from .settings import redis_url
import signal
from threading import Event
from .jobs import run_once
from .storage import Store

log = logging.getLogger(__name__)


def serve(store, stop, model=None):
    last_redis_warning = -60.0
    queue = None
    if redis_url():
        from .streams import StreamQueue

        queue = StreamQueue(store)
    while not stop.is_set():
        try:
            worked = False
            if queue:
                try:
                    queue.publish()
                    worked = queue.consume_once(model)
                except Exception as exc:
                    if time.monotonic() - last_redis_warning >= 60:
                        log.warning(
                            "Redis dispatch unavailable: %s", type(exc).__name__
                        )
                        last_redis_warning = time.monotonic()
            # 有限补偿扫描：Redis故障、保留期删除或投递中断不会丢失SQL任务。
            worked = run_once(store, model) or worked
            if not worked:
                from .indexing import run_once as index_once

                worked = index_once(store)
        except Exception as exc:
            log.warning("Worker iteration failed: %s", type(exc).__name__)
            worked = False
        if not worked:
            stop.wait(1)


def main():
    from .observability import configure, flush

    configure()
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    store = Store()
    try:
        serve(store, stop)
    finally:
        store.engine.dispose()
        flush()


if __name__ == "__main__":
    main()
