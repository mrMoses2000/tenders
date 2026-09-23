from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import asyncpg

from procurement_bot.phone import normalize_phone

STRONG_IDENTITY_TYPES = frozenset(
    {"2gis_firm_id", "domain", "bin_iin", "instagram", "source_record"}
)
CONTACT_TYPES = frozenset(
    {"phone", "whatsapp", "email", "website", "instagram", "telegram"}
)
SOURCE_TYPES = frozenset(
    {"2gis", "web", "supplier_site", "marketplace", "supplier_message", "manual", "import"}
)
SUPPLIER_TYPES = frozenset(
    {"business", "sole_proprietor", "individual", "market_stall", "unknown"}
)
OFFER_STATUSES = frozenset(
    {"lead", "confirm", "exact", "mismatch", "not_found", "withdrawn", "selected"}
)
AVAILABILITY_STATUSES = frozenset(
    {"unknown", "in_stock_unquantified", "in_stock", "preorder", "out_of_stock"}
)
VAT_STATUSES = frozenset({"unknown", "included", "excluded", "not_applicable"})
DOCUMENT_STATUSES = frozenset({"unknown", "available", "partial", "unavailable"})
DELIVERY_STATUSES = frozenset({"unknown", "pickup", "delivery", "both", "unavailable"})


class PersistenceConflictError(RuntimeError):
    """An idempotency key was reused for a different immutable fact."""


class SupplierIdentityConflictError(PersistenceConflictError):
    """A globally unique supplier identity already belongs to another supplier."""


@dataclass(frozen=True, slots=True)
class UpsertResult:
    id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class SourceEvidenceInput:
    source_type: str
    source_locator: str
    captured_at: datetime
    content_sha256: str
    raw_payload: Mapping[str, Any]
    idempotency_key: str
    research_query_id: UUID | None = None
    source_url: str | None = None
    external_id: str = ""
    title: str = ""
    snapshot_storage_path: str = ""

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"unsupported source_type: {self.source_type}")
        if not self.source_locator.strip():
            raise ValueError("source_locator must be non-empty")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if self.source_url is not None:
            parsed = urlparse(self.source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("source_url must be an absolute HTTP(S) URL")


@dataclass(frozen=True, slots=True)
class SupplierInput:
    display_name: str
    identity_type: str
    identity_value: str
    source_evidence_id: UUID | None = None
    legal_name: str = ""
    supplier_type: str = "business"
    country_code: str = "KZ"
    confidence: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("display_name must be non-empty")
        if self.identity_type not in STRONG_IDENTITY_TYPES:
            raise ValueError("supplier creation requires a strong identity")
        normalize_supplier_identity(self.identity_type, self.identity_value)
        if self.supplier_type not in SUPPLIER_TYPES:
            raise ValueError(f"unsupported supplier_type: {self.supplier_type}")
        if not re.fullmatch(r"[A-Z]{2}", self.country_code):
            raise ValueError("country_code must be an ISO alpha-2 code")
        _validate_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class SupplierLocationInput:
    source_evidence_id: UUID
    country_code: str = "KZ"
    city: str = ""
    address_text: str = ""
    district: str = ""
    market_name: str = ""
    pavilion: str = ""
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    source_provider: str = ""
    provider_place_id: str = ""
    confidence: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        if self.latitude is not None and not Decimal("-90") <= self.latitude <= Decimal("90"):
            raise ValueError("latitude is outside [-90, 90]")
        if self.longitude is not None and not Decimal("-180") <= self.longitude <= Decimal("180"):
            raise ValueError("longitude is outside [-180, 180]")
        if not re.fullmatch(r"[A-Z]{2}", self.country_code):
            raise ValueError("country_code must be an ISO alpha-2 code")
        _validate_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class SupplierContactInput:
    contact_type: str
    display_value: str
    source_evidence_id: UUID
    label: str = ""
    is_whatsapp_confirmed: bool = False
    confidence: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.contact_type not in CONTACT_TYPES:
            raise ValueError(f"unsupported contact_type: {self.contact_type}")
        normalize_contact(self.contact_type, self.display_value)
        if self.is_whatsapp_confirmed and self.contact_type not in {"phone", "whatsapp"}:
            raise ValueError("WhatsApp confirmation is valid only for phone contacts")
        _validate_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class OfferInput:
    request_item_id: UUID
    supplier_id: UUID
    idempotency_key: str
    supplier_location_id: UUID | None = None
    supplier_product_name: str = ""
    model_sku: str = ""
    status: str = "lead"

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if self.status not in OFFER_STATUSES:
            raise ValueError(f"unsupported offer status: {self.status}")


@dataclass(frozen=True, slots=True)
class OfferObservationInput:
    offer_id: UUID
    source_evidence_id: UUID
    observed_at: datetime
    observed_by: str
    source_type: str
    idempotency_key: str
    external_message_id: str = ""
    price_amount: Decimal | None = None
    currency: str | None = None
    price_unit: str | None = None
    pack_qty: Decimal | None = None
    moq: Decimal | None = None
    order_multiple: Decimal | None = None
    available_qty: Decimal | None = None
    availability_status: str = "unknown"
    vat_status: str = "unknown"
    document_status: str = "unknown"
    delivery_status: str = "unknown"
    pickup_point: str | None = None
    availability_date: date | None = None
    evidence_level: int = 1
    raw_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"unsupported source_type: {self.source_type}")
        if not self.observed_by.strip() or not self.idempotency_key.strip():
            raise ValueError("observed_by and idempotency_key must be non-empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.price_amount is not None and self.currency is None:
            raise ValueError("currency is required when price_amount is known")
        if self.currency is not None and not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("currency must be an ISO 4217-style uppercase code")
        if self.price_amount is not None and self.price_amount < 0:
            raise ValueError("price_amount cannot be negative")
        for name in ("pack_qty", "moq", "order_multiple"):
            quantity = getattr(self, name)
            if quantity is not None and quantity <= 0:
                raise ValueError(f"{name} must be positive")
        if self.available_qty is not None and self.available_qty < 0:
            raise ValueError("available_qty cannot be negative")
        if self.availability_status not in AVAILABILITY_STATUSES:
            raise ValueError(f"unsupported availability_status: {self.availability_status}")
        if self.vat_status not in VAT_STATUSES:
            raise ValueError(f"unsupported vat_status: {self.vat_status}")
        if self.document_status not in DOCUMENT_STATUSES:
            raise ValueError(f"unsupported document_status: {self.document_status}")
        if self.delivery_status not in DELIVERY_STATUSES:
            raise ValueError(f"unsupported delivery_status: {self.delivery_status}")
        if self.availability_status == "in_stock" and self.available_qty is None:
            raise ValueError("available_qty is required for in_stock")
        if not 1 <= self.evidence_level <= 5:
            raise ValueError("evidence_level must be between 1 and 5")


def normalize_supplier_identity(identity_type: str, value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("identity value must be non-empty")
    if identity_type == "bin_iin":
        normalized = re.sub(r"\D", "", raw)
        if not re.fullmatch(r"\d{12}", normalized):
            raise ValueError("BIN/IIN must contain exactly 12 digits")
        return normalized
    if identity_type == "domain":
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if not host or "." not in host:
            raise ValueError("domain identity must contain a valid host name")
        return host
    if identity_type == "instagram":
        normalized = raw.casefold().rstrip("/").rsplit("/", maxsplit=1)[-1].lstrip("@")
        if not re.fullmatch(r"[a-z0-9._]{1,30}", normalized):
            raise ValueError("invalid Instagram identity")
        return normalized
    if identity_type in {"2gis_firm_id", "source_record"}:
        return " ".join(raw.casefold().split())
    raise ValueError(f"unsupported strong identity type: {identity_type}")


def normalize_contact(contact_type: str, value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("contact value must be non-empty")
    if contact_type in {"phone", "whatsapp"}:
        return normalize_phone(raw)
    if contact_type == "email":
        normalized = raw.casefold()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("invalid email address")
        return normalized
    if contact_type == "website":
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        if not parsed.hostname:
            raise ValueError("invalid website")
        path = parsed.path.rstrip("/")
        return f"{parsed.hostname.casefold().removeprefix('www.')}{path}"
    if contact_type == "instagram":
        return normalize_supplier_identity("instagram", raw)
    if contact_type == "telegram":
        normalized = raw.casefold().rstrip("/").rsplit("/", maxsplit=1)[-1].lstrip("@")
        if not re.fullmatch(r"[a-z0-9_]{5,32}", normalized):
            raise ValueError("invalid Telegram username")
        return normalized
    raise ValueError(f"unsupported contact_type: {contact_type}")


def location_key(value: SupplierLocationInput) -> str:
    canonical = {
        "country_code": value.country_code,
        "city": _clean(value.city),
        "address_text": _clean(value.address_text),
        "district": _clean(value.district),
        "market_name": _clean(value.market_name),
        "pavilion": _clean(value.pavilion),
        "latitude": str(value.latitude) if value.latitude is not None else None,
        "longitude": str(value.longitude) if value.longitude is not None else None,
        "source_provider": _clean(value.source_provider),
        "provider_place_id": _clean(value.provider_place_id),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def upsert_source_evidence(
    connection: asyncpg.Connection,
    evidence: SourceEvidenceInput,
) -> UpsertResult:
    evidence_id = uuid4()
    row = await connection.fetchrow(
        """
        INSERT INTO source_evidence(
            id,research_query_id,source_type,source_locator,source_url,external_id,
            title,captured_at,content_sha256,snapshot_storage_path,raw_payload,
            idempotency_key
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        evidence_id,
        evidence.research_query_id,
        evidence.source_type,
        evidence.source_locator,
        evidence.source_url,
        evidence.external_id,
        evidence.title,
        evidence.captured_at,
        evidence.content_sha256,
        evidence.snapshot_storage_path,
        dict(evidence.raw_payload),
        evidence.idempotency_key,
    )
    if row is not None:
        return UpsertResult(id=row["id"], created=True)
    existing = await connection.fetchrow(
        "SELECT * FROM source_evidence WHERE idempotency_key=$1",
        evidence.idempotency_key,
    )
    if existing is None:
        raise RuntimeError("source evidence conflict vanished")
    expected = {
        "research_query_id": evidence.research_query_id,
        "source_type": evidence.source_type,
        "source_locator": evidence.source_locator,
        "source_url": evidence.source_url,
        "external_id": evidence.external_id,
        "title": evidence.title,
        "captured_at": evidence.captured_at,
        "content_sha256": evidence.content_sha256,
        "snapshot_storage_path": evidence.snapshot_storage_path,
        "raw_payload": dict(evidence.raw_payload),
    }
    _verify_immutable(existing, expected, "source evidence")
    return UpsertResult(id=existing["id"], created=False)


async def upsert_supplier(
    connection: asyncpg.Connection,
    supplier: SupplierInput,
) -> UpsertResult:
    normalized = normalize_supplier_identity(supplier.identity_type, supplier.identity_value)
    lock_key = f"supplier-identity:{supplier.identity_type}:{normalized}"
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", lock_key
        )
        existing = await connection.fetchrow(
            """
            SELECT s.id
            FROM supplier_aliases AS a
            JOIN suppliers AS s ON s.id=a.supplier_id
            WHERE a.alias_type=$1 AND a.normalized_value=$2 AND a.active
            """,
            supplier.identity_type,
            normalized,
        )
        if existing is not None:
            await connection.execute(
                """
                UPDATE supplier_aliases
                SET last_seen_at=GREATEST(last_seen_at,now()),
                    confidence=GREATEST(confidence,$3)
                WHERE supplier_id=$1 AND alias_type=$2 AND normalized_value=$4
                """,
                existing["id"],
                supplier.identity_type,
                supplier.confidence,
                normalized,
            )
            return UpsertResult(id=existing["id"], created=False)

        supplier_id = uuid4()
        bin_iin = normalized if supplier.identity_type == "bin_iin" else None
        await connection.execute(
            """
            INSERT INTO suppliers(
                id,legal_name,display_name,supplier_type,country_code,bin_iin
            ) VALUES ($1,$2,$3,$4,$5,$6)
            """,
            supplier_id,
            supplier.legal_name,
            supplier.display_name,
            supplier.supplier_type,
            supplier.country_code,
            bin_iin,
        )
        await connection.execute(
            """
            INSERT INTO supplier_aliases(
                supplier_id,alias_type,display_value,normalized_value,
                source_evidence_id,confidence
            ) VALUES ($1,$2,$3,$4,$5,$6)
            """,
            supplier_id,
            supplier.identity_type,
            supplier.identity_value,
            normalized,
            supplier.source_evidence_id,
            supplier.confidence,
        )
        return UpsertResult(id=supplier_id, created=True)


async def upsert_supplier_contact(
    connection: asyncpg.Connection,
    supplier_id: UUID,
    contact: SupplierContactInput,
) -> UpsertResult:
    normalized = normalize_contact(contact.contact_type, contact.display_value)
    lock_key = f"supplier-contact:{contact.contact_type}:{normalized}"
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", lock_key
        )
        existing = await connection.fetchrow(
            """
            SELECT id,supplier_id FROM supplier_contacts
            WHERE normalized_value=$2 AND active AND (
                contact_type=$1 OR (
                    contact_type IN ('phone','whatsapp')
                    AND $1 IN ('phone','whatsapp')
                )
            )
            """,
            contact.contact_type,
            normalized,
        )
        if existing is not None and existing["supplier_id"] != supplier_id:
            raise SupplierIdentityConflictError(
                f"{contact.contact_type} identity belongs to another supplier"
            )
        if existing is not None:
            await connection.execute(
                """
                UPDATE supplier_contacts
                SET display_value=$2,label=$3,
                    is_whatsapp_confirmed=is_whatsapp_confirmed OR $4,
                    source_evidence_id=$5,confidence=GREATEST(confidence,$6),
                    last_seen_at=now()
                WHERE id=$1
                """,
                existing["id"],
                contact.display_value,
                contact.label,
                contact.is_whatsapp_confirmed,
                contact.source_evidence_id,
                contact.confidence,
            )
            return UpsertResult(id=existing["id"], created=False)
        contact_id = uuid4()
        await connection.execute(
            """
            INSERT INTO supplier_contacts(
                id,supplier_id,contact_type,display_value,normalized_value,label,
                is_whatsapp_confirmed,source_evidence_id,confidence
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            contact_id,
            supplier_id,
            contact.contact_type,
            contact.display_value,
            normalized,
            contact.label,
            contact.is_whatsapp_confirmed,
            contact.source_evidence_id,
            contact.confidence,
        )
        return UpsertResult(id=contact_id, created=True)


async def upsert_supplier_location(
    connection: asyncpg.Connection,
    supplier_id: UUID,
    location: SupplierLocationInput,
) -> UpsertResult:
    key = location_key(location)
    identity = (
        f"provider:{_clean(location.source_provider)}:{_clean(location.provider_place_id)}"
        if location.source_provider and location.provider_place_id
        else f"location:{key}"
    )
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", identity
        )
        existing = None
        if location.source_provider and location.provider_place_id:
            existing = await connection.fetchrow(
                """
                SELECT id,supplier_id FROM supplier_locations
                WHERE source_provider=$1 AND provider_place_id=$2 AND active
                """,
                location.source_provider,
                location.provider_place_id,
            )
            if existing is not None and existing["supplier_id"] != supplier_id:
                raise SupplierIdentityConflictError(
                    "provider place identity belongs to another supplier"
                )
        if existing is None:
            existing = await connection.fetchrow(
                """
                SELECT id,supplier_id FROM supplier_locations
                WHERE supplier_id=$1 AND location_key=$2
                """,
                supplier_id,
                key,
            )
        values = (
            location.country_code,
            location.city,
            location.address_text,
            location.district,
            location.market_name,
            location.pavilion,
            location.latitude,
            location.longitude,
            location.source_provider,
            location.provider_place_id,
            location.source_evidence_id,
            location.confidence,
        )
        if existing is not None:
            await connection.execute(
                """
                UPDATE supplier_locations
                SET location_key=$2,country_code=$3,city=$4,address_text=$5,district=$6,
                    market_name=$7,pavilion=$8,latitude=$9,longitude=$10,
                    source_provider=$11,provider_place_id=$12,
                    source_evidence_id=$13,confidence=GREATEST(confidence,$14),
                    last_seen_at=now()
                WHERE id=$1
                """,
                existing["id"],
                key,
                *values,
            )
            return UpsertResult(id=existing["id"], created=False)
        location_id = uuid4()
        await connection.execute(
            """
            INSERT INTO supplier_locations(
                id,supplier_id,location_key,country_code,city,address_text,
                district,market_name,pavilion,latitude,longitude,source_provider,
                provider_place_id,source_evidence_id,confidence
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            """,
            location_id,
            supplier_id,
            key,
            *values,
        )
        return UpsertResult(id=location_id, created=True)


async def upsert_offer(
    connection: asyncpg.Connection,
    offer: OfferInput,
) -> UpsertResult:
    offer_id = uuid4()
    row = await connection.fetchrow(
        """
        INSERT INTO offers(
            id,request_item_id,supplier_id,supplier_location_id,
            supplier_product_name,model_sku,status,idempotency_key
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        offer_id,
        offer.request_item_id,
        offer.supplier_id,
        offer.supplier_location_id,
        offer.supplier_product_name,
        offer.model_sku,
        offer.status,
        offer.idempotency_key,
    )
    if row is not None:
        return UpsertResult(id=row["id"], created=True)
    existing = await connection.fetchrow(
        "SELECT * FROM offers WHERE idempotency_key=$1", offer.idempotency_key
    )
    if existing is None:
        raise RuntimeError("offer conflict vanished")
    _verify_immutable(
        existing,
        {
            "request_item_id": offer.request_item_id,
            "supplier_id": offer.supplier_id,
            "supplier_location_id": offer.supplier_location_id,
            "supplier_product_name": offer.supplier_product_name,
            "model_sku": offer.model_sku,
            "status": offer.status,
        },
        "offer",
    )
    return UpsertResult(id=existing["id"], created=False)


async def upsert_offer_observation(
    connection: asyncpg.Connection,
    observation: OfferObservationInput,
) -> UpsertResult:
    observation_id = uuid4()
    values = _observation_values(observation)
    row = await connection.fetchrow(
        """
        INSERT INTO offer_observations(
            id,offer_id,source_evidence_id,observed_at,observed_by,source_type,
            external_message_id,price_amount,currency,price_unit,pack_qty,moq,
            order_multiple,available_qty,availability_status,vat_status,
            document_status,delivery_status,pickup_point,availability_date,
            evidence_level,raw_payload,idempotency_key
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
            $18,$19,$20,$21,$22::jsonb,$23
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        observation_id,
        *values,
        observation.idempotency_key,
    )
    if row is not None:
        return UpsertResult(id=row["id"], created=True)
    existing = await connection.fetchrow(
        "SELECT * FROM offer_observations WHERE idempotency_key=$1",
        observation.idempotency_key,
    )
    if existing is None:
        raise RuntimeError("offer observation conflict vanished")
    columns = (
        "offer_id",
        "source_evidence_id",
        "observed_at",
        "observed_by",
        "source_type",
        "external_message_id",
        "price_amount",
        "currency",
        "price_unit",
        "pack_qty",
        "moq",
        "order_multiple",
        "available_qty",
        "availability_status",
        "vat_status",
        "document_status",
        "delivery_status",
        "pickup_point",
        "availability_date",
        "evidence_level",
        "raw_payload",
    )
    _verify_immutable(existing, dict(zip(columns, values, strict=True)), "offer observation")
    return UpsertResult(id=existing["id"], created=False)


def _observation_values(value: OfferObservationInput) -> tuple[Any, ...]:
    return (
        value.offer_id,
        value.source_evidence_id,
        value.observed_at,
        value.observed_by,
        value.source_type,
        value.external_message_id,
        value.price_amount,
        value.currency,
        value.price_unit,
        value.pack_qty,
        value.moq,
        value.order_multiple,
        value.available_qty,
        value.availability_status,
        value.vat_status,
        value.document_status,
        value.delivery_status,
        value.pickup_point,
        value.availability_date,
        value.evidence_level,
        dict(value.raw_payload),
    )


def _verify_immutable(
    existing: Mapping[str, Any], expected: Mapping[str, Any], entity: str
) -> None:
    changed = [key for key, value in expected.items() if existing[key] != value]
    if changed:
        fields = ", ".join(sorted(changed))
        raise PersistenceConflictError(
            f"{entity} idempotency key was reused with different fields: {fields}"
        )


def _clean(value: str) -> str:
    return " ".join(value.casefold().split())


def _validate_confidence(value: Decimal) -> None:
    if not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("confidence must be between 0 and 1")
