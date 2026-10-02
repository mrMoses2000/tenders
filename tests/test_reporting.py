from procurement_bot.reporting import (
    ProcurementReport,
    ReportEvidence,
    ReportItem,
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


def test_full_report_shows_all_55_items_and_marks_50_without_offers() -> None:
    report = ProcurementReport(
        case_reference="abc123",
        title="Заявка на 55 позиций",
        city="Алматы",
        search_scope="весь город",
        items=[
            ReportItem(
                item_number=number,
                item_name=f"Позиция {number}",
                quantity=20 if number == 55 else None,
                unit="рулон" if number == 55 else None,
                specification_text="<script>не исполнять</script>" if number == 55 else None,
            )
            for number in range(1, 56)
        ],
        cards=[
            SupplierReportCard(
                item_number=number,
                item_name=f"Позиция {number}",
                supplier_name=f"Поставщик {number}",
                product_name=f"Товар {number}",
                technical_status="нужно подтвердить",
                availability="неизвестно",
            )
            for number in range(1, 6)
        ],
    )

    output = render_report_html(report)

    assert output.count('<section class="item-group">') == 55
    assert output.count("Предложений пока нет.") == 50
    assert "Позиция 55: Позиция 55" in output
    assert "Поставщик 5" in output
    assert "Требуется: 20 рулон" in output
    assert "&lt;script&gt;не исполнять&lt;/script&gt;" in output
    assert "<script>не исполнять</script>" not in output
