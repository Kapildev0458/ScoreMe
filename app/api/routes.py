"""REST endpoints."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from app.configuration.registry import parse_config
from app.container import Container
from app.domain.errors import ConfigError

router = APIRouter()
admin_router = APIRouter(prefix="/admin", tags=["admin (demo / testing)"])


def get_container(request: Request) -> Container:
    return request.app.state.container


# ----------------------------------------------------------------------------- schemas
class ReviewBody(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    reviewer: str = Field(min_length=1)
    note: str = ""


class FaultBody(BaseModel):
    mode: Literal["none", "fail_always", "fail_times", "timeout_always", "random"]
    count: int = Field(0, ge=0)
    failure_rate: float = Field(0.0, ge=0.0, le=1.0)


class StorageFaultBody(BaseModel):
    transaction: Literal["intake", "claim", "outcome", "review"] = "outcome"
    times: int = Field(1, ge=1, le=10)


# ----------------------------------------------------------------------------- health
@router.get("/health", tags=["system"])
def health(c: Container = Depends(get_container)) -> dict[str, Any]:
    return {"status": "ok", "workflows": [w["workflow_id"] for w in c.registry.list()],
            "dependencies": c.gateway.status()}


# ----------------------------------------------------------------------------- workflows
@router.get("/workflows", tags=["workflows"])
def list_workflows(c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    return c.registry.list()


@router.get("/workflows/{workflow_id}", tags=["workflows"])
def get_workflow(workflow_id: str, version: int | None = None, c: Container = Depends(get_container)) -> dict:
    cfg = c.registry.get(workflow_id, version)
    return {"config_hash": c.registry.hash_of(workflow_id, cfg.version), "config": cfg.model_dump(mode="json")}


@router.put("/workflows/{workflow_id}", tags=["workflows"], status_code=201)
def update_workflow(workflow_id: str, config: dict[str, Any] = Body(...),
                    c: Container = Depends(get_container)) -> dict[str, Any]:
    """Publish a new version of a workflow (rules change without code change or restart).

    The config is fully validated first; if invalid, the active version is untouched.
    """
    if config.get("workflow_id", workflow_id) != workflow_id:
        raise ConfigError("workflow_id in body does not match URL")
    config = {**config, "workflow_id": workflow_id}
    cfg = c.registry.register(parse_config(config, "api"), auto_version=True)
    return {"workflow_id": workflow_id, "active_version": cfg.version,
            "config_hash": c.registry.hash_of(workflow_id, cfg.version)}


# ----------------------------------------------------------------------------- requests
@router.post("/workflows/{workflow_id}/requests", tags=["requests"], status_code=201)
def submit_request(workflow_id: str, response: Response, payload: Any = Body(...),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                   c: Container = Depends(get_container)) -> dict[str, Any]:
    """Submit a request. Send an `Idempotency-Key` header to make retries safe."""
    result = c.service.submit(workflow_id, payload, idempotency_key)
    if result.replayed:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return result.request


@router.get("/requests", tags=["requests"])
def list_requests(workflow_id: str | None = None, status: str | None = None,
                  limit: int = Query(50, le=500), c: Container = Depends(get_container)) -> list[dict]:
    rows = c.repo.list_requests(workflow_id, status, limit)
    return [{k: r[k] for k in ("id", "workflow_id", "workflow_version", "status", "decision", "attempts",
                               "created_at")} for r in rows]


@router.get("/requests/{request_id}", tags=["requests"])
def get_request(request_id: str, c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.service.get_request(request_id)


@router.get("/requests/{request_id}/history", tags=["audit"])
def get_history(request_id: str, c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    return c.service.history(request_id)


@router.get("/requests/{request_id}/audit", tags=["audit"])
def get_audit(request_id: str, c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.service.audit(request_id)


@router.get("/requests/{request_id}/explain", tags=["audit"])
def explain(request_id: str, c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.service.explain(request_id)


@router.post("/requests/{request_id}/retry", tags=["requests"])
def retry(request_id: str, c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.service.retry(request_id)


@router.post("/requests/{request_id}/review", tags=["requests"])
def review(request_id: str, body: ReviewBody, c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.service.review(request_id, body.decision, body.reviewer, body.note)


# ----------------------------------------------------------------------------- admin
@admin_router.get("/dependencies")
def dependency_status(c: Container = Depends(get_container)) -> dict[str, Any]:
    return c.gateway.status()


@admin_router.post("/dependencies/{name}/faults")
def set_fault(name: str, body: FaultBody, c: Container = Depends(get_container)) -> dict[str, Any]:
    if name not in c.gateway.service_names:
        raise ConfigError(f"Unknown dependency '{name}'", {"known": sorted(c.gateway.service_names)})
    c.gateway.service(name).faults.configure(body.mode, body.count, body.failure_rate)
    if body.mode == "none":
        c.gateway.breaker(name).reset()
    return c.gateway.status()[name]


@admin_router.post("/storage/faults")
def set_storage_fault(body: StorageFaultBody, c: Container = Depends(get_container)) -> dict[str, Any]:
    c.repo.inject_commit_failure(body.transaction, body.times)
    return {"injected": body.model_dump()}


@admin_router.post("/workflows/reload")
def reload_workflows(c: Container = Depends(get_container)) -> list[dict[str, Any]]:
    """Re-read config/workflows/*.yaml (new versions are registered, identical ones ignored)."""
    c.registry.load_directory(c.settings.config_dir)
    return c.registry.list()
