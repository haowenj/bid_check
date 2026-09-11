from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class LLMRequestLimiter:
    """Limit concurrent in-flight requests to an LLM endpoint."""

    def __init__(self, max_concurrency: int):
        if max_concurrency < 1:
            raise ValueError("LLM 请求并发数必须大于等于 1")
        self.max_concurrency = max_concurrency
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()


_limiter_lock = threading.Lock()
_global_limiter = LLMRequestLimiter(5)


def configure_llm_concurrency(max_concurrency: int) -> LLMRequestLimiter:
    """Configure the process-wide LLM request limiter for a running app."""

    global _global_limiter
    limiter = LLMRequestLimiter(max_concurrency)
    with _limiter_lock:
        _global_limiter = limiter
    return limiter


def get_llm_request_limiter() -> LLMRequestLimiter:
    with _limiter_lock:
        return _global_limiter


@contextmanager
def llm_request_slot() -> Iterator[None]:
    """Acquire the shared slot immediately around one LLM HTTP request."""

    with get_llm_request_limiter().slot():
        yield
