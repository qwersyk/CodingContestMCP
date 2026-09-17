"""Request-local account context; never shared between MCP callers."""

from contextvars import ContextVar

from .service import Service

account_service: ContextVar[Service] = ContextVar("ccc_account_service")


def current_service() -> Service:
    try:
        return account_service.get()
    except LookupError as error:
        raise RuntimeError("No authenticated CCC request context") from error
