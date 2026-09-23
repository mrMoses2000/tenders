from pathlib import Path


def test_supplier_language_schema_is_explicitly_ru_kz() -> None:
    sql = (Path(__file__).parents[1] / "migrations/009_supplier_languages.sql").read_text()

    assert "preferred_language" in sql
    assert "preferred_language IN ('ru','kk')" in sql
    assert "language_code IN ('','ru','kk','unknown')" in sql
