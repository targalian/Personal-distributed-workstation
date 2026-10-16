"""Retain identity-checked in-flight results while a PM remains cooperatively paused."""

import copy
import time


def begin_pause(state) -> None:
    """Caller holds the state lock; retries keep the original continuation point."""
    if state.stop_reason == "paused" and state.pause.get("format") == 1:
        return
    state.pause = {"format": 1, "source_phase": state.phase, "requested_at": time.time(), "returned": {}}


def retain_paused_result(pm, kind: str, result: dict, *, execution_id: str = "") -> bool:
    """Save evidence only: do not accept outputs, release dependencies or deliver."""
    st = pm._state
    with st.lock:
        if (
            not st.stop_event.is_set()
            or st.stop_reason != "paused"
            or st.pause.get("format") != 1
            or pm._checkpoint_error
        ):
            return False
        if kind == "subtask":
            name = result.get("task_name")
            attempt = st.execution_attempts.get(name) if isinstance(name, str) else None
            if (
                not attempt
                or attempt.get("phase") not in ("active", "processing")
                or result.get("status") not in ("completed", "failed", "unknown")
                or any(
                    result.get(key) != attempt.get(key)
                    for key in ("subtask_id", "parent_task_id", "pm_id", "reporter_id")
                )
                or name in st.subtask_outputs
            ):
                return False
            if attempt.get("received_result"):
                return attempt["received_result"] == result
            attempt["received_result"] = copy.deepcopy(result)
            attempt["phase"] = "processing"
        elif kind == "direct":
            attempt = st.direct_execution
            if not execution_id or attempt.get("execution_id") != execution_id:
                return False
            if attempt.get("state") == "returned":
                return attempt.get("result") == result
            if attempt.get("state") != "active":
                return False
            attempt.update(state="returned", result=copy.deepcopy(result))
        elif kind in ("planning", "aggregation"):
            expected_phase = "planning" if kind == "planning" else "aggregating"
            if st.pause.get("source_phase") != expected_phase:
                return False
            returned = st.pause["returned"]
            if kind in returned:
                return returned[kind] == result
            returned[kind] = copy.deepcopy(result)
        else:
            return False
        return st.persist()  # Keep phase/stop intent paused; failure remains unknown.
