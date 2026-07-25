"""抽出スキーマと日付正規化のテスト。

Ollama の structured output は required でないフィールドを省略できるため、
extraction_json_schema は全プロパティを required にする (実測で date と
confidence が常に欠落した対策)。日付はモデルが和式で返しがちなので
Receipt 側で ISO に正規化する。
"""

import pytest

from receipt_ledger.models import Receipt, extraction_json_schema


def _objects(node):
    """schema 中の全 object ノードを列挙する。"""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield node
        for v in node.values():
            yield from _objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _objects(v)


def test_schema_requires_all_properties():
    schema = extraction_json_schema()
    objs = list(_objects(schema))
    assert objs, "schema に object が見つからない"
    for obj in objs:
        assert sorted(obj.get("required", [])) == sorted(obj["properties"].keys())


def test_schema_keeps_date_nullable():
    schema = extraction_json_schema()
    receipt = schema["$defs"]["Receipt"]["properties"]["date"]
    # Optional[str] は anyOf [string, null] のまま (読めなければ null を許す)
    types = {alt.get("type") for alt in receipt.get("anyOf", [])}
    assert "null" in types


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026年07月15日 12:34", "2026-07-15"),
        ("2026年7月5日", "2026-07-05"),
        ("2026/7/15", "2026-07-15"),
        ("2026.07.15", "2026-07-15"),
        ("2026-07-15", "2026-07-15"),
        ("不明", "不明"),  # パターン外は素通し (validate で弾く)
    ],
)
def test_date_normalization(raw, expected):
    r = Receipt(merchant="m", date=raw, total=100.0, confidence=0.9)
    assert r.date == expected


def test_date_none_passthrough():
    r = Receipt(merchant="m", date=None, total=100.0, confidence=0.9)
    assert r.date is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("01-Feb-2026", "2026-02-01"),
        ("1 Feb 2026", "2026-02-01"),
        ("Feb 10,2026", "2026-02-10"),
        ("Feb 10, 2026", "2026-02-10"),
        ("May-04-2026", "2026-05-04"),
        ("25-12-2026", "2026-12-25"),  # 13 以上がある → 日と確定できる
        ("12-25-2026", "2026-12-25"),
        ("06-11-2026", "06-11-2026"),  # 月日が曖昧 → 触らない (validate で弾く)
    ],
)
def test_date_normalization_english(raw, expected):
    r = Receipt(merchant="m", date=raw, total=100.0, confidence=0.9)
    assert r.date == expected


def test_ambiguous_date_resolved_as_mdy_for_foreign_currency():
    # 北米レジ印字 (MM-DD-YYYY)。外貨レシートに限り月-日と解釈する
    from receipt_ledger.models import Currency

    r = Receipt(merchant="m", date="06-11-2026", currency=Currency.CAD, total=10.0, confidence=0.9)
    assert r.date == "2026-06-11"


def test_ambiguous_date_untouched_for_jpy():
    r = Receipt(merchant="m", date="06-11-2026", total=100.0, confidence=0.9)
    assert r.date == "06-11-2026"  # validate 側で形式不正として弾かれる


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jun 28, 26 12:38 pm", "2026-06-28"),  # 2桁年 + 時刻付き
        ("Feb 07, 26 11:08 AM", "2026-02-07"),
        ("28 Jun 26", "2026-06-28"),
    ],
)
def test_date_two_digit_year(raw, expected):
    r = Receipt(merchant="m", date=raw, total=100.0, confidence=0.9)
    assert r.date == expected


@pytest.fixture
def fixed_today(monkeypatch):
    """2 桁年の判別 (_plausible_year) は今年に依存するので固定する。"""
    from datetime import date as real_date

    import receipt_ledger.models as models

    class _FixedDate(real_date):
        @classmethod
        def today(cls):
            return real_date(2026, 7, 25)

    monkeypatch.setattr(models, "date", _FixedDate)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("26-05-29", "2026-05-29"),  # YY-MM-DD (Cactus Club レジ印字の実例)
        ("DATE 26-05-29 TIME 6:33PM", "2026-05-29"),
        ("13-05-26", "2026-05-13"),  # DD-MM-YY (日 > 12 で月日確定)
        ("27-05-26", "2026-05-27"),
        ("05-29-26", "2026-05-29"),  # MM-DD-YY
        ("26/05/29", "2026-05-29"),
        # 両端とも年に見える (26 と 25) → 誤読リスクを取らず素通し
        ("26-05-25", "26-05-25"),
    ],
)
def test_date_two_digit_year_numeric(fixed_today, raw, expected):
    r = Receipt(merchant="m", date=raw, total=100.0, confidence=0.9)
    assert r.date == expected


def test_two_digit_year_ambiguous_month_day_resolved_by_currency(fixed_today):
    # 05-06-26: 年は末尾と判るが月日が曖昧 → 4 桁年に展開して通貨で解決
    from receipt_ledger.models import Currency

    cad = Receipt(merchant="m", date="05-06-26", currency=Currency.CAD, total=10.0, confidence=0.9)
    assert cad.date == "2026-05-06"  # 北米式 MM-DD
    jpy = Receipt(merchant="m", date="05-06-26", total=100.0, confidence=0.9)
    assert jpy.date == "05-06-2026"  # 判断材料なし → validate で弾かれる形のまま


def test_currency_corrected_by_gst_label():
    # 日系海外店 (店名日本語) の JPY 誤判定を GST ラベルで補正
    from receipt_ledger.models import Currency

    r = Receipt(
        merchant="Fujiya",
        date="09/08/2025",
        total=10.45,
        confidence=0.9,
        tax_labels=["GST"],
    )
    assert r.currency == Currency.CAD
    assert r._currency_note is not None
    # 補正が日付解決より先に効く → 北米式 MM-DD-YYYY と解釈される
    assert r.date == "2025-09-08"


def test_currency_corrected_by_canadian_address():
    from receipt_ledger.models import Currency

    r = Receipt(
        merchant="Fujiya",
        date="2025-09-08",
        total=10.45,
        confidence=0.9,
        address="912 Clark Dr, Vancouver, BC V5L 3J8",
    )
    assert r.currency == Currency.CAD


def test_currency_not_corrected_for_plain_jpy():
    from receipt_ledger.models import Currency

    r = Receipt(
        merchant="スターバックス",
        date="2026-07-15",
        total=1100.0,
        confidence=0.9,
        address="東京都渋谷区1-2-3",
        tax_labels=["消費税"],
    )
    assert r.currency == Currency.JPY
    assert r._currency_note is None


@pytest.mark.parametrize(("raw", "expected"), [(0.9, 0.9), (8.0, 0.8), (85.0, 0.85), (1.0, 1.0), (0.0, 0.0)])
def test_confidence_scale_normalization(raw, expected):
    r = Receipt(merchant="m", date="2026-07-15", total=100.0, confidence=raw)
    assert r.confidence == expected


def test_category_hint_schema_is_enum():
    # 自由記述だと語彙が安定しない実測 → スキーマの enum で生成側に強制する
    schema = extraction_json_schema()
    hint = schema["$defs"]["Receipt"]["properties"]["category_hint"]
    alts = hint.get("anyOf", [])
    enums = [alt for alt in alts if "enum" in alt or alt.get("const")]
    assert enums, f"category_hint に enum が無い: {hint}"
    assert "食事" in (enums[0].get("enum") or [enums[0].get("const")])


def test_category_hint_out_of_vocabulary_coerced_to_none():
    # モデルが enum 制約を外しても抽出全体は失敗させず、分類フォールバックに委ねる
    r = Receipt(merchant="m", date="2026-07-15", total=100.0, confidence=0.9, category_hint="謎の語")
    assert r.category_hint is None


def test_category_hint_in_vocabulary_kept():
    r = Receipt(merchant="m", date="2026-07-15", total=100.0, confidence=0.9, category_hint="食事")
    assert r.category_hint == "食事"
