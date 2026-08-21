from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from common import GUIDED_SEARCH_RUN_LOGS_DIR, connect_db, utc_now_iso
from search_engine import RunDebugLogger

from . import storage
from .orchestrator import GuidedSearchOrchestrator
from .resources import (
    AdaptiveResourceController,
    ResourceAdjustment,
    ThreadSafeDelayController,
    collect_resource_snapshot,
)


LOGGER = logging.getLogger(__name__)
GUIDED_SEARCH_QUEUE: queue.Queue[int] = queue.Queue()
_GUIDED_WORKER_STARTED = False
_START_LOCK = threading.Lock()
_PENDING_LOCK = threading.Lock()
_PENDING_SESSION_IDS: set[int] = set()
_ORCHESTRATOR_FACTORY: Callable[[], GuidedSearchOrchestrator] = GuidedSearchOrchestrator


def register_orchestrator_factory(factory: Callable[[], GuidedSearchOrchestrator]) -> None:
    global _ORCHESTRATOR_FACTORY
    _ORCHESTRATOR_FACTORY = factory


def enqueue_guided_search(session_id: int, *, delay_seconds: float = 0.0) -> None:
    session_id = int(session_id)
    with _PENDING_LOCK:
        if session_id in _PENDING_SESSION_IDS:
            return
        _PENDING_SESSION_IDS.add(session_id)

    def deliver() -> None:
        GUIDED_SEARCH_QUEUE.put(session_id)

    delay = max(0.0, float(delay_seconds))
    if delay <= 0:
        deliver()
        return
    timer = threading.Timer(delay, deliver)
    timer.name = f"GuidedSearchWake-{session_id}"
    timer.daemon = True
    timer.start()


class SearchResourceAdapter:
    """Bridge district-search HTTP/dispatcher hooks to the deterministic controller."""

    def __init__(self, run_id: int, resource_policy: Mapping[str, Any]) -> None:
        self.run_id = int(run_id)
        self._lock = threading.RLock()
        self._requests = 0
        self._errors = 0
        self._timeouts = 0
        self._rate_limits = 0
        self._samples = 0
        minimum = max(1, int(resource_policy.get("min_workers") or 1))
        maximum = max(minimum, min(8, int(resource_policy.get("max_workers") or 4)))
        initial = max(minimum, min(maximum, int(resource_policy.get("initial_workers") or minimum)))
        initial_delay = max(0.0, float(resource_policy.get("initial_delay_seconds") or 0.75))
        self.delay_controller = ThreadSafeDelayController(
            initial_delay,
            min_delay_seconds=0.0,
            max_delay_seconds=120.0,
        )
        self.controller = AdaptiveResourceController(
            min_workers=minimum,
            max_workers=maximum,
            initial_workers=initial,
            delay_controller=self.delay_controller,
            event_callback=self._record_adjustment,
        )

    @property
    def target_workers(self) -> int:
        return self.controller.target_workers

    @property
    def current_delay_seconds(self) -> float:
        return self.delay_controller.get_delay()

    def before_request(self, **_kwargs: Any) -> None:
        self.delay_controller.wait()
        with self._lock:
            self._requests += 1

    def after_request(self) -> None:
        return None

    def observe_response(
        self,
        *,
        status_code: int,
        retry_after_seconds: float | None = None,
        **_kwargs: Any,
    ) -> None:
        status = int(status_code)
        with self._lock:
            if status >= 400:
                self._errors += 1
            if status == 429:
                self._rate_limits += 1
        self.controller.observe_response(
            status,
            retry_after_seconds=retry_after_seconds,
        )

    def observe_error(self, error: BaseException, **_kwargs: Any) -> None:
        with self._lock:
            self._errors += 1
            name = type(error).__name__.casefold()
            if "timeout" in name:
                self._timeouts += 1

    def observe_district_completion(
        self,
        *,
        success: bool,
        backlog: int,
        active: int,
        **_kwargs: Any,
    ) -> None:
        with self._lock:
            self._samples += 1
            requests = max(1, self._requests)
            error_rate = self._errors / requests
            timeout_rate = self._timeouts / requests
            rate_limits = self._rate_limits
            include_gpu = self._samples == 1 or self._samples % 10 == 0
        snapshot = collect_resource_snapshot(
            backlog=max(0, int(backlog)),
            active_workers=max(0, int(active)),
            http_error_rate=min(1.0, error_rate),
            timeout_rate=min(1.0, timeout_rate),
            rate_limit_count=rate_limits,
            include_gpu=include_gpu,
        )
        self.controller.observe(snapshot)

    def _record_adjustment(self, adjustment: ResourceAdjustment) -> None:
        with connect_db() as conn:
            link = conn.execute(
                """
                SELECT session_id
                FROM guided_search_child_runs
                WHERE child_type = 'search' AND child_run_id = ?
                """,
                (self.run_id,),
            ).fetchone()
        if link is None:
            return
        session_id = int(link["session_id"])
        if adjustment.workers_changed:
            direction = "Reduced" if adjustment.target_workers < adjustment.previous_workers else "Increased"
            description = (
                f"{direction} workers from {adjustment.previous_workers} to "
                f"{adjustment.target_workers} after {adjustment.reason}."
            )
        else:
            description = (
                f"Adjusted request delay from {adjustment.previous_delay_seconds:.2f}s to "
                f"{adjustment.delay_seconds:.2f}s after {adjustment.reason}."
            )
        try:
            storage.update_session(
                session_id,
                current_workers=adjustment.target_workers,
                current_delay_seconds=adjustment.delay_seconds,
            )
            storage.add_step(
                session_id,
                "resource_adjustment",
                status="completed",
                short_description=description,
                input_data={
                    "action": adjustment.action,
                    "reason": adjustment.reason,
                    "previous_workers": adjustment.previous_workers,
                    "target_workers": adjustment.target_workers,
                    "previous_delay_seconds": adjustment.previous_delay_seconds,
                    "delay_seconds": adjustment.delay_seconds,
                },
            )
            session = storage.get_session(session_id)
            debug_path = (
                Path(str(session.get("debug_log_path")))
                if session and session.get("debug_log_path")
                else GUIDED_SEARCH_RUN_LOGS_DIR / f"guided-search-{session_id}.log"
            )
            RunDebugLogger(debug_path).log(
                "resource_adjustment",
                session_id=session_id,
                run_id=self.run_id,
                action=adjustment.action,
                reason=adjustment.reason,
                previous_workers=adjustment.previous_workers,
                target_workers=adjustment.target_workers,
                previous_delay_seconds=adjustment.previous_delay_seconds,
                delay_seconds=adjustment.delay_seconds,
            )
        except Exception:
            LOGGER.exception("Could not persist Guided Search resource adjustment")


def resource_controller_for_search_run(
    *,
    run_id: int,
    resource_policy: Mapping[str, Any],
) -> SearchResourceAdapter:
    return SearchResourceAdapter(run_id, resource_policy)


def guided_search_worker() -> None:
    orchestrator = _ORCHESTRATOR_FACTORY()
    while True:
        session_id = GUIDED_SEARCH_QUEUE.get()
        with _PENDING_LOCK:
            _PENDING_SESSION_IDS.discard(session_id)
        try:
            outcome = orchestrator.advance(session_id)
            if outcome.wake_after_seconds is not None:
                enqueue_guided_search(session_id, delay_seconds=outcome.wake_after_seconds)
        except Exception:
            LOGGER.exception("Queued Guided Search session %s failed", session_id)
        finally:
            GUIDED_SEARCH_QUEUE.task_done()


def start_guided_search_worker() -> None:
    global _GUIDED_WORKER_STARTED
    if _GUIDED_WORKER_STARTED:
        return
    with _START_LOCK:
        if _GUIDED_WORKER_STARTED:
            return
        now = utc_now_iso()
        with connect_db() as conn:
            # Claims are process-local leases. A fresh process can safely release
            # claims left by its predecessor while preserving each persisted stage.
            conn.execute(
                """
                UPDATE guided_search_sessions
                SET claim_token = NULL, claim_expires_at = NULL,
                    next_wake_at = COALESCE(next_wake_at, ?), updated_at = ?
                WHERE status IN ('planning','queued','profiling','searching',
                                 'evaluating','replanning','summarizing')
                   OR (
                       cancel_requested = 1
                       AND status IN ('draft','needs_clarification','ready','needs_review')
                   )
                """,
                (now, now),
            )
            session_ids = [
                int(row["id"])
                for row in conn.execute(
                    """
                    SELECT id FROM guided_search_sessions
                    WHERE status IN ('planning','queued','profiling','searching',
                                     'evaluating','replanning','summarizing')
                       OR (
                           cancel_requested = 1
                           AND status IN ('draft','needs_clarification','ready','needs_review')
                       )
                    ORDER BY id
                    """
                )
            ]
            conn.commit()
        thread = threading.Thread(
            target=guided_search_worker,
            name="EdScannerGuidedSearchWorker",
            daemon=True,
        )
        thread.start()
        _GUIDED_WORKER_STARTED = True
        for session_id in session_ids:
            enqueue_guided_search(session_id)


__all__ = [
    "GUIDED_SEARCH_QUEUE",
    "SearchResourceAdapter",
    "enqueue_guided_search",
    "register_orchestrator_factory",
    "resource_controller_for_search_run",
    "start_guided_search_worker",
]
