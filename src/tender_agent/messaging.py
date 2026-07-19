from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from .persistence import StateStore, canonical_json


@dataclass(frozen=True)
class MessageDraft:
    draft_id: str
    provider: str
    recipient: str
    body: str
    request_id: str
    supplier_id: str = ""

    @property
    def payload_hash(self) -> str:
        return sha256(canonical_json(self.__dict__).encode()).hexdigest()


class MessagingAdapter(Protocol):
    def send(self, recipient: str, body: str, idempotency_key: str) -> str: ...


class ApprovedMessageDispatcher:
    def __init__(self, store: StateStore, adapter: MessagingAdapter) -> None:
        self.store = store
        self.adapter = adapter

    def dispatch(
        self, draft: MessageDraft, approval_id: str, idempotency_key: str
    ) -> dict[str, str]:
        cached = self.store.side_effect_result(idempotency_key, draft.payload_hash)
        if cached is not None:
            return {str(key): str(value) for key, value in cached.items()}
        if not self.store.consume_approval(approval_id, "send_message", draft.payload_hash):
            raise PermissionError("exact message payload is not approved")
        status = self.store.begin_side_effect(idempotency_key, "send_message", draft.payload_hash)
        if status != "created":
            raise RuntimeError("side-effect outcome is uncertain; reconcile it manually")
        provider_message_id = self.adapter.send(draft.recipient, draft.body, idempotency_key)
        result = {"status": "sent", "provider_message_id": provider_message_id}
        self.store.complete_side_effect(idempotency_key, draft.payload_hash, result)
        return result
