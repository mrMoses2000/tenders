from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

from .persistence import canonical_json

IMMUTABLE_REQUEST_PROPERTIES = frozenset(
    {
        "Наименование",
        "Тех. спек",
        "Количество",
        "Единица измерения",
        "Цена за 1 единицу товара",
        "№",
    }
)


class NotionGateway(Protocol):
    def get_properties(self, page_id: str) -> Mapping[str, Any]: ...
    def update_properties(self, page_id: str, properties: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class PropertyChange:
    property_name: str
    before: Any
    after: Any


@dataclass(frozen=True)
class PagePatch:
    page_id: str
    changes: tuple[PropertyChange, ...]

    @property
    def desired_properties(self) -> dict[str, Any]:
        return {change.property_name: change.after for change in self.changes}

    @property
    def rollback_properties(self) -> dict[str, Any]:
        return {change.property_name: change.before for change in self.changes}


@dataclass(frozen=True)
class SyncPlan:
    patches: tuple[PagePatch, ...]
    plan_hash: str


@dataclass(frozen=True)
class SyncResult:
    status: str
    plan_hash: str
    applied_page_ids: tuple[str, ...] = ()
    rollback_patches: tuple[PagePatch, ...] = ()
    error: str = ""


def plan_notion_sync(
    current_rows: Mapping[str, Mapping[str, Any]],
    desired_rows: Mapping[str, Mapping[str, Any]],
) -> SyncPlan:
    missing_pages = set(desired_rows).difference(current_rows)
    if missing_pages:
        raise ValueError(f"desired rows reference unknown pages: {sorted(missing_pages)}")
    patches: list[PagePatch] = []
    for page_id in sorted(desired_rows):
        current = current_rows[page_id]
        desired = desired_rows[page_id]
        changes: list[PropertyChange] = []
        for name in sorted(desired):
            before = current.get(name)
            after = desired[name]
            if before == after:
                continue
            if name in IMMUTABLE_REQUEST_PROPERTIES:
                raise ValueError(f"immutable Notion property differs on {page_id}: {name}")
            changes.append(PropertyChange(name, before, after))
        if changes:
            patches.append(PagePatch(page_id, tuple(changes)))
    payload = [
        {
            "page_id": patch.page_id,
            "changes": [change.__dict__ for change in patch.changes],
        }
        for patch in patches
    ]
    plan_hash = sha256(canonical_json(payload).encode()).hexdigest()
    return SyncPlan(tuple(patches), plan_hash)


def apply_sync_plan(
    gateway: NotionGateway, plan: SyncPlan, dry_run: bool = True
) -> SyncResult:
    if dry_run:
        return SyncResult("dry_run", plan.plan_hash)
    applied: list[PagePatch] = []
    try:
        for patch in plan.patches:
            gateway.update_properties(patch.page_id, patch.desired_properties)
            applied.append(patch)
            current = gateway.get_properties(patch.page_id)
            mismatched = {
                name: expected
                for name, expected in patch.desired_properties.items()
                if current.get(name) != expected
            }
            if mismatched:
                raise RuntimeError(f"verification failed for {patch.page_id}: {sorted(mismatched)}")
    except Exception as error:
        rollback = tuple(
            PagePatch(
                patch.page_id,
                tuple(
                    PropertyChange(change.property_name, change.after, change.before)
                    for change in patch.changes
                ),
            )
            for patch in reversed(applied)
        )
        return SyncResult(
            "partial_failure", plan.plan_hash, tuple(item.page_id for item in applied),
            rollback, f"{type(error).__name__}: {error}",
        )
    return SyncResult("applied", plan.plan_hash, tuple(item.page_id for item in applied))


class InMemoryNotionGateway:
    def __init__(self, pages: Mapping[str, Mapping[str, Any]]) -> None:
        self.pages = {page_id: dict(properties) for page_id, properties in pages.items()}

    def get_properties(self, page_id: str) -> Mapping[str, Any]:
        return dict(self.pages[page_id])

    def update_properties(self, page_id: str, properties: Mapping[str, Any]) -> None:
        self.pages[page_id].update(properties)
