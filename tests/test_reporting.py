from procurement_bot.reporting import (
    ProcurementReport,
    ReportEvidence,
    SupplierReportCard,
    render_report_html,
)


def test_report_escapes_untrusted_supplier_content_and_blocks_script_sources() -> None:
    report = ProcurementReport(
        case_reference="abc123",
        title="Марля",
        city="Алматы",
        search_scope="Первая Алматы",
        suppliers_found=1,
        cards=[
            SupplierReportCard(
                supplier_name="<script>alert(1)</script>",
                product_name="Марля",
                technical_status="нужно подтвердить",
                availability="неизвестно",
                evidence=[
                    ReportEvidence(label="карточка", url="javascript:alert(1)"),
                ],
            )
        ],
    )
    output = render_report_html(report)
    assert "<script>" not in output
    assert "&lt;script&gt;" in output
    assert "javascript:" not in output
    assert "default-src 'none'" in output


def test_ranked_cards_render_first() -> None:
    base = dict(
        product_name="Марля",
        technical_status="совпадает",
        availability="в наличии",
    )
    report = ProcurementReport(
        case_reference="abc123",
        title="Марля",
        city="Алматы",
        search_scope="весь город",
        cards=[
            SupplierReportCard(supplier_name="Без ранга", **base),
            SupplierReportCard(supplier_name="Лучший", rank=1, **base),
        ],
    )
    output = render_report_html(report)
    assert output.index("Лучший") < output.index("Без ранга")
