from __future__ import annotations

import platform
import resource
import time
from contextlib import contextmanager


def peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system().lower() == "darwin":
        return float(value) / (1024 * 1024)
    return float(value) / 1024


@contextmanager
def measured() -> dict[str, float]:
    result = {"started_rss_mb": peak_rss_mb()}
    started = time.perf_counter()
    try:
        yield result
    finally:
        result["latency_seconds"] = time.perf_counter() - started
        result["peak_rss_mb"] = peak_rss_mb()
        result["rss_growth_mb"] = max(
            0.0, result["peak_rss_mb"] - result["started_rss_mb"]
        )
