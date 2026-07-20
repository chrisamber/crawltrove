"""Structured stdlib logging for the service.

One configure_logging() call at startup replaces the ad-hoc print() statements
that used to scatter across the app. The whole codebase already logs through
`logging.getLogger(<module>)`; this just wires the root logger to stderr with a
compact, parseable format. Honors:

    LOG_LEVEL   default INFO
    LOG_FORMAT  "json" for one JSON object per line (good for log shippers),
                anything else for a human "ts level logger msg" line.

Idempotent — safe to call more than once (the startup hook may re-run on the
deploy pipeline's container restarts).
"""
import json as _json
import logging
import os
import sys

_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Wire the root logger once. No-op on subsequent calls."""
    global _configured
    if _configured:
        return
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("LOG_FORMAT", "").lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level_name, logging.INFO))
    _configured = True
