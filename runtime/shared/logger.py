import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# --- Contextvars para trazabilidad por request ---
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def set_request_id(value: str) -> None:
    _request_id.set(value)


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


class JsonFormatter(logging.Formatter):
    """Formateador que serializa cada LogRecord como un objeto JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "component": record.name,
            "request_id": _request_id.get(),
            "correlation_id": _correlation_id.get(),
        }

        # Propaga cualquier campo extra pasado al logger
        for key, value in record.__dict__.items():
            if key not in {
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "levelname", "levelno", "lineno",
                "message", "module", "msecs", "msg", "name", "pathname",
                "process", "processName", "relativeCreated", "stack_info",
                "thread", "threadName", "taskName",
            } and key not in payload:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def _build_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    return handler


def get_logger(component_name: str) -> logging.Logger:
    """
    Devuelve un logger configurado con salida JSON estructurada.

    Uso:
        from runtime.shared.logger import get_logger
        logger = get_logger(__name__)
        logger.info("Mensaje", extra={"key": "value"})
    """
    logger = logging.getLogger(component_name)

    if not logger.handlers:
        logger.addHandler(_build_handler())
        logger.propagate = False

    from runtime.shared.config import settings
    logger.setLevel(settings.LOG_LEVEL.upper())
    return logger
