import json

from falcon_mcp.audit import AuditLog, trace_id


def test_audit_is_complete_paginated_and_redacts_credentials(config):
    log = AuditLog(config.log_root)
    token = trace_id.set("test-trace")
    try:
        log.event("request", args={"token": "do-not-log", "nested": [{"password": "secret-pw"}], "value": 0.123})
        log.event("result", result={"verified": False, "before": 1, "after": 0.5})
    finally:
        trace_id.reset(token)
    records = log.read(limit=1)
    assert records["has_more"] and records["total_count"] == 2
    assert records["items"][0]["trace_id"] == "test-trace"
    assert records["items"][0]["args"]["token"] == "[REDACTED]"
    raw = log.path.read_text(encoding="utf8")
    assert "do-not-log" not in raw and "secret-pw" not in raw
    log.event("malformed", value=float("nan"))
    for line in log.path.read_text().splitlines():
        json.loads(line)
