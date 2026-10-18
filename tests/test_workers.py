import time

import pytest

from enclave.workers import Timeout, WorkerPool
from tests.slow_job import stall_on_slow


def test_an_overrunning_job_is_killed_and_its_worker_replaced() -> None:
    pool = WorkerPool(workers=1, deadline=0.5, job=stall_on_slow)
    try:
        started = time.monotonic()
        with pytest.raises(Timeout):
            pool("slow")
        assert time.monotonic() - started < 2
        assert pool("Signed by Foo Inc.\n")["parties"] == ["Signed by Foo Inc"]
    finally:
        pool.close()
