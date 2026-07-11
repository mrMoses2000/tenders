from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_TIMEOUT = 10.0


class ExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractedProduct:
    source_url: str
    title: str = ""
    product_name: str = ""
    description: str = ""
    sku: str = ""
    brand: str = ""
    image_url: str = ""
    price: str = ""
    currency: str = ""
    availability_signal: str = "unknown"
    stock_confirmed: bool = False
    evidence_level: str = "2 — Публичный лид"
    extraction_method: str = "meta"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _ProductHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.json_ld: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self._script_parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): (value or "") for key, value in attrs}
        tag = tag.casefold()
        if tag in {"style", "noscript", "svg"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = attributes.get("property") or attributes.get("name") or attributes.get("itemprop")
            content = attributes.get("content", "").strip()
            if key and content:
                self.meta[key.casefold()] = content
        elif tag == "script" and "ld+json" in attributes.get("type", "").casefold():
            self._in_json_ld = True
            self._script_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._script_parts))
            self._in_json_ld = False
            self._script_parts = []
        if tag in {"style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._script_parts.append(data)
        elif self._in_title:
            self.title_parts.append(data)
        elif not self._ignored_depth:
            cleaned = " ".join(data.split())
            if cleaned:
                self.text_parts.append(cleaned)


def _iter_json_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_nodes(child)


def _type_contains_product(value: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    return any(str(item).casefold().endswith("product") for item in values)


def _first_offer(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("url") or "")
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    return str(value).strip()


def _availability(value: Any) -> str:
    token = _text(value).casefold().rsplit("/", 1)[-1]
    mapping = {
        "instock": "in_stock",
        "outofstock": "out_of_stock",
        "preorder": "preorder",
        "backorder": "backorder",
        "limitedavailability": "limited",
    }
    return mapping.get(token, "unknown")


def _absolute_url(value: str, source_url: str) -> str:
    if not value:
        return ""
    return urljoin(source_url, value) if source_url else value


def parse_product_html(html: str, source_url: str = "") -> ExtractedProduct:
    parser = _ProductHTMLParser()
    parser.feed(html)

    products: list[dict[str, Any]] = []
    for raw in parser.json_ld:
        try:
            decoded = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        products.extend(node for node in _iter_json_nodes(decoded) if _type_contains_product(node.get("@type")))

    title = " ".join(" ".join(parser.title_parts).split())
    if products:
        product = products[0]
        offer = _first_offer(product.get("offers"))
        image = product.get("image")
        return ExtractedProduct(
            source_url=source_url,
            title=title,
            product_name=_text(product.get("name")) or parser.meta.get("og:title", title),
            description=_text(product.get("description")) or parser.meta.get("og:description", ""),
            sku=_text(product.get("sku") or product.get("mpn")),
            brand=_text(product.get("brand")),
            image_url=_absolute_url(_text(image) or parser.meta.get("og:image", ""), source_url),
            price=_text(offer.get("price") or offer.get("lowPrice")),
            currency=_text(offer.get("priceCurrency")),
            availability_signal=_availability(offer.get("availability")),
            stock_confirmed=False,
            evidence_level="3 — Карточка проверена",
            extraction_method="json-ld",
        )

    meta = parser.meta
    return ExtractedProduct(
        source_url=source_url,
        title=title,
        product_name=meta.get("og:title") or meta.get("twitter:title") or title,
        description=meta.get("og:description") or meta.get("description", ""),
        image_url=_absolute_url(meta.get("og:image") or meta.get("twitter:image", ""), source_url),
        price=meta.get("product:price:amount") or meta.get("og:price:amount", ""),
        currency=meta.get("product:price:currency") or meta.get("og:price:currency", ""),
        availability_signal="unknown",
        stock_confirmed=False,
        evidence_level="2 — Публичный лид",
        extraction_method="meta",
    )


def _is_forbidden_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not ip.is_global


def validate_public_url(url: str, *, resolve_dns: bool = True) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ExtractionError("Only public http/https URLs are allowed")
    if parsed.username or parsed.password:
        raise ExtractionError("Credentials in URLs are not allowed")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        raise ExtractionError("Local hosts are not allowed")
    try:
        if _is_forbidden_ip(host):
            raise ExtractionError("Private or special-purpose IP addresses are not allowed")
        return url
    except ValueError:
        pass
    if resolve_dns:
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = {item[4][0] for item in socket.getaddrinfo(host, port)}
        except socket.gaierror as exc:
            raise ExtractionError(f"Cannot resolve host: {host}") from exc
        if not addresses or any(_is_forbidden_ip(address) for address in addresses):
            raise ExtractionError("Host resolves to a private or special-purpose address")
    return url


class _SafeRedirectHandler(HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request:
        target = urljoin(req.full_url, newurl)
        validate_public_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_public_html(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    retries: int = 1,
) -> tuple[str, str]:
    validate_public_url(url)
    if timeout <= 0 or timeout > 30:
        raise ValueError("timeout must be between 0 and 30 seconds")
    if max_bytes <= 0 or max_bytes > 10_000_000:
        raise ValueError("max_bytes must be between 1 and 10,000,000")
    if retries < 0 or retries > 2:
        raise ValueError("retries must be between 0 and 2")
    request = Request(
        url,
        headers={
            "User-Agent": "TenderResearchBot/0.1 (+manual procurement research)",
            "Accept": "text/html,application/xhtml+xml;q=0.9",
        },
    )
    opener = build_opener(_SafeRedirectHandler())
    retryable_http = {429, 500, 502, 503, 504}
    for attempt in range(retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                final_url = response.geturl()
                validate_public_url(final_url)
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise ExtractionError(f"Unsupported content type: {content_type}")
                declared = response.headers.get("Content-Length")
                try:
                    too_large = bool(declared and int(declared) > max_bytes)
                except ValueError as exc:
                    raise ExtractionError("Invalid Content-Length header") from exc
                if too_large:
                    raise ExtractionError("Response is larger than max_bytes")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise ExtractionError("Response exceeded max_bytes")
                charset = response.headers.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace"), final_url
        except ExtractionError:
            raise
        except HTTPError as exc:
            if attempt < retries and exc.code in retryable_http:
                time.sleep(RetryBackoff.seconds(attempt))
                continue
            raise ExtractionError(f"Failed to fetch public page: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                time.sleep(RetryBackoff.seconds(attempt))
                continue
            raise ExtractionError(f"Failed to fetch public page: {exc}") from exc
    raise AssertionError("unreachable")


class RetryBackoff:
    """Small bounded retry delay kept separate for deterministic tests."""

    @staticmethod
    def seconds(attempt: int) -> float:
        return min(0.25 * (2**attempt), 1.0)


def extract_public_product(url: str, **fetch_options: Any) -> ExtractedProduct:
    html, final_url = fetch_public_html(url, **fetch_options)
    return parse_product_html(html, final_url)
