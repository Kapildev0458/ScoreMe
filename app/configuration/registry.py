"""Versioned workflow registry.

* Configs are immutable once registered: (workflow_id, version) -> exact content hash.
* The *latest* version is active for new requests; every request is pinned to
  the version it was received under, so retries and audits are reproducible
  even after the rules change.
* Every version is persisted, so pinned versions survive a restart.
* A new version is validated completely before it is activated; an invalid
  config is rejected and the previously active version keeps serving.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.configuration.models import EnrichStage, WorkflowConfig
from app.domain.errors import ConfigError, WorkflowNotFound
from app.storage.repository import Repository

log = logging.getLogger(__name__)


def config_hash(cfg: WorkflowConfig) -> str:
    body = cfg.model_dump(mode="json", exclude={"version"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def parse_config(data: Any, source: str = "<input>") -> WorkflowConfig:
    try:
        return WorkflowConfig.model_validate(data)
    except ValidationError as exc:
        errors = [{"loc": ".".join(str(p) for p in e["loc"]), "error": e["msg"]} for e in exc.errors()]
        raise ConfigError(f"Invalid workflow config ({source})", {"errors": errors}) from exc


class WorkflowRegistry:
    def __init__(self, repo: Repository, known_services: set[str]) -> None:
        self._repo = repo
        self._known_services = known_services
        self._lock = threading.RLock()
        self._versions: dict[str, dict[int, WorkflowConfig]] = {}
        self._hashes: dict[tuple[str, int], str] = {}

    # ------------------------------------------------------------------ loading
    def hydrate(self) -> None:
        """Reload every persisted version (so requests pinned to old versions still work)."""
        for row in self._repo.load_workflow_versions():
            cfg = parse_config(row["config"], f"db:{row['workflow_id']}@v{row['version']}")
            with self._lock:
                self._versions.setdefault(cfg.workflow_id, {})[cfg.version] = cfg
                self._hashes[(cfg.workflow_id, cfg.version)] = row["config_hash"]

    def load_directory(self, path: str | Path) -> list[WorkflowConfig]:
        loaded = []
        for file in sorted(Path(path).glob("*.y*ml")):
            with open(file, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            loaded.append(self.register(parse_config(data, file.name)))
        return loaded

    # ------------------------------------------------------------------ registration
    def register(self, cfg: WorkflowConfig, *, auto_version: bool = False) -> WorkflowConfig:
        self._check_services(cfg)
        with self._lock:
            existing = self._versions.get(cfg.workflow_id, {})
            digest = config_hash(cfg)
            if auto_version:
                if existing:
                    latest_v = max(existing)
                    if self._hashes[(cfg.workflow_id, latest_v)] == digest:
                        return existing[latest_v]  # identical to active config: no new version
                cfg = cfg.model_copy(update={"version": max(existing, default=0) + 1})
            if cfg.version in existing:
                if self._hashes[(cfg.workflow_id, cfg.version)] == digest:
                    return existing[cfg.version]
                raise ConfigError(
                    f"Workflow '{cfg.workflow_id}' version {cfg.version} already exists with different content. "
                    "Versions are immutable: bump 'version' (or use PUT /workflows/{id}, which auto-versions).",
                    {"workflow_id": cfg.workflow_id, "version": cfg.version},
                )
            self._repo.save_workflow_version(cfg.workflow_id, cfg.version, digest, cfg.model_dump(mode="json"))
            self._versions.setdefault(cfg.workflow_id, {})[cfg.version] = cfg
            self._hashes[(cfg.workflow_id, cfg.version)] = digest
            log.info("registered workflow %s v%s (%s)", cfg.workflow_id, cfg.version, digest[:12])
            return cfg

    def _check_services(self, cfg: WorkflowConfig) -> None:
        unknown = sorted({s.service for s in cfg.stages.values()
                          if isinstance(s, EnrichStage) and s.service not in self._known_services})
        if unknown:
            raise ConfigError(f"Workflow '{cfg.workflow_id}' references unknown services {unknown}",
                              {"known_services": sorted(self._known_services)})

    # ------------------------------------------------------------------ lookup
    def get(self, workflow_id: str, version: int | None = None) -> WorkflowConfig:
        with self._lock:
            versions = self._versions.get(workflow_id)
            if not versions:
                raise WorkflowNotFound(f"Unknown workflow '{workflow_id}'", {"available": sorted(self._versions)})
            if version is None:
                return versions[max(versions)]
            if version not in versions:
                raise WorkflowNotFound(f"Workflow '{workflow_id}' has no version {version}")
            return versions[version]

    def hash_of(self, workflow_id: str, version: int) -> str:
        return self._hashes[(workflow_id, version)]

    def versions(self, workflow_id: str) -> list[int]:
        self.get(workflow_id)
        return sorted(self._versions[workflow_id])

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for wid, versions in sorted(self._versions.items()):
                active = versions[max(versions)]
                out.append({"workflow_id": wid, "name": active.name, "description": active.description,
                            "active_version": active.version, "versions": sorted(versions),
                            "config_hash": self._hashes[(wid, active.version)]})
            return out
