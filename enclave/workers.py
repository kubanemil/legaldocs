"""Extraction runs in processes because a thread cannot be killed. A job past its deadline
has its worker killed and replaced."""

import multiprocessing
import queue
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from enclave.extractor import extract_fields

type Job = Callable[[str], dict[str, Any]]

_SPAWN = multiprocessing.get_context("spawn")


class Timeout(Exception):
    pass


class Busy(Exception):
    pass


class WorkerPool:
    def __init__(self, workers: int, deadline: float, job: Job = extract_fields) -> None:
        self._deadline = deadline
        self._job = job
        self._idle: queue.SimpleQueue[_Worker] = queue.SimpleQueue()
        for _ in range(workers):
            self._idle.put(_Worker(job))

    def __call__(self, text: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._deadline
        try:
            worker = self._idle.get(timeout=self._deadline)
        except queue.Empty:
            raise Busy from None
        try:
            return worker.run(text, deadline - time.monotonic())
        except (Timeout, EOFError, OSError):
            worker.kill()
            worker = _Worker(self._job)
            raise
        finally:
            self._idle.put(worker)

    def close(self) -> None:
        while True:
            try:
                self._idle.get_nowait().kill()
            except queue.Empty:
                return


class _Worker:
    def __init__(self, job: Job) -> None:
        self._connection, child = _SPAWN.Pipe()
        self._process = _SPAWN.Process(target=_serve, args=(child, job), daemon=True)
        self._process.start()
        child.close()

    def run(self, text: str, timeout: float) -> dict[str, Any]:
        self._connection.send(text)
        if not self._connection.poll(max(timeout, 0)):
            raise Timeout
        result: dict[str, Any] = self._connection.recv()
        return result

    def kill(self) -> None:
        self._process.kill()
        self._process.join()
        self._connection.close()


def _serve(connection: Connection, job: Job) -> None:
    while True:
        try:
            text = connection.recv()
        except EOFError:
            return
        connection.send(job(text))
