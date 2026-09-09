"""Minimal Prometheus-text metrics.

Two sources are combined at scrape time:

* in-process counters and duration sums, which cover what this process did;
* aggregates read from PostgreSQL, which cover the whole system, so scraping
  the API also reports what the worker has been doing.

A metrics client library would add a dependency without changing the output.
What it would add — histograms, exemplars, multiprocess collection — is only
worth having once there is a real scrape target, which is noted in
docs/production-gap.md.
"""

from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_durations: dict[str, tuple[int, float]] = {}


def increment(name: str, value: float = 1.0, **labels: str) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counters[key] += value


def observe_duration(name: str, seconds: float) -> None:
    with _lock:
        count, total = _durations.get(name, (0, 0.0))
        _durations[name] = (count + 1, total + seconds)


def snapshot() -> tuple[
    dict[tuple[str, tuple[tuple[str, str], ...]], float], dict[str, tuple[int, float]]
]:
    with _lock:
        return dict(_counters), dict(_durations)


def reset() -> None:
    with _lock:
        _counters.clear()
        _durations.clear()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(extra_gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] | None = None) -> str:
    counters, durations = snapshot()
    lines: list[str] = []

    by_name: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = defaultdict(list)
    for (name, labels), value in counters.items():
        by_name[name].append((labels, value))

    for name in sorted(by_name):
        lines.append(f"# TYPE {name} counter")
        for labels, value in sorted(by_name[name]):
            lines.append(f"{name}{_labels(labels)} {value:g}")

    for name in sorted(durations):
        count, total = durations[name]
        lines.append(f"# TYPE {name} summary")
        lines.append(f"{name}_count {count:g}")
        lines.append(f"{name}_sum {total:.6f}")

    for name, series in sorted((extra_gauges or {}).items()):
        lines.append(f"# TYPE {name} gauge")
        for labels, value in sorted(series.items()):
            lines.append(f"{name}{_labels(labels)} {value:g}")

    return "\n".join(lines) + "\n"


def _labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"
