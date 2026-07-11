from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from tender_agent.extractor import ExtractionError, fetch_public_html, parse_product_html, validate_public_url


FIXTURES = Path(__file__).parent / "fixtures"


class _Headers:
    def __init__(self, content_type: str, length: str | None = None) -> None:
        self.content_type = content_type
        self.length = length

    def get_content_type(self) -> str:
        return self.content_type

    def get_content_charset(self) -> str:
        return "utf-8"

    def get(self, name: str) -> str | None:
        return self.length if name == "Content-Length" else None


class _Response:
    def __init__(self, payload: bytes, content_type: str = "text/html", length: str | None = None) -> None:
        self.payload = payload
        self.headers = _Headers(content_type, length)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def geturl(self) -> str:
        return "https://example.com/item"

    def read(self, size: int) -> bytes:
        return self.payload[:size]


class ExtractorTests(unittest.TestCase):
    def test_json_ld_product(self) -> None:
        result = parse_product_html(
            (FIXTURES / "product_jsonld.html").read_text(encoding="utf-8"),
            "https://shop.example/p139",
        )
        self.assertEqual(result.product_name, "Корзина P139 9 л")
        self.assertEqual(result.sku, "P139")
        self.assertEqual(result.price, "800")
        self.assertEqual(result.availability_signal, "in_stock")
        self.assertFalse(result.stock_confirmed)
        self.assertEqual(result.extraction_method, "json-ld")

    def test_meta_fallback_does_not_infer_stock_from_body_text(self) -> None:
        result = parse_product_html(
            (FIXTURES / "product_og.html").read_text(encoding="utf-8"),
            "https://shop.example/cement",
        )
        self.assertEqual(result.price, "2290")
        self.assertEqual(result.image_url, "https://shop.example/cement.jpg")
        self.assertEqual(result.availability_signal, "unknown")
        self.assertFalse(result.stock_confirmed)

    def test_private_urls_are_rejected_without_dns(self) -> None:
        for url in ("http://127.0.0.1/a", "http://10.0.0.1/a", "file:///etc/passwd", "http://localhost/a"):
            with self.subTest(url=url), self.assertRaises(ExtractionError):
                validate_public_url(url, resolve_dns=False)

    def test_public_literal_ip_is_allowed_without_dns(self) -> None:
        self.assertEqual(
            validate_public_url("https://8.8.8.8/page", resolve_dns=False),
            "https://8.8.8.8/page",
        )

    @patch("tender_agent.extractor.validate_public_url", side_effect=lambda value: value)
    @patch("tender_agent.extractor.build_opener")
    def test_non_html_response_is_rejected(self, build: Mock, _validate: Mock) -> None:
        build.return_value.open.return_value = _Response(b"{}", "application/json")
        with self.assertRaisesRegex(ExtractionError, "Unsupported content type"):
            fetch_public_html("https://example.com/item", retries=0)

    @patch("tender_agent.extractor.validate_public_url", side_effect=lambda value: value)
    @patch("tender_agent.extractor.build_opener")
    def test_oversize_response_is_rejected(self, build: Mock, _validate: Mock) -> None:
        build.return_value.open.return_value = _Response(b"x" * 11, length="11")
        with self.assertRaisesRegex(ExtractionError, "larger than max_bytes"):
            fetch_public_html("https://example.com/item", max_bytes=10, retries=0)

    @patch("tender_agent.extractor.time.sleep")
    @patch("tender_agent.extractor.validate_public_url", side_effect=lambda value: value)
    @patch("tender_agent.extractor.build_opener")
    def test_transient_http_error_is_retried(
        self, build: Mock, _validate: Mock, sleep: Mock
    ) -> None:
        error = HTTPError("https://example.com/item", 503, "busy", {}, None)
        try:
            build.return_value.open.side_effect = [error, _Response(b"<title>OK</title>")]
            html, _ = fetch_public_html("https://example.com/item", retries=1)
            self.assertIn("OK", html)
            self.assertEqual(build.return_value.open.call_count, 2)
            sleep.assert_called_once()
        finally:
            error.close()


if __name__ == "__main__":
    unittest.main()
