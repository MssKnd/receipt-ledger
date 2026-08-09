"""Wise の全取引エクスポート (transaction-history.csv) の読み取り。

このモジュールの仕事は「仕訳にできる行」と「できない行」を分けることだけで、
勘定科目の判断はしない (journal.py の担当)。

仕訳にできない行は捨てずに Skipped として返し、呼び出し側が要手動リストとして
人に見せる。レシート経路が failed/ ディレクトリで表現しているものの CSV 版。

実データで確認した前提 (2026-08 取得のエクスポート):

- カード決済の Source currency は全て JPY。円残高から決済時に都度両替する運用な
  ので、円額は Wise 側で確定済み。**Frankfurter による換算は不要**で、外貨残高を
  持たない以上、為替差損益も発生しない。
- 手数料は Source fee amount に別建てで載り、Source amount (after fees) には
  含まれない。Wise 残高から出ていく総額は両者の和。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

# 使用する列。実物のヘッダに一致。Wise がエクスポート仕様を変えたら黙って
# 誤った仕訳を作るのではなく、読み込み時点で落としたい。
REQUIRED_COLUMNS = (
    "ID",
    "Status",
    "Direction",
    "Created on",
    "Source fee amount",
    "Source amount (after fees)",
    "Source currency",
    "Target name",
    "Target amount (after fees)",
    "Target currency",
    "Exchange rate",
    "Category",
)

# 帳簿の通貨。これ以外の残高からの決済は円額が確定しないので仕訳にしない。
BOOKKEEPING_CURRENCY = "JPY"

# 仕訳の対象にする Status / Direction。
_STATUS_COMPLETED = "COMPLETED"
_STATUS_REFUNDED = "REFUNDED"
_DIRECTION_OUT = "OUT"

# 残高へのチャージ。Wise がこの Category 名で出す。
_CATEGORY_TOP_UP = "Money added"


class Kind(str, Enum):
    CARD = "CARD_TRANSACTION"
    TRANSFER = "TRANSFER"
    OTHER = "OTHER"


@dataclass(frozen=True)
class WiseTxn:
    """仕訳にできる 1 取引。金額は全て円 (Wise が確定させた実額)。"""

    txn_id: str
    kind: Kind
    txn_date: date
    amount_jpy: int  # Source amount (after fees) = 費用の本体
    fee_jpy: int  # Source fee amount = Wise の支払手数料
    merchant: str  # Target name
    category: str  # Wise の Category。弱いヒントとしてのみ使う
    target_amount: Decimal
    target_currency: str

    @property
    def total_jpy(self) -> int:
        """Wise 残高から出ていく総額 = 貸方 (預け金) の金額。"""
        return self.amount_jpy + self.fee_jpy

    def fx_note(self) -> str:
        """換算根拠。推定レートではなく Wise が実際に適用したレート。"""
        if not self.target_amount or self.target_currency == BOOKKEEPING_CURRENCY:
            return ""
        rate = (Decimal(self.amount_jpy) / self.target_amount).quantize(Decimal("0.01"))
        return f"{self.target_currency} {self.target_amount} @{rate} (Wise実レート)"


@dataclass(frozen=True)
class Skipped:
    """仕訳にしなかった行。人が見て手で処理するためのもの。"""

    txn_id: str
    txn_date: date | None
    merchant: str
    reason: str


class WiseFormatError(Exception):
    """エクスポートの形式が想定と違う。"""


def _kind(txn_id: str) -> Kind:
    prefix = txn_id.split("-", 1)[0]
    try:
        return Kind(prefix)
    except ValueError:
        return Kind.OTHER


def _parse_date(value: str) -> date | None:
    """"2026-08-01 14:45:37" → date。

    取引日は Created on を使う。送金は着金 (Finished on) が翌日にずれることが
    あるが、既存の手入力仕訳は起票日で切っているのでそれに合わせる。
    """
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _jpy(value: str, *, field: str, txn_id: str) -> int:
    """円建て金額を整数にする。"150000.0" のような小数表記で来る。"""
    if not value.strip():
        return 0
    try:
        amount = Decimal(value.strip())
    except InvalidOperation as exc:
        raise WiseFormatError(f"{txn_id}: {field} を数値として読めない: {value!r}") from exc
    if amount != amount.to_integral_value():
        raise WiseFormatError(f"{txn_id}: {field} が円未満の端数を持つ: {value!r}")
    return int(amount)


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value.strip() or "0")
    except InvalidOperation:
        return Decimal(0)


def _is_zero_amount(row: dict) -> bool:
    """通貨に関わらず「金額の無い行」か。与信取消の判定に使う。

    Source currency の検査より手前で呼ぶので、円建てを前提にできない
    (CAD の "33.25" を _jpy に通すと端数エラーになってしまう)。
    """
    return not _decimal(row["Source amount (after fees)"]) and not _decimal(
        row["Source fee amount"]
    )


def parse_file(path: Path) -> tuple[list[WiseTxn], list[Skipped]]:
    """Wise の CSV を (仕訳にする取引, 仕訳にしない行) に分ける。

    行の順序はエクスポートのまま (新しい順) 保つ。並べ替えは呼び出し側の判断。
    """
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise WiseFormatError(f"Wise CSV に必要な列がない: {', '.join(missing)}")
        rows = list(reader)

    txns: list[WiseTxn] = []
    skipped: list[Skipped] = []

    for row in rows:
        txn_id = (row["ID"] or "").strip()
        merchant = (row["Target name"] or "").strip()
        txn_date = _parse_date(row["Created on"] or "")

        def skip(reason: str) -> None:
            skipped.append(Skipped(txn_id, txn_date, merchant, reason))

        if txn_date is None:
            skip(f"Created on を日付として読めない: {row['Created on']!r}")
            continue

        status = (row["Status"] or "").strip().upper()
        if status != _STATUS_COMPLETED:
            # 金額 0 の REFUNDED は与信取消で、実際に手を動かすことは無い。
            # 毎月これが「要手動確認」に並ぶとリスト自体を読み飛ばす習慣がつき、
            # 金額のある本物の返金が紛れたときに見逃すので出し分ける。
            if status == _STATUS_REFUNDED and _is_zero_amount(row):
                skip("Status=REFUNDED / 金額 0 (与信取消、仕訳不要)")
            else:
                skip(f"Status={status} (返金の可能性、要判断)")
            continue

        category = (row["Category"] or "").strip()
        direction = (row["Direction"] or "").strip().upper()
        if direction != _DIRECTION_OUT:
            # チャージ (Money added) は銀行明細側で計上済みなので、ここで
            # 仕訳にすると二重計上になる。
            #
            # ただし「入金 = 必ずチャージ」と決めつけない。金額のある返金が
            # COMPLETED/IN のカード取引として来るかは未確認で、もし来た場合に
            # 「計上済みなので対応不要」と読める理由を付けると、返金の逆仕訳が
            # 漏れて預け金残高が合わなくなる。
            if category == _CATEGORY_TOP_UP or _kind(txn_id) is Kind.TRANSFER:
                skip(f"Direction={direction} (チャージは銀行明細側で計上)")
            else:
                skip(f"Direction={direction} Category={category} (入金の種別が未確認、要判断)")
            continue

        source_currency = (row["Source currency"] or "").strip().upper()
        if source_currency != BOOKKEEPING_CURRENCY:
            # 外貨残高からの決済。円額が Wise 側で確定していないので機械的に
            # 仕訳にできない。
            skip(f"Source currency={source_currency} (円残高以外からの決済)")
            continue

        amount = _jpy(row["Source amount (after fees)"], field="Source amount", txn_id=txn_id)
        fee = _jpy(row["Source fee amount"], field="Source fee amount", txn_id=txn_id)
        if amount < 0 or fee < 0:
            # Wise が返金を負値の OUT で表すかは未確認。負の借方行を無警告で
            # 作るより、人に判断させる。ここで例外にしないのは、1 行の異常で
            # ファイル全体の preview を失わせないため (列の変化はファイル単位で
            # 落とすが、行単位の異常は要手動リストに回す)。
            skip(f"負の金額 (本体 {amount} / 手数料 {fee})")
            continue
        if amount == 0:
            # 手数料だけの行は 0 円の費用借方行を生むので仕訳にしない。
            skip(f"費用本体が 0 円 (手数料 {fee})" if fee else "金額 0")
            continue

        txns.append(
            WiseTxn(
                txn_id=txn_id,
                kind=_kind(txn_id),
                txn_date=txn_date,
                amount_jpy=amount,
                fee_jpy=fee,
                merchant=merchant,
                category=(row["Category"] or "").strip(),
                target_amount=_decimal(row["Target amount (after fees)"]),
                target_currency=(row["Target currency"] or "").strip().upper(),
            )
        )

    return txns, skipped
