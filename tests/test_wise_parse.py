from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from receipt_ledger.wise.parse import Kind, WiseFormatError, parse_file

FIXTURE = Path(__file__).parent / "fixtures" / "wise-sample.csv"


@pytest.fixture
def parsed():
    return parse_file(FIXTURE)


def test_keeps_only_bookable_rows(parsed):
    txns, _ = parsed
    assert [t.txn_id for t in txns] == [
        "CARD_TRANSACTION-1000000001",
        "CARD_TRANSACTION-1000000002",
        "TRANSFER-2000000001",
        "CARD_TRANSACTION-1000000005",
        "CARD_TRANSACTION-1000000006",
    ]


def test_skip_reasons(parsed):
    _, skipped = parsed
    reasons = {s.txn_id: s.reason for s in skipped}
    # チャージは銀行明細側で計上済み → ここで仕訳にすると二重計上
    assert "チャージは銀行明細側で計上" in reasons["TRANSFER-2000000002"]
    # 0 円の与信取消は仕訳不要と言い切る (毎月並ぶとリストを読み飛ばすため)
    assert "与信取消、仕訳不要" in reasons["CARD_TRANSACTION-1000000003"]
    # 外貨残高からの決済は円額が確定していない
    assert "Source currency=CAD" in reasons["CARD_TRANSACTION-1000000004"]


def _rewrite(tmp_path: Path, old: str, new: str) -> Path:
    src = FIXTURE.read_text(encoding="utf-8")
    assert old in src, f"フィクスチャに {old!r} が無い"
    path = tmp_path / "variant.csv"
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    return path


def test_nonzero_refund_is_not_called_a_void(tmp_path: Path):
    """金額のある返金を「与信取消」と言い切らない。

    返金が REFUNDED で来るかは未確認。「仕訳不要」と読める理由を付けると
    逆仕訳が漏れて預け金残高が合わなくなる。"""
    path = _rewrite(
        tmp_path,
        '"Acme LLC",0,JPY,"Temp Auth",0,JPY,1',
        '"Acme LLC",5000,JPY,"Temp Auth",5000,JPY,1',
    )
    _, skipped = parse_file(path)
    reason = next(s.reason for s in skipped if s.txn_id == "CARD_TRANSACTION-1000000003")
    assert "要判断" in reason
    assert "仕訳不要" not in reason


def test_incoming_card_transaction_is_not_assumed_to_be_a_top_up(tmp_path: Path):
    """IN = 必ずチャージ、と決めつけない (返金が IN で来る可能性が未確認)。"""
    path = _rewrite(
        tmp_path,
        'TRANSFER-2000000002,COMPLETED,IN,"2026-07-27 12:00:00","2026-07-27 12:10:00",0,JPY,,,'
        '"Acme LLC",300000.0,JPY,"Acme LLC",300000.0,JPY,1.0,,,"Tester","Money added",',
        '"CARD_TRANSACTION-1000000007",COMPLETED,IN,"2026-07-27 12:00:00",'
        '"2026-07-27 12:10:00",0,JPY,,,'
        '"Acme LLC",5000,JPY,"Example Store",43.00,CAD,0.0086,,,"Tester",General,',
    )
    _, skipped = parse_file(path)
    reason = next(s.reason for s in skipped if s.txn_id == "CARD_TRANSACTION-1000000007")
    assert "入金の種別が未確認" in reason
    assert "銀行明細側で計上" not in reason


def test_fee_only_row_is_skipped(tmp_path: Path):
    """本体 0 円 + 手数料ありは 0 円の借方行を生むので仕訳にしない。"""
    path = _rewrite(tmp_path, ",8000,JPY,", ",0,JPY,")
    txns, skipped = parse_file(path)
    assert "CARD_TRANSACTION-1000000001" not in {t.txn_id for t in txns}
    reason = next(s.reason for s in skipped if s.txn_id == "CARD_TRANSACTION-1000000001")
    assert "費用本体が 0 円" in reason


def test_negative_amount_is_skipped_not_booked(tmp_path: Path):
    """負値は無警告で負の借方行にせず人に回す。1 行の異常で全体を落とさない。"""
    path = _rewrite(tmp_path, ",8000,JPY,", ",-8000,JPY,")
    txns, skipped = parse_file(path)
    assert "CARD_TRANSACTION-1000000001" not in {t.txn_id for t in txns}
    assert "負の金額" in next(
        s.reason for s in skipped if s.txn_id == "CARD_TRANSACTION-1000000001"
    )
    # 他の行は通常どおり処理される
    assert len(txns) == 4


def test_empty_fee_field_is_zero(tmp_path: Path):
    """円建てで fee 欄が空文字のケース (実データの空 fee 行は CAD で手前で弾かれる)。"""
    path = _rewrite(tmp_path, ",50,JPY,,,\"Acme LLC\",8000,JPY,", ",,,,,\"Acme LLC\",8000,JPY,")
    txns, _ = parse_file(path)
    txn = next(t for t in txns if t.txn_id == "CARD_TRANSACTION-1000000001")
    assert txn.fee_jpy == 0
    assert txn.total_jpy == 8000


def test_skipped_rows_keep_context_for_humans(parsed):
    _, skipped = parsed
    charge = next(s for s in skipped if s.txn_id == "TRANSFER-2000000002")
    assert charge.txn_date == date(2026, 7, 27)
    assert charge.merchant == "Acme LLC"


def test_amounts_and_kind(parsed):
    txns, _ = parsed
    card = txns[0]
    assert card.kind is Kind.CARD
    assert card.txn_date == date(2026, 7, 3)
    assert (card.amount_jpy, card.fee_jpy, card.total_jpy) == (8000, 50, 8050)
    assert card.target_amount == Decimal("70.00")
    assert card.target_currency == "CAD"

    transfer = txns[2]
    assert transfer.kind is Kind.TRANSFER
    # "150000.0" のような小数表記でも円は整数で受ける
    assert (transfer.amount_jpy, transfer.fee_jpy, transfer.total_jpy) == (150000, 900, 150900)


def test_uses_created_on_not_finished_on(parsed):
    txns, _ = parsed
    # Created 07-01 / Finished 07-02。既存の手入力仕訳は起票日で切っている
    assert txns[2].txn_date == date(2026, 7, 1)


def test_zero_fee_is_kept(parsed):
    txns, _ = parsed
    free = next(t for t in txns if t.txn_id == "CARD_TRANSACTION-1000000006")
    assert free.fee_jpy == 0
    assert free.total_jpy == 5000


def test_fx_note_uses_wise_actual_rate(parsed):
    txns, _ = parsed
    # 8000 / 70.00 = 114.29。Frankfurter の推定ではなく Wise の実レート
    assert txns[0].fx_note() == "CAD 70.00 @114.29 (Wise実レート)"


def test_missing_column_is_rejected(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text("ID,Status\nX,COMPLETED\n", encoding="utf-8")
    with pytest.raises(WiseFormatError, match="必要な列がない"):
        parse_file(path)


def test_fractional_yen_is_rejected(tmp_path: Path):
    src = FIXTURE.read_text(encoding="utf-8").replace(",8000,JPY,", ",8000.5,JPY,")
    path = tmp_path / "frac.csv"
    path.write_text(src, encoding="utf-8")
    with pytest.raises(WiseFormatError, match="円未満の端数"):
        parse_file(path)
