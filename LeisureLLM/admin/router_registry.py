from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Iterable


@dataclass(frozen=True)
class AdminRouterRegistration:
    name: str
    module_path: str
    description: str
    surface: str = "api"

    def load_module(self) -> ModuleType:
        return import_module(self.module_path)


ROUTER_REGISTRY: tuple[AdminRouterRegistration, ...] = (
    AdminRouterRegistration("system", "admin.routers.system", "Infrastructure, backups, jobs, and runtime operations", "ops"),
    AdminRouterRegistration("settings", "admin.routers.settings", "Setup wizard, settings, prompts, and secret management", "settings"),
    AdminRouterRegistration("model_router_api", "admin.routers.model_router_api", "Model backends, pipelines, presets, and routing diagnostics", "models"),
    AdminRouterRegistration("accounts", "admin.routers.accounts", "Authentication, bootstrap, and account management", "auth"),
    AdminRouterRegistration("knowledge", "admin.routers.knowledge", "Knowledge base indexing, import/export, and corpus management", "knowledge"),
    AdminRouterRegistration("artifacts", "admin.routers.artifacts", "Operational artifact CRUD and review actions", "artifacts"),
    AdminRouterRegistration("continuity", "admin.routers.continuity", "Operational continuity states, invariants, and sweep controls", "continuity"),
    AdminRouterRegistration("review_queue", "admin.routers.review_queue", "Unified review queue for proposals and operational work", "review"),
    AdminRouterRegistration("chat", "admin.routers.chat", "Agentic chat, tool confirmation, and request streaming", "chat"),
    AdminRouterRegistration("activity", "admin.routers.activity", "Unified activity and event timeline", "activity"),
    AdminRouterRegistration("inbox", "admin.routers.inbox", "Async question threads and inbox workflows", "inbox"),
    AdminRouterRegistration("moat", "admin.routers.moat", "Preferences, retention, backups, and feedback loops", "moat"),
    AdminRouterRegistration("kb_search", "admin.routers.kb_search", "Knowledge base search interfaces", "knowledge"),
    AdminRouterRegistration("retrieval_log", "admin.routers.retrieval_log", "Retrieval and inference trace inspection", "observability"),
    AdminRouterRegistration("pulse", "admin.routers.pulse", "Unified operational attention surface", "pulse"),
)


def iter_router_modules() -> Iterable[tuple[AdminRouterRegistration, ModuleType]]:
    for registration in ROUTER_REGISTRY:
        yield registration, registration.load_module()