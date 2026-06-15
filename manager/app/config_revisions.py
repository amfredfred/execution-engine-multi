"""Durable, validated engine configuration revisions."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import yaml

from src.config.settings import AppConfig
from manager.app.config_store import AgentConfigStore
from manager.app.models import AgentStatus
from manager.app.process_supervisor import ProcessSupervisor
from manager.app.registry import AgentRegistry


class ConfigRevisionService:
    def __init__(
        self,
        registry: AgentRegistry,
        config_store: AgentConfigStore,
        supervisor: ProcessSupervisor,
    ) -> None:
        self._registry = registry
        self._config_store = config_store
        self._supervisor = supervisor

    def apply(self, engine_id: str, patch: dict) -> dict:
        reg = self._registry.get_agent(engine_id)
        if not reg:
            raise ValueError(f"Engine {engine_id} not found")
        document = self._config_store.preview_agent_config(reg, patch)
        self._validate(document, reg.data_dir)
        serialized = json.dumps(document, sort_keys=True, separators=(",", ":"))
        revision = self._registry.create_config_revision(
            engine_id,
            document,
            hashlib.sha256(serialized.encode()).hexdigest(),
        )
        self._config_store.write_config_document(reg, document)
        if reg.status in (AgentStatus.RUNNING, AgentStatus.STARTING):
            self._supervisor.terminate(engine_id)
        else:
            self._registry.activate_config_revision(engine_id, revision)
        return {"engine_id": engine_id, "revision": revision, "status": "pending_restart"}

    def worker_ready(self, engine_id: str) -> None:
        revision = self._registry.latest_desired_config_revision(engine_id)
        if revision is not None:
            self._registry.activate_config_revision(engine_id, revision)

    @staticmethod
    def _validate(document: dict, data_dir: str) -> None:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            dir=data_dir,
            encoding="utf-8",
            delete=False,
        ) as handle:
            yaml.safe_dump(document, handle, default_flow_style=False)
            path = handle.name
        try:
            AppConfig.from_yaml(path)
        finally:
            Path(path).unlink(missing_ok=True)

