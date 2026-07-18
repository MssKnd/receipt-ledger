from datetime import date

from receipt_ledger.models import LineItem, Receipt, TaxBucket
from receipt_ledger.validate import validate_receipt


def _receipt(**kw) -> Receipt:
    base = dict(
        merchant="スターバックス",
        date="2024-05-10",
        total=1000.0,
        confidence=0.9,
    )
    base.update(kw)
    return Receipt(**base)


def test_ok(config):
    assert validate_receipt(_receipt(), config).ok


def test_low_confidence(config):
    r = validate_receipt(_receipt(confidence=0.3), config)
    assert not r.ok
    assert any("確信度" in x for x in r.reasons)


def test_missing_date(config):
    r = validate_receipt(_receipt(date=None), config)
    assert not r.ok


def test_unknown_merchant(config):
    r = validate_receipt(_receipt(merchant="UNKNOWN"), config)
    assert not r.ok


def test_zero_total(config):
    assert not validate_receipt(_receipt(total=0), config).ok


def test_checksum_within_tolerance(config):
    r = _receipt(
        total=1000.0,
        line_items=[LineItem(description="a", amount=600), LineItem(description="b", amount=380)],
    )
    # 980 vs 1000 = 2% 乖離 → 許容 5% 内
    assert validate_receipt(r, config).ok


def test_checksum_exceeds_tolerance(config):
    r = _receipt(
        total=1000.0,
        line_items=[LineItem(description="a", amount=100)],
    )
    res = validate_receipt(r, config)
    assert not res.ok
    assert any("乖離" in x for x in res.reasons)


def test_no_line_items_skips_checksum(config):
    # 明細が無いと検算はスキップ (誤検出防止)
    assert validate_receipt(_receipt(line_items=[]), config).ok


def test_checksum_external_tax_passes(config):
    # 外税表記: 明細は税抜合計 1000、総額は税込 1100。×1.10 が一致するので OK
    r = _receipt(
        total=1100.0,
        line_items=[LineItem(description="a", amount=600), LineItem(description="b", amount=400)],
    )
    assert validate_receipt(r, config).ok


def test_checksum_with_explicit_tax_amount_passes(config):
    # 税抜明細 1000 + 税額バケット 80 = 1080 が総額に一致
    r = _receipt(
        total=1080.0,
        line_items=[LineItem(description="a", amount=1000)],
        tax_buckets=[TaxBucket(rate_percent=8, tax_amount=80, net_amount=1000)],
    )
    assert validate_receipt(r, config).ok


def test_future_date_rejected(config):
    r = validate_receipt(_receipt(date="2024-05-10"), config, today=date(2024, 5, 1))
    assert not r.ok
    assert any("未来" in x for x in r.reasons)


def test_bad_date_format_rejected(config):
    # 2024/05/10 や May 10, 2024 は Receipt 側で ISO に正規化されるようになった。
    # 正規化パターンにも合わない文字列は従来どおり形式不正で弾く。
    r = validate_receipt(_receipt(date="06-11-2026"), config)  # 月日が曖昧
    assert not r.ok
    assert any("形式" in x for x in r.reasons)


def test_ancient_year_rejected(config):
    # 2004→誤読で 0204 のような年は弾く
    r = validate_receipt(_receipt(date="0204-05-10"), config)
    assert not r.ok
    assert any("年が不自然" in x for x in r.reasons)


def test_checksum_with_tip(config):
    # 北米飲食: subtotal 42.49 + tax 3.52 + tip 9.20 = 55.21 (実レシート由来)
    r = _receipt(
        date="2026-02-02",
        total=55.21,
        tip_amount=9.20,
        line_items=[
            LineItem(description="feast", amount=23.0),
            LineItem(description="sampler", amount=3.0),
            LineItem(description="tea", amount=4.5),
            LineItem(description="lager", amount=8.0),
            LineItem(description="doogh", amount=3.99),
        ],
        tax_buckets=[TaxBucket(rate_percent=5, tax_amount=3.52, net_amount=42.49)],
    )
    assert validate_receipt(r, config).ok


def test_checksum_tip_still_rejects_mismatch(config):
    # チップを足しても合わない総額は従来どおり弾く
    r = _receipt(
        total=99.0,
        tip_amount=5.0,
        line_items=[LineItem(description="a", amount=10.0)],
    )
    assert not validate_receipt(r, config).ok
