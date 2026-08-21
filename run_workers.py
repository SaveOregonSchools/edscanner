from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Callable

from common import connect_db, init_db, utc_now_iso
from profile_runs import execute_profile_discovery_run
from search_engine import execute_search_run


LOGGER = logging.getLogger(__name__)

SEARCH_QUEUE: queue.Queue[int] = queue.Queue()
PROFILE_DISCOVERY_QUEUE: queue.Queue[int] = queue.Queue()

_SEARCH_WORKER_STARTED = False
_PROFILE_WORKER_STARTED = False
_START_LOCK = threading.Lock()
_SEARCH_RESOURCE_CONTROLLER_FACTORY: Callable[..., Any] | None = None


def register_search_resource_controller_factory(factory: Callable[..., Any] | None) -> None:
    """Register the optional factory used to restore adaptive search controls.

    The factory may accept keyword arguments ``run_id`` and ``resource_policy``.
    A positional ``resource_policy``-only callable is also supported for simple
    integrations and tests.
    """

    global _SEARCH_RESOURCE_CONTROLLER_FACTORY
    _SEARCH_RESOURCE_CONTROLLER_FACTORY = factory


def _resource_controller_for_run(run_id: int) -> Any | None:
    factory = _SEARCH_RESOURCE_CONTROLLER_FACTORY
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT adaptive_enabled, resource_policy_json
            FROM search_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None or not bool(row["adaptive_enabled"]):
        return None
    try:
        policy = json.loads(row["resource_policy_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        policy = {}
    if not isinstance(policy, dict):
        policy = {}
    if factory is None:
        try:
            from guided_search.resources import (
                AdaptiveResourceController,
                ThreadSafeDelayController,
            )

            minimum = max(1, int(policy.get("min_workers", 1)))
            maximum = max(minimum, min(int(policy.get("max_workers", minimum)), 8))
            initial = max(
                minimum,
                min(int(policy.get("initial_workers", maximum)), maximum),
            )
            initial_delay = max(0.0, float(policy.get("initial_delay_seconds", 0.75)))
            maximum_delay = max(
                initial_delay,
                float(policy.get("max_delay_seconds", 120.0)),
            )
            return AdaptiveResourceController(
                min_workers=minimum,
                max_workers=maximum,
                initial_workers=initial,
                delay_controller=ThreadSafeDelayController(
                    initial_delay,
                    min_delay_seconds=max(0.0, float(policy.get("min_delay_seconds", 0.0))),
                    max_delay_seconds=maximum_delay,
                ),
                event_callback=lambda adjustment: LOGGER.info(
                    "Search run %s resource adjustment: %s",
                    run_id,
                    adjustment.reason,
                ),
            )
        except (ImportError, TypeError, ValueError):
            LOGGER.exception("Could not restore adaptive controller for search run %s", run_id)
            return None
    try:
        return factory(run_id=run_id, resource_policy=policy)
    except TypeError:
        return factory(policy)


def enqueue_search_run(run_id: int) -> None:
    SEARCH_QUEUE.put(int(run_id))
    LOGGER.info("Queued search run %s", run_id)


def enqueue_profile_discovery_run(run_id: int) -> None:
    PROFILE_DISCOVERY_QUEUE.put(int(run_id))
    LOGGER.info("Queued profile discovery run %s", run_id)


def search_worker() -> None:
    while True:
        run_id = SEARCH_QUEUE.get()
        try:
            with connect_db() as conn:
                run = conn.execute(
                    "SELECT status FROM search_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
            if run is None or run["status"] != "queued":
                continue
            execute_search_run(
                run_id,
                resource_controller=_resource_controller_for_run(run_id),
            )
        except Exception:
            LOGGER.exception("Queued search run %s failed", run_id)
        finally:
            SEARCH_QUEUE.task_done()


def profile_discovery_worker() -> None:
    while True:
        run_id = PROFILE_DISCOVERY_QUEUE.get()
        try:
            with connect_db() as conn:
                run = conn.execute(
                    "SELECT status FROM profile_discovery_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
            if run is None or run["status"] != "queued":
                continue
            execute_profile_discovery_run(run_id)
        except Exception:
            LOGGER.exception("Queued profile discovery run %s failed", run_id)
        finally:
            PROFILE_DISCOVERY_QUEUE.task_done()


def _recover_search_runs() -> list[int]:
    init_db()
    with connect_db() as conn:
        now = utc_now_iso()
        conn.execute(
            """
            UPDATE search_runs
            SET status = 'failed', finished_at = ?,
                error_message = 'Staged Guided Search child was never linked; no district work was executed.'
            WHERE status = 'staging'
              AND NOT EXISTS (
                  SELECT 1 FROM guided_search_child_runs child
                  WHERE child.child_type = 'search'
                    AND child.child_run_id = search_runs.id
              )
            """,
            (now,),
        )
        conn.execute(
            """
            UPDATE search_run_items
            SET status = 'queued', started_at = NULL,
                finished_at = NULL, queued_at = ?, updated_at = ?,
                error_message = 'Item was queued again after app restart.'
            WHERE status = 'running'
              AND run_id IN (SELECT id FROM search_runs WHERE status = 'running')
            """,
            (now, now),
        )
        conn.execute(
            """
            UPDATE search_runs
            SET status = 'queued',
                error_message = 'Run was queued again after app restart.'
            WHERE status = 'running'
            """
        )
        queued_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM search_runs WHERE status = 'queued' ORDER BY id"
            )
        ]
        conn.commit()
    return queued_ids


def _recover_profile_runs() -> list[int]:
    init_db()
    with connect_db() as conn:
        now = utc_now_iso()
        conn.execute(
            """
            UPDATE profile_discovery_runs
            SET status = 'failed', finished_at = ?,
                error_message = 'Staged Guided Search child was never linked; no profile work was executed.'
            WHERE status = 'staging'
              AND NOT EXISTS (
                  SELECT 1 FROM guided_search_child_runs child
                  WHERE child.child_type = 'profile_discovery'
                    AND child.child_run_id = profile_discovery_runs.id
              )
            """,
            (now,),
        )
        conn.execute(
            """
            UPDATE profile_discovery_run_items
            SET status = 'queued', started_at = NULL,
                finished_at = NULL, queued_at = ?, updated_at = ?,
                error_message = 'Item was queued again after app restart.'
            WHERE status = 'running'
              AND run_id IN (
                  SELECT id FROM profile_discovery_runs WHERE status = 'running'
              )
            """,
            (now, now),
        )
        conn.execute(
            """
            UPDATE profile_discovery_runs
            SET status = 'queued',
                error_message = 'Run was queued again after app restart.'
            WHERE status = 'running'
            """
        )
        queued_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM profile_discovery_runs WHERE status = 'queued' ORDER BY id"
            )
        ]
        conn.commit()
    return queued_ids


def start_search_worker() -> None:
    global _SEARCH_WORKER_STARTED
    with _START_LOCK:
        if _SEARCH_WORKER_STARTED:
            return
        queued_ids = _recover_search_runs()
        thread = threading.Thread(
            target=search_worker,
            name="EdScannerSearchWorker",
            daemon=True,
        )
        thread.start()
        _SEARCH_WORKER_STARTED = True
    for run_id in queued_ids:
        enqueue_search_run(run_id)


def start_profile_discovery_worker() -> None:
    global _PROFILE_WORKER_STARTED
    with _START_LOCK:
        if _PROFILE_WORKER_STARTED:
            return
        queued_ids = _recover_profile_runs()
        thread = threading.Thread(
            target=profile_discovery_worker,
            name="EdScannerProfileDiscoveryWorker",
            daemon=True,
        )
        thread.start()
        _PROFILE_WORKER_STARTED = True
    for run_id in queued_ids:
        enqueue_profile_discovery_run(run_id)


def start_run_workers() -> None:
    start_search_worker()
    start_profile_discovery_worker()
