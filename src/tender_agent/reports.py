from __future__ import annotations

from .snapshot import ProcurementSnapshot


def render_operator_markdown(snapshot: ProcurementSnapshot) -> str:
    line_by_id = {line.line_id: line for line in snapshot.lines}
    sections: dict[str, list[str]] = {
        "Докупить / допоставить": [],
        "Замена": [],
        "Проверить в школе": [],
        "Закрыто": [],
    }
    for state in snapshot.states:
        line = line_by_id[state.line_id]
        row = (
            f"- №{line.number} — {line.name}: {state.reconciliation_status}; "
            f"доставлено {state.delivered_quantity}/{line.required_quantity} {line.unit}"
        )
        placed = False
        if state.remaining_to_deliver > 0:
            sections["Докупить / допоставить"].append(row)
            placed = True
        if state.replacement_status != "Не требуется / неизвестно":
            sections["Замена"].append(row)
            placed = True
        if state.conflicts or "провер" in state.reconciliation_status.lower() or state.remaining_to_accept > 0:
            sections["Проверить в школе"].append(row)
            placed = True
        if not placed:
            sections["Закрыто"].append(row)

    output = [
        f"# Операционный отчёт — {snapshot.request_id}", "",
        f"Snapshot: `{snapshot.checksum}`", "",
    ]
    for title, rows in sections.items():
        output.extend((f"## {title}", ""))
        output.extend(rows or ["- Нет позиций."])
        output.append("")
    return "\n".join(output).rstrip() + "\n"
