from __future__ import annotations

import math
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable


MEBIBYTE = 1024 * 1024
GIBIBYTE = 1024 * MEBIBYTE


@dataclass(frozen=True, slots=True)
class NvidiaTelemetry:
    utilization_percent: float
    memory_used_bytes: int
    memory_total_bytes: int


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    observed_at: float
    cpu_percent: float | None = None
    available_memory_bytes: int | None = None
    memory_percent: float | None = None
    backlog: int = 0
    active_workers: int = 0
    http_error_rate: float = 0.0
    timeout_rate: float = 0.0
    rate_limit_count: int = 0
    llm_latency_seconds: float | None = None
    llm_failures: int = 0
    gpu_utilization_percent: float | None = None
    gpu_memory_used_bytes: int | None = None
    gpu_memory_total_bytes: int | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.observed_at):
            raise ValueError("observed_at must be finite")
        for name in ("cpu_percent", "memory_percent", "gpu_utilization_percent"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0 or value > 100):
                raise ValueError(f"{name} must be between 0 and 100 when supplied")
        for name in ("http_error_rate", "timeout_rate"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or value > 1:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in (
            "available_memory_bytes",
            "backlog",
            "active_workers",
            "rate_limit_count",
            "llm_failures",
            "gpu_memory_used_bytes",
            "gpu_memory_total_bytes",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.llm_latency_seconds is not None and (
            not math.isfinite(self.llm_latency_seconds) or self.llm_latency_seconds < 0
        ):
            raise ValueError("llm_latency_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class ResourceAdjustment:
    action: str
    previous_workers: int
    target_workers: int
    previous_delay_seconds: float
    delay_seconds: float
    reason: str
    observed_at: float

    @property
    def workers_changed(self) -> bool:
        return self.previous_workers != self.target_workers

    @property
    def delay_changed(self) -> bool:
        return not math.isclose(self.previous_delay_seconds, self.delay_seconds)

    @property
    def new_workers(self) -> int:
        return self.target_workers

    @property
    def new_delay_seconds(self) -> float:
        return self.delay_seconds

    @property
    def changed(self) -> bool:
        return self.workers_changed or self.delay_changed


class ThreadSafeDelayController:
    """A bounded mutable request delay; fixed searches need not use it."""

    def __init__(
        self,
        delay_seconds: float = 0.0,
        *,
        min_delay_seconds: float = 0.0,
        max_delay_seconds: float = 120.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        minimum = float(min_delay_seconds)
        maximum = float(max_delay_seconds)
        initial = float(delay_seconds)
        if not all(math.isfinite(value) for value in (minimum, maximum, initial)):
            raise ValueError("delay bounds and initial delay must be finite")
        minimum = max(0.0, minimum)
        maximum = max(minimum, maximum)
        self._minimum = minimum
        self._maximum = maximum
        self._delay = min(max(initial, minimum), maximum)
        self._sleeper = sleeper
        self._lock = threading.RLock()

    @property
    def min_delay_seconds(self) -> float:
        return self._minimum

    @property
    def max_delay_seconds(self) -> float:
        return self._maximum

    @property
    def delay_seconds(self) -> float:
        return self.get_delay()

    def get_delay(self) -> float:
        with self._lock:
            return self._delay

    def set_delay(self, delay_seconds: float) -> float:
        value = float(delay_seconds)
        if not math.isfinite(value):
            raise ValueError("delay_seconds must be finite")
        with self._lock:
            self._delay = min(max(value, self._minimum), self._maximum)
            return self._delay

    def increase_to(self, minimum_delay_seconds: float) -> float:
        value = float(minimum_delay_seconds)
        if not math.isfinite(value):
            raise ValueError("minimum_delay_seconds must be finite")
        with self._lock:
            self._delay = min(max(self._delay, value, self._minimum), self._maximum)
            return self._delay

    def wait(self, cancel_event: threading.Event | None = None) -> bool:
        """Wait the current delay and return false only when cancellation wins."""

        delay = self.get_delay()
        if delay <= 0:
            return cancel_event is None or not cancel_event.is_set()
        if cancel_event is not None:
            return not cancel_event.wait(delay)
        self._sleeper(delay)
        return True


EventCallback = Callable[[ResourceAdjustment], None]


class AdaptiveResourceController:
    """Deterministic bounded worker/delay adaptation with hysteresis.

    It never owns or terminates executor work.  A bounded dispatcher reads
    ``target_workers`` and simply refrains from submitting replacements while the
    active count is above that target.
    """

    def __init__(
        self,
        *,
        min_workers: int,
        max_workers: int,
        initial_workers: int | None = None,
        delay_controller: ThreadSafeDelayController | None = None,
        high_cpu_percent: float = 85.0,
        healthy_cpu_percent: float = 55.0,
        minimum_available_memory_bytes: int = 768 * MEBIBYTE,
        high_memory_percent: float = 90.0,
        high_error_rate: float = 0.20,
        high_timeout_rate: float = 0.12,
        pressure_samples: int = 2,
        healthy_samples: int = 3,
        cooldown_seconds: float = 15.0,
        healthy_backlog: int = 3,
        event_callback: EventCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        minimum = int(min_workers)
        maximum = int(max_workers)
        if minimum < 1 or maximum < minimum:
            raise ValueError("worker bounds must satisfy 1 <= min_workers <= max_workers")
        target = maximum if initial_workers is None else int(initial_workers)
        if target < minimum or target > maximum:
            raise ValueError("initial_workers must be within worker bounds")
        if not 0 <= healthy_cpu_percent < high_cpu_percent <= 100:
            raise ValueError("CPU thresholds must be ordered within 0..100")
        if not 0 <= high_memory_percent <= 100:
            raise ValueError("high_memory_percent must be within 0..100")
        if not 0 <= high_error_rate <= 1 or not 0 <= high_timeout_rate <= 1:
            raise ValueError("error-rate thresholds must be within 0..1")

        self.min_workers = minimum
        self.max_workers = maximum
        self._target_workers = target
        self.delay_controller = delay_controller or ThreadSafeDelayController()
        self.high_cpu_percent = float(high_cpu_percent)
        self.healthy_cpu_percent = float(healthy_cpu_percent)
        self.minimum_available_memory_bytes = max(1, int(minimum_available_memory_bytes))
        self.high_memory_percent = float(high_memory_percent)
        self.high_error_rate = float(high_error_rate)
        self.high_timeout_rate = float(high_timeout_rate)
        self.pressure_samples = max(1, min(int(pressure_samples), 20))
        self.healthy_samples = max(1, min(int(healthy_samples), 50))
        cooldown = float(cooldown_seconds)
        if not math.isfinite(cooldown):
            raise ValueError("cooldown_seconds must be finite")
        self.cooldown_seconds = max(0.0, min(cooldown, 3600.0))
        self.healthy_backlog = max(1, int(healthy_backlog))
        self._event_callback = event_callback
        self._clock = clock
        self._pressure_count = 0
        self._healthy_count = 0
        # Snapshot event counters are cumulative for a controller/run. Tracking
        # deltas prevents one historical 429 or LLM failure from creating
        # perpetual pressure on every later observation.
        self._last_rate_limit_count = 0
        self._last_llm_failures = 0
        self._last_adjustment_at = float("-inf")
        self._lock = threading.RLock()

    @property
    def target_workers(self) -> int:
        with self._lock:
            return self._target_workers

    @property
    def current_workers(self) -> int:
        return self.target_workers

    @property
    def current_delay_seconds(self) -> float:
        return self.delay_controller.get_delay()

    @property
    def delay_seconds(self) -> float:
        return self.current_delay_seconds

    def get_target_workers(self, **_context: object) -> int:
        return self.target_workers

    def get_delay_seconds(self) -> float:
        return self.current_delay_seconds

    def before_request(self, *, url: str = "") -> bool:
        del url
        return self.delay_controller.wait()

    wait_before_request = before_request

    def observe(
        self,
        snapshot: ResourceSnapshot,
        *,
        now: float | None = None,
    ) -> ResourceAdjustment | None:
        timestamp = self._clock() if now is None else float(now)
        if not math.isfinite(timestamp):
            raise ValueError("now must be finite")
        callback: EventCallback | None = None
        adjustment: ResourceAdjustment | None = None
        with self._lock:
            new_rate_limits = self._counter_delta(
                snapshot.rate_limit_count, self._last_rate_limit_count
            )
            new_llm_failures = self._counter_delta(
                snapshot.llm_failures, self._last_llm_failures
            )
            self._last_rate_limit_count = snapshot.rate_limit_count
            self._last_llm_failures = snapshot.llm_failures
            pressure_reasons = self._pressure_reasons(
                snapshot,
                new_rate_limits=new_rate_limits,
                new_llm_failures=new_llm_failures,
            )
            healthy = self._is_healthy(
                snapshot,
                new_rate_limits=new_rate_limits,
                new_llm_failures=new_llm_failures,
            )
            if pressure_reasons:
                self._pressure_count += 1
                self._healthy_count = 0
            elif healthy:
                self._healthy_count += 1
                self._pressure_count = 0
            else:
                self._pressure_count = 0
                self._healthy_count = 0

            cooled_down = timestamp - self._last_adjustment_at >= self.cooldown_seconds
            if (
                pressure_reasons
                and self._pressure_count >= self.pressure_samples
                and cooled_down
            ):
                reason = ", ".join(pressure_reasons)
                adjustment = self._adjust_locked(
                    action="decrease",
                    worker_delta=-1,
                    proposed_delay=max(
                        self.delay_controller.get_delay() + 0.25,
                        self.delay_controller.get_delay() * 1.25,
                    ),
                    reason=f"sustained {reason}",
                    timestamp=timestamp,
                )
                self._pressure_count = 0
                self._healthy_count = 0
            elif (
                healthy
                and self._healthy_count >= self.healthy_samples
                and cooled_down
            ):
                adjustment = self._adjust_locked(
                    action="increase",
                    worker_delta=1,
                    proposed_delay=self.delay_controller.get_delay() * 0.90,
                    reason="sustained healthy utilization with pending backlog",
                    timestamp=timestamp,
                )
                self._pressure_count = 0
                self._healthy_count = 0
            if adjustment is not None:
                callback = self._event_callback
        self._emit(callback, adjustment)
        return adjustment

    def record_rate_limit(
        self,
        retry_after: str | int | float | None = None,
        *,
        now: float | None = None,
        wall_clock: datetime | None = None,
    ) -> ResourceAdjustment:
        timestamp = self._clock() if now is None else float(now)
        if not math.isfinite(timestamp):
            raise ValueError("now must be finite")
        retry_seconds = parse_retry_after(retry_after, now=wall_clock)
        callback: EventCallback | None
        with self._lock:
            current_delay = self.delay_controller.get_delay()
            proposed_delay = max(current_delay + 0.5, current_delay * 2.0, retry_seconds or 0.0)
            reason = "HTTP 429 rate limit"
            if retry_seconds is not None:
                reason += f" with Retry-After {retry_seconds:.3g}s"
            adjustment = self._adjust_locked(
                action="rate_limit",
                worker_delta=-1,
                proposed_delay=proposed_delay,
                reason=reason,
                timestamp=timestamp,
                force_event=True,
            )
            self._pressure_count = 0
            self._healthy_count = 0
            callback = self._event_callback
        self._emit(callback, adjustment)
        return adjustment

    def defer_requests(
        self,
        delay_seconds: float,
        *,
        reason: str = "server-requested backoff",
        now: float | None = None,
    ) -> ResourceAdjustment:
        timestamp = self._clock() if now is None else float(now)
        requested = float(delay_seconds)
        if not math.isfinite(timestamp) or not math.isfinite(requested):
            raise ValueError("backoff delay and now must be finite")
        requested = max(0.0, requested)
        callback: EventCallback | None
        with self._lock:
            adjustment = self._adjust_locked(
                action="backoff",
                worker_delta=-1,
                proposed_delay=max(self.delay_controller.get_delay(), requested),
                reason=str(reason or "server-requested backoff")[:500],
                timestamp=timestamp,
                force_event=True,
            )
            assert adjustment is not None
            self._pressure_count = 0
            self._healthy_count = 0
            callback = self._event_callback
        self._emit(callback, adjustment)
        return adjustment

    back_off = defer_requests

    def observe_response(
        self,
        status_code: int,
        headers: dict[str, str] | None = None,
        *,
        url: str = "",
        retry_after_seconds: float | None = None,
    ) -> ResourceAdjustment | None:
        del url
        status = int(status_code)
        retry_value: str | float | None = retry_after_seconds
        if retry_value is None and headers:
            for key, value in headers.items():
                if str(key).casefold() == "retry-after":
                    retry_value = str(value)
                    break
        if status == 429:
            return self.record_rate_limit(retry_value)
        if status == 503 and retry_value is not None:
            seconds = parse_retry_after(retry_value)
            if seconds is not None:
                return self.defer_requests(seconds, reason="HTTP 503 Retry-After")
        return None

    on_response = observe_response

    @staticmethod
    def _counter_delta(current: int, previous: int) -> int:
        # A lower value means the producer deliberately reset its window.
        return current - previous if current >= previous else current

    def _pressure_reasons(
        self,
        snapshot: ResourceSnapshot,
        *,
        new_rate_limits: int,
        new_llm_failures: int,
    ) -> list[str]:
        reasons: list[str] = []
        if snapshot.cpu_percent is not None and snapshot.cpu_percent >= self.high_cpu_percent:
            reasons.append("CPU pressure")
        if (
            snapshot.available_memory_bytes is not None
            and snapshot.available_memory_bytes <= self.minimum_available_memory_bytes
        ):
            reasons.append("low available memory")
        if snapshot.memory_percent is not None and snapshot.memory_percent >= self.high_memory_percent:
            reasons.append("memory pressure")
        if snapshot.http_error_rate >= self.high_error_rate:
            reasons.append("HTTP errors")
        if snapshot.timeout_rate >= self.high_timeout_rate:
            reasons.append("request timeouts")
        if new_rate_limits > 0:
            reasons.append("rate limits")
        if new_llm_failures > 0:
            reasons.append("LLM failures")
        return reasons

    def _is_healthy(
        self,
        snapshot: ResourceSnapshot,
        *,
        new_rate_limits: int,
        new_llm_failures: int,
    ) -> bool:
        if snapshot.backlog < max(self.healthy_backlog, self._target_workers):
            return False
        # Error-free backlog is not evidence of low system utilization. If both
        # CPU and memory telemetry are unavailable, hold the current target; the
        # pressure/error paths still conservatively scale down.
        if (
            snapshot.cpu_percent is None
            and snapshot.available_memory_bytes is None
            and snapshot.memory_percent is None
        ):
            return False
        if snapshot.cpu_percent is not None and snapshot.cpu_percent > self.healthy_cpu_percent:
            return False
        if (
            snapshot.available_memory_bytes is not None
            and snapshot.available_memory_bytes <= self.minimum_available_memory_bytes * 2
        ):
            return False
        if snapshot.memory_percent is not None and snapshot.memory_percent >= 75.0:
            return False
        return (
            snapshot.http_error_rate < min(0.05, self.high_error_rate)
            and snapshot.timeout_rate < min(0.03, self.high_timeout_rate)
            and new_rate_limits == 0
            and new_llm_failures == 0
        )

    def _adjust_locked(
        self,
        *,
        action: str,
        worker_delta: int,
        proposed_delay: float,
        reason: str,
        timestamp: float,
        force_event: bool = False,
    ) -> ResourceAdjustment | None:
        previous_workers = self._target_workers
        previous_delay = self.delay_controller.get_delay()
        target = min(self.max_workers, max(self.min_workers, previous_workers + worker_delta))
        delay = self.delay_controller.set_delay(proposed_delay)
        if target == previous_workers and math.isclose(delay, previous_delay) and not force_event:
            return None
        self._target_workers = target
        self._last_adjustment_at = timestamp
        return ResourceAdjustment(
            action=action,
            previous_workers=previous_workers,
            target_workers=target,
            previous_delay_seconds=previous_delay,
            delay_seconds=delay,
            reason=reason,
            observed_at=timestamp,
        )

    @staticmethod
    def _emit(
        callback: EventCallback | None,
        adjustment: ResourceAdjustment | None,
    ) -> None:
        if callback is None or adjustment is None:
            return
        try:
            callback(adjustment)
        except Exception:
            # Logging/persistence hooks cannot be allowed to break dispatch control.
            return


def parse_retry_after(
    value: str | int | float | None,
    *,
    now: datetime | None = None,
) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or float(value) < 0:
            return None
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        seconds = (parsed - reference).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def read_nvidia_telemetry(*, timeout_seconds: float = 1.5) -> NvidiaTelemetry | None:
    command = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "check": False,
        "timeout": max(0.1, min(float(timeout_seconds), 10.0)),
    }
    creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creation_flag:
        kwargs["creationflags"] = creation_flag
    try:
        result = subprocess.run(command, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    utilizations: list[float] = []
    memory_used_mib = 0.0
    memory_total_mib = 0.0
    try:
        for line in str(result.stdout or "").splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 3:
                continue
            utilization, memory_used, memory_total = (float(field) for field in fields)
            if not all(math.isfinite(value) and value >= 0 for value in (utilization, memory_used, memory_total)):
                continue
            utilizations.append(min(utilization, 100.0))
            memory_used_mib += memory_used
            memory_total_mib += memory_total
    except (TypeError, ValueError):
        return None
    if not utilizations:
        return None
    return NvidiaTelemetry(
        utilization_percent=max(utilizations),
        memory_used_bytes=int(memory_used_mib * MEBIBYTE),
        memory_total_bytes=int(memory_total_mib * MEBIBYTE),
    )


def collect_resource_snapshot(
    *,
    backlog: int = 0,
    active_workers: int = 0,
    http_error_rate: float = 0.0,
    timeout_rate: float = 0.0,
    rate_limit_count: int = 0,
    llm_latency_seconds: float | None = None,
    llm_failures: int = 0,
    include_gpu: bool = True,
    observed_at: float | None = None,
) -> ResourceSnapshot:
    cpu_percent: float | None = None
    available_memory_bytes: int | None = None
    memory_percent: float | None = None
    try:
        import psutil  # type: ignore[import-not-found]

        cpu_percent = float(psutil.cpu_percent(interval=None))
        memory = psutil.virtual_memory()
        available_memory_bytes = int(memory.available)
        memory_percent = float(memory.percent)
    except (ImportError, AttributeError, OSError, RuntimeError, ValueError):
        pass

    gpu = read_nvidia_telemetry() if include_gpu else None
    return ResourceSnapshot(
        observed_at=time.monotonic() if observed_at is None else float(observed_at),
        cpu_percent=cpu_percent,
        available_memory_bytes=available_memory_bytes,
        memory_percent=memory_percent,
        backlog=max(0, int(backlog)),
        active_workers=max(0, int(active_workers)),
        http_error_rate=min(1.0, max(0.0, float(http_error_rate))),
        timeout_rate=min(1.0, max(0.0, float(timeout_rate))),
        rate_limit_count=max(0, int(rate_limit_count)),
        llm_latency_seconds=llm_latency_seconds,
        llm_failures=max(0, int(llm_failures)),
        gpu_utilization_percent=gpu.utilization_percent if gpu else None,
        gpu_memory_used_bytes=gpu.memory_used_bytes if gpu else None,
        gpu_memory_total_bytes=gpu.memory_total_bytes if gpu else None,
    )


__all__ = [
    "AdaptiveResourceController",
    "EventCallback",
    "NvidiaTelemetry",
    "ResourceAdjustment",
    "ResourceSnapshot",
    "ThreadSafeDelayController",
    "collect_resource_snapshot",
    "parse_retry_after",
    "read_nvidia_telemetry",
]
