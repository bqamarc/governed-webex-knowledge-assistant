from __future__ import annotations

import logging
import signal
import threading
import time

from .runtime import Runtime
from .webex import PermanentWebexAPIError, RetryableWebexAPIError

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.stop_event = threading.Event()

    def stop(self, *_args: object) -> None:
        self.stop_event.set()

    def run_once(self) -> bool:
        lease = self.runtime.queue.lease_next()
        if lease is None:
            return False
        try:
            self.runtime.bot_service().process(lease.payload)
        except RetryableWebexAPIError as exc:
            state = self.runtime.queue.fail(
                lease.id,
                exc,
                retry_after_seconds=exc.retry_after_seconds,
            )
            logger.warning("job_failed state=%s code=%s", state, exc.code)
        except PermanentWebexAPIError as exc:
            state = self.runtime.queue.fail(lease.id, exc, terminal=True)
            logger.warning("job_failed state=%s code=%s", state, exc.code)
        except Exception as exc:
            state = self.runtime.queue.fail(lease.id, exc)
            logger.warning("job_failed state=%s type=%s", state, type(exc).__name__)
        else:
            self.runtime.queue.complete(lease.id)
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self.stop_event.is_set():
            if not self.run_once():
                self.stop_event.wait(self.runtime.settings.worker_poll_seconds)


def wait_for_worker(worker: Worker, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not worker.stop_event.is_set():
        time.sleep(0.01)
