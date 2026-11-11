"""Composition root: the single place where concrete components are wired together.

Everything else depends on constructor-injected collaborators, which is what
lets tests swap in a temp database, a no-sleep gateway, injected faults, etc.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.configuration.registry import WorkflowRegistry
from app.engine.executor import WorkflowExecutor
from app.services.decision_service import DecisionService
from app.services.dependencies import DependencyGateway
from app.storage.repository import Repository

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    db_path: str = str(ROOT / "data" / "scoreme.db")
    config_dir: str = str(ROOT / "config" / "workflows")
    enable_admin: bool = True  # fault-injection / reload endpoints (disable in production)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.getenv("SCOREME_DB", cls.db_path),
            config_dir=os.getenv("SCOREME_CONFIG_DIR", cls.config_dir),
            enable_admin=os.getenv("SCOREME_ENABLE_ADMIN", "true").lower() == "true",
        )


@dataclass
class Container:
    settings: Settings
    repo: Repository
    gateway: DependencyGateway
    registry: WorkflowRegistry
    executor: WorkflowExecutor
    service: DecisionService


def build_container(settings: Settings | None = None, sleep: Callable[[float], None] | None = None,
                    gateway: DependencyGateway | None = None) -> Container:
    settings = settings or Settings.from_env()
    repo = Repository(settings.db_path)
    if gateway is None:
        gateway = DependencyGateway(sleep=sleep) if sleep else DependencyGateway()
    registry = WorkflowRegistry(repo, gateway.service_names)
    registry.hydrate()
    registry.load_directory(settings.config_dir)
    executor = WorkflowExecutor(gateway)
    service = DecisionService(repo, registry, executor)
    return Container(settings, repo, gateway, registry, executor, service)
