"""logger.py -- structured JSON logging to stdout (Render captures it)."""
import json
import sys
from datetime import datetime, timezone


def log(event: str, **fields):
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event}
    rec.update(fields)
    try:
        sys.stdout.write(json.dumps(rec, default=str) + "\n")
        sys.stdout.flush()
    except Exception:
        pass
