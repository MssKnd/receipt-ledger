from dataclasses import replace
from pathlib import Path

import pytest

from receipt_ledger.config import AccountRule, MerchantRule, WiseConfig
from receipt_ledger.wise.journal import to_entries, to_entry
from receipt_ledger.wise.parse import parse_file

FIXTURE = Path(__file__).parent / "fixtures" / "wise-sample.csv"


@pytest.fixture
def wise_config(config):
    """実際の帳簿と同じ形 (貸方 預け金/Wise、手数料 支払手数料/Wise) にする。"""
    return replace(
        config,
        accounts=(
            AccountRule("備品・消耗品費", hints=("Office expenses",)),
            AccountRule("旅費交通費", hints=("Travel",)),
        ),
        fallback_account="雑費",
        wise=WiseConfig(
            merchants=(
                MerchantRule(
                    match="Example SaaS",
                    account="支払手数料",
                    sub_account="Vendor",
                    summary="SaaS 月額",
                ),
                MerchantRule(
                    match="Example Contractor",
                    account="業務委託料",
                    sub_account="Example Contractor",
                ),
            )
        ),
    )


@pytest.fixture
def entries(wise_config):
    txns, _ = parse_file(FIXTURE)
    return {e.txn.txn_id: e for e in to_entries(txns, wise_config)}


def test_compound_entry_shape(entries):
    """借方 費用 + 支払手数料 / 貸方 預け金 の 2 行 1 組。"""
    e = entries["CARD_TRANSACTION-1000000001"]
    assert [(d.account, d.sub_account, d.amount_jpy) for d in e.debits] == [
        ("備品・消耗品費", "", 8000),
        ("支払手数料", "Wise", 50),
    ]
    assert (e.credit_account, e.credit_sub_account) == ("預け金", "Wise")
    assert e.credit_amount_jpy == 8050
    assert e.is_balanced()


def test_merchant_rule_overrides_account_and_summary(entries):
    e = entries["CARD_TRANSACTION-1000000002"]
    assert (e.debits[0].account, e.debits[0].sub_account) == ("支払手数料", "Vendor")
    # 摘要は既存の手入力に合わせて店名を書き換えられる
    assert e.summary == "SaaS 月額"
    assert not e.needs_review


def test_merchant_rule_without_summary_keeps_target_name(entries):
    e = entries["TRANSFER-2000000001"]
    assert e.debits[0].account == "業務委託料"
    assert e.summary == "Example Contractor"


def test_shared_whitelist_hit_is_flagged_not_trusted(entries):
    """共通ホワイトリスト由来の一致は科目を提案するが、無印では通さない。

    ヒントは日本語のレジ印字向けなので、英語の取引先名に対しては根拠が弱い。
    無印で確定させると人が preview で誤りを止める設計を迂回する。"""
    e = entries["CARD_TRANSACTION-1000000006"]
    assert e.debits[0].account == "備品・消耗品費"
    assert e.needs_review
    assert "弱い一致" in e.review_notes[0]


def test_weak_hint_collision_does_not_silently_confirm(config):
    """実測の事故: 同じヒント語を 2 科目が持っていると、同点タイが定義順で
    解決されて無関係な科目に化けた。科目はどちらに転んでも要確認にする。"""
    cfg = replace(
        config,
        accounts=(
            AccountRule("新聞図書費", hints=("Shop", "書籍")),
            AccountRule("備品・消耗品費", hints=("Shop", "文房具")),
        ),
        wise=WiseConfig(),
    )
    txns, _ = parse_file(FIXTURE)
    txn = next(t for t in txns if t.merchant == "Example Store")
    entry = to_entry(replace(txn, merchant="Example Shop"), cfg)
    assert entry.needs_review


def test_unknown_merchant_falls_back_and_flags_review(entries):
    e = entries["CARD_TRANSACTION-1000000005"]
    assert e.debits[0].account == "雑費"
    assert e.needs_review
    assert "Unknown Merchant Ltd" in e.review_notes[0]


def test_zero_fee_has_no_fee_line(entries):
    e = entries["CARD_TRANSACTION-1000000006"]
    assert len(e.debits) == 1
    assert e.credit_amount_jpy == 5000
    assert e.is_balanced()


def test_memo_carries_wise_rate_and_id(entries):
    e = entries["CARD_TRANSACTION-1000000001"]
    assert "Wise実レート" in e.memo
    assert "CARD_TRANSACTION-1000000001" in e.memo


def test_longest_merchant_match_wins(wise_config):
    """短い語が長い語を巻き込まないこと。"""
    cfg = replace(
        wise_config,
        wise=replace(
            wise_config.wise,
            merchants=(
                MerchantRule(match="Example", account="雑費"),
                MerchantRule(match="Example Store", account="備品・消耗品費"),
            ),
        ),
    )
    txns, _ = parse_file(FIXTURE)
    txn = next(t for t in txns if t.merchant == "Example Store")
    assert to_entry(txn, cfg).debits[0].account == "備品・消耗品費"


def test_all_entries_balance(entries):
    assert all(e.is_balanced() for e in entries.values())
