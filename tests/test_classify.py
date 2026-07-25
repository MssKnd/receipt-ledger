from receipt_ledger.classify import (
    TAX_8_REDUCED,
    TAX_10,
    TAX_EXEMPT,
    classify,
)
from receipt_ledger.models import Currency, Receipt, TaxBucket


def _receipt(**kw) -> Receipt:
    base = dict(merchant="店", date="2024-05-10", total=1000.0, confidence=0.9)
    base.update(kw)
    return Receipt(**base)


def test_account_by_hint(config):
    c = classify(
        _receipt(merchant="スターバックス渋谷", tax_buckets=[TaxBucket(rate_percent=10, tax_amount=90, net_amount=910)]),
        config,
    )
    assert c.debit_account == "会議費"
    assert not c.needs_review


def test_account_by_category_hint(config):
    c = classify(_receipt(merchant="謎の店", category_hint="書籍"), config)
    assert c.debit_account == "新聞図書費"


def test_account_fallback(config):
    c = classify(_receipt(merchant="判別不能な店", category_hint="謎"), config)
    assert c.debit_account == "雑費"
    assert c.needs_review


def test_tax_foreign_is_exempt(config):
    # 店名がヒットして科目が決まるケースにして、税区分だけを見る
    c = classify(_receipt(merchant="スターバックス", currency=Currency.CAD, tax_buckets=[]), config)
    assert c.tax_category == TAX_EXEMPT
    assert not c.needs_review  # 国外は税でも科目でも要確認にならない


def test_tax_domestic_10(config):
    c = classify(
        _receipt(currency=Currency.JPY, tax_buckets=[TaxBucket(rate_percent=10, tax_amount=90, net_amount=910)]),
        config,
    )
    assert c.tax_category == TAX_10


def test_tax_domestic_8(config):
    c = classify(
        _receipt(merchant="スーパー", tax_buckets=[TaxBucket(rate_percent=8, tax_amount=74, net_amount=926)]),
        config,
    )
    assert c.tax_category == TAX_8_REDUCED


def test_tax_missing_assumes_10_but_review(config):
    c = classify(_receipt(tax_buckets=[]), config)
    assert c.tax_category == TAX_10
    assert c.needs_review


def test_tax_mixed_needs_review(config):
    c = classify(
        _receipt(
            tax_buckets=[
                TaxBucket(rate_percent=10, tax_amount=50, net_amount=500),
                TaxBucket(rate_percent=8, tax_amount=37, net_amount=463),
            ]
        ),
        config,
    )
    assert c.needs_review


def test_hint_match_ignores_case(config):
    # レジ印字は大文字が多い ("RAMEN GOJIRO" 実測)。ヒント "Ramen" で拾える
    c = classify(_receipt(merchant="RAMEN GOJIRO", currency=Currency.CAD), config)
    assert c.debit_account == "会議費"
    assert not c.needs_review


def test_hint_match_ignores_width(config):
    # 全角英字も NFKC 正規化でマッチする
    c = classify(_receipt(merchant="ＲＡＭＥＮ ＧＯＪＩＲＯ"), config)
    assert c.debit_account == "会議費"


def test_line_items_do_not_override_category_hint():
    # 実測: DAISO の明細 "Grill Grate" が飲食ヒント "Grill" に命中して
    # 会議費に化けた。店名+category_hint で決まるなら明細は見ない
    from receipt_ledger.config import AccountRule, Config
    from receipt_ledger.models import LineItem

    config2 = Config(
        directories=None,
        accounts=(
            AccountRule("会議費", hints=("食事", "Grill")),
            AccountRule("備品・消耗品費", hints=("消耗品",)),
        ),
        fallback_account="雑費",
        fx_cache_path=None,
        min_confidence=0.55,
        checksum_tolerance=0.05,
    )
    c = classify(
        _receipt(
            merchant="DAISO",
            currency=Currency.CAD,
            category_hint="雑貨・消耗品",
            line_items=[LineItem(description="Grill Grate That Prevent", amount=6.5)],
        ),
        config2,
    )
    assert c.debit_account == "備品・消耗品費"


def test_line_items_used_when_name_and_hint_give_nothing(config):
    from receipt_ledger.models import LineItem

    c = classify(
        _receipt(
            merchant="無名の店",
            currency=Currency.CAD,
            line_items=[LineItem(description="コーヒー", amount=500)],
        ),
        config,
    )
    assert c.debit_account == "会議費"
