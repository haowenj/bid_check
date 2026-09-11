from __future__ import annotations

import threading
import time

from app.llm_concurrency import LLMRequestLimiter


def test_llm_request_limiter_caps_inflight_requests():
    limiter = LLMRequestLimiter(2)
    active = 0
    max_active = 0
    state_lock = threading.Lock()
    barrier = threading.Barrier(5)

    def request() -> None:
        nonlocal active, max_active
        barrier.wait()
        with limiter.slot():
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1

    threads = [threading.Thread(target=request) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_active == 2
