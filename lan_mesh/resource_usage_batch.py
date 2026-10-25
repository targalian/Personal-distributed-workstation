"""Shared idempotent usage batch adapter for HTTP and Worker WebSocket."""

from .event_bus import publish_event

def apply_usage_batch(records: list) -> dict:
    """M5-2: 批量用量记录应用 (HTTP /api/resources/usage 与
    /ws/worker 直推通道共用) — usage_id 幂等去重。

    Returns:
        {"total", "recorded", "duplicate"}
    """
    from .model_resources import record_usage_global
    recorded = duplicate = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        res = record_usage_global(
            rec.get("model", ""),
            rec.get("input_tokens", 0),
            rec.get("output_tokens", 0),
            usage_id=str(rec.get("usage_id", "")),
            task_id=str(rec.get("task_id", "")),
            project_id=str(rec.get("project_id", "")),
        )
        if res.get("duplicate"):
            duplicate += 1
        elif res.get("tracked"):
            recorded += 1
    publish_event("usage_reported",
                  {"total": len(records), "recorded": recorded,
                   "duplicate": duplicate})
    return {"total": len(records), "recorded": recorded,
            "duplicate": duplicate}


