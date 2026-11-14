"""Bounded, thread-safe performance measurements (durations may overlap)."""
from __future__ import annotations

import resource
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class Performance:
    def __init__(self):
        self.started = time.monotonic()
        self.cpu_started = time.process_time()
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.child_cpu_started = children.ru_utime + children.ru_stime
        self.seconds = {}
        self.counts = {}
        self.latencies = []
        self.lock = threading.Lock()
        self.gauges = {}
        self.memory_sampled_at = float("-inf")

    def observe(self, name, value):
        with self.lock:
            self.gauges[name] = value
            peak = name + "_peak"
            self.gauges[peak] = max(value, self.gauges.get(peak, value))

    def sample_memory(self):
        now = time.monotonic()
        with self.lock:
            if now - self.memory_sampled_at < 1:
                return
            self.memory_sampled_at = now
        # Linux cgroups include tmpfs, native decoders and concurrent FFmpeg.
        for name, paths in {
            "container_memory_bytes": ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            "container_memory_limit_bytes": ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            "container_memory_kernel_peak_bytes": ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"),
        }.items():
            for path in paths:
                try:
                    value = int(Path(path).read_text().strip())
                except (OSError, ValueError):
                    continue
                self.observe(name, value)
                break

    def add(self, name, seconds=0.0, count=1):
        with self.lock:
            self.seconds[name] = self.seconds.get(name, 0.0) + seconds
            self.counts[name] = self.counts.get(name, 0) + count

    @contextmanager
    def measure(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            self.add(name, time.monotonic() - started)

    def request(self, seconds):
        self.add("model_requests", seconds)
        with self.lock:
            # A bounded rolling sample; totals include every request.
            if len(self.latencies) >= 2048:
                self.latencies.pop(0)
            self.latencies.append(seconds)

    def report(self, completed=0):
        self.sample_memory()
        with self.lock:
            elapsed = time.monotonic() - self.started
            cpu = time.process_time() - self.cpu_started
            children = resource.getrusage(resource.RUSAGE_CHILDREN)
            child_cpu = children.ru_utime + children.ru_stime - self.child_cpu_started
            values = sorted(self.latencies)
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            utilization = {}
            for name, capacity in self.seconds.items():
                if name.endswith(".available_slot_seconds"):
                    phase = name.removesuffix(".available_slot_seconds")
                    utilization[phase] = self.seconds.get(phase + ".occupied_slot_seconds", 0) / max(capacity, .001)
            return {
                "elapsed_seconds": elapsed, "cpu_seconds": cpu,
                "child_cpu_seconds": child_cpu,
                "average_cpu_cores": (cpu + child_cpu) / max(elapsed, .001),
                "peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
                "stage_seconds": dict(self.seconds), "counts": dict(self.counts),
                "gauges": dict(self.gauges),
                "request_slot_utilization": utilization,
                "windows_per_second": completed / max(elapsed, .001),
                "request_latency_p50_seconds": values[len(values) // 2] if values else None,
                "request_latency_p95_seconds": values[min(len(values)-1, int(len(values)*.95))] if values else None,
                "latency_sample_size": len(values),
            }
