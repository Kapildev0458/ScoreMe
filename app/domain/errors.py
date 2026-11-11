"""Domain errors. Each maps to one HTTP status in the API layer.

The core never imports FastAPI: ``http_status`` is just metadata so the API
layer can translate errors generically.
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    http_status = 400
    code = "domain_error"

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class WorkflowNotFound(DomainError):
    http_status = 404
    code = "workflow_not_found"


class RequestNotFound(DomainError):
    http_status = 404
    code = "request_not_found"


class InputValidationError(DomainError):
    http_status = 422
    code = "invalid_input"


class ConfigError(DomainError):
    http_status = 400
    code = "invalid_config"


class IdempotencyConflict(DomainError):
    http_status = 409
    code = "idempotency_conflict"


class RequestInProgress(DomainError):
    http_status = 409
    code = "request_in_progress"


class InvalidStateTransition(DomainError):
    http_status = 409
    code = "invalid_state_transition"


class PersistenceError(DomainError):
    http_status = 503
    code = "persistence_failure"
