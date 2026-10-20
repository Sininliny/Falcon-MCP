"""Full local JSONL audit events; never log bridge credentials or audio samples."""

from __future__ import annotations

import contextvars
import json
import math
import os
import threading
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

trace_id = contextvars.ContextVar("falcon_trace_id", default=None)
_secrets = {"token", "password", "authorization", "api_key", "secret"}


def redact(value: Any):
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite": str(value)}
    if isinstance(value, dict):
        return {k: "[REDACTED]" if str(k).casefold() in _secrets else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, root: Path):
        self.root = root
        self.session = uuid.uuid4().hex
        self.path = root / f"audit-{datetime.now(UTC):%Y%m%d}-{os.getpid()}-{self.session}.jsonl"
        self.lock = threading.Lock()

    def event(self, event: str, **data):
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "session": self.session,
            "trace_id": trace_id.get(),
            "event": event,
            **redact(data),
        }
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, default=str)
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf8") as handle:
                handle.write(encoded + "\n")

    def error(self, event: str, exc: BaseException, **data):
        self.event(
            event,
            error=str(exc),
            exception_type=type(exc).__name__,
            traceback="".join(traceback.format_exception(exc)),
            **data,
        )

    def read(self, offset: int = 0, limit: int = 50, event: str = "", session: str = "") -> dict:
        # Pagination includes the current session and previous sessions, never credential files.
        selected, total = [], 0
        for path in sorted(self.root.glob("audit-*.jsonl")):
            with path.open(encoding="utf8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # a concurrently appended final line can be incomplete
                    if event and record.get("event") != event:
                        continue
                    if session and record.get("session") != session:
                        continue
                    if offset <= total < offset + limit:
                        selected.append(record)
                    total += 1
        return {
            "items": selected,
            "total_count": total,
            "has_more": offset + limit < total,
            "next_offset": min(total, offset + limit),
            "log_root": str(self.root),
        }
