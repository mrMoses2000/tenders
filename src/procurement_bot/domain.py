from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class JobKind(StrEnum):
    DOWNLOAD_ATTACHMENT = "download_attachment"
    EXTRACT_DOCUMENT = "extract_document"
    TRANSCRIBE_VOICE = "transcribe_voice"
    PROCESS_INTAKE = "process_intake"
    PREPARE_RESEARCH = "prepare_research"
    START_RESEARCH = "start_research"
    EXECUTE_RESEARCH = "execute_research"
    PREPARE_SUPPLIER_OUTREACH = "prepare_supplier_outreach"
    BUILD_REPORT = "build_report"
    RESOLVE_LOCALITY = "resolve_locality"
    PROCESS_SUPPLIER_REPLY = "process_supplier_reply"
    ENQUEUE_WHATSAPP_SEND = "enqueue_whatsapp_send"


class AttachmentKind(StrEnum):
    DOCUMENT = "document"
    VOICE = "voice"
    AUDIO = "audio"
    PHOTO = "photo"


class CaseStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_CLARIFICATION = "needs_clarification"
    READY = "ready"
    RESEARCHING = "researching"
    CONTACTING = "contacting"
    EVALUATING = "evaluating"
    REPORT_READY = "report_ready"
    SELECTED = "selected"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ClarificationKind(StrEnum):
    CITY = "city"
    SEARCH_SCOPE = "search_scope"
    PRODUCT = "product"
    QUANTITY = "quantity"
    UNIT = "unit"
    TECHNICAL_SPEC = "technical_spec"
    ALLOW_ANALOGS = "allow_analogs"


@dataclass(frozen=True)
class Actor:
    user_id: UUID
    telegram_id: int
    active: bool = True
