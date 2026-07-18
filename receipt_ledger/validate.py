"""検算と確信度チェック。VL モデルの数字誤読を検出する主防衛線。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .config import Config
from .models import Receipt

# 妥当な取引日の下限 (これ以前は年の誤読とみなす)。
_MIN_YEAR = 2000


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: list[str]  # NG のとき人間向けの理由


def _check_date(receipt: Receipt, today: date) -> list[str]:
    if receipt.date is None:
        return ["取引日を読み取れなかった"]
    try:
        d = datetime.strptime(receipt.date, "%Y-%m-%d").date()
    except ValueError:
        return [f"取引日の形式が不正: {receipt.date!r}"]
    if d > today:
        return [f"取引日が未来: {receipt.date}"]
    if d.year < _MIN_YEAR:
        return [f"取引日の年が不自然 (誤読の疑い): {receipt.date}"]
    return []


def _check_checksum(receipt: Receipt, tolerance: float) -> list[str]:
    """明細合計 ≈ 総額。明細が読めているときだけ検算する。

    明細は税込/税抜どちらの表記もあり得る (外税店では税抜)。そこで
    「合計そのもの」「×1.10」「×1.08」「+ 税額」のいずれかが総額に一致すれば
    OK とし、正しく読めた外税レシートを取りこぼさない。"""
    if not receipt.line_items:
        return []
    subtotal = sum(li.amount for li in receipt.line_items)
    if subtotal <= 0 or receipt.total <= 0:
        return []

    candidates = [subtotal, subtotal * 1.10, subtotal * 1.08]
    tax_sum = sum(b.tax_amount for b in receipt.tax_buckets if b.tax_amount > 0)
    if tax_sum > 0:
        candidates.append(subtotal + tax_sum)
    # 北米の飲食レシートはチップ込みが総額 (subtotal + tax + tip = total)。
    if receipt.tip_amount > 0:
        candidates.extend([c + receipt.tip_amount for c in list(candidates)])

    best = min(abs(c - receipt.total) / receipt.total for c in candidates)
    if best > tolerance:
        return [
            f"明細合計 {subtotal:g} が総額 {receipt.total:g} と乖離 "
            f"(最良 {best:.0%} > 許容 {tolerance:.0%})"
        ]
    return []


def validate_receipt(
    receipt: Receipt, config: Config, today: date | None = None
) -> ValidationResult:
    reasons: list[str] = []

    if receipt.total <= 0:
        reasons.append(f"総額が不正: {receipt.total}")

    reasons.extend(_check_date(receipt, today or date.today()))

    if receipt.merchant.strip().upper() in ("", "UNKNOWN"):
        reasons.append("店名を読み取れなかった")

    if receipt.confidence < config.min_confidence:
        reasons.append(
            f"確信度が低い: {receipt.confidence:.2f} < {config.min_confidence:.2f}"
        )

    reasons.extend(_check_checksum(receipt, config.checksum_tolerance))

    return ValidationResult(ok=not reasons, reasons=reasons)
