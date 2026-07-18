"""借方勘定科目の推定と税区分の決定。

- 勘定科目: 設定のホワイトリストからのみ選ぶ。ヒント語のマッチ数が最大の科目を
  採用し、どれもマッチしなければフォールバック科目 (雑費) に落として要確認とする。
- 税区分: 国外 (外貨) は「対象外」。国内は税率表記から「課仕 10%」/
  「課仕(軽)8%」を決める。混在や不明は要確認。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import Currency, Receipt

# MF の税区分名。勘定科目一覧エクスポートの表記 (課仕 10% / 対象外) に一致
# させた。軽減 8% は科目一覧に現れないため MF 標準の表記を採用 — 初回イン
# ポートで通ることを要確認。なお仕訳帳エクスポートでは税区分は全行空
# (= 科目デフォルト) だったので、空で出して MF に委ねる選択肢もある。
TAX_EXEMPT = "対象外"
TAX_10 = "課仕 10%"
TAX_8_REDUCED = "課仕(軽)8%"
TAX_UNKNOWN = ""  # 空 = MF の科目デフォルトに委ねる


@dataclass(frozen=True)
class Classification:
    debit_account: str
    debit_sub_account: str
    tax_category: str
    needs_review: bool
    review_notes: list[str]


def _pick_account(receipt: Receipt, config: Config) -> tuple[str, str, bool, str | None]:
    """(科目, 補助科目, フォールバックか, note) を返す。"""
    haystack = " ".join(
        filter(
            None,
            [
                receipt.merchant,
                receipt.category_hint or "",
                " ".join(li.description for li in receipt.line_items),
            ],
        )
    )

    best = None
    best_score = 0
    for rule in config.accounts:
        score = sum(1 for h in rule.hints if h and h in haystack)
        if score > best_score:
            best_score = score
            best = rule

    if best is None or best_score == 0:
        return config.fallback_account, "", True, "科目を自動判定できずフォールバック"
    return best.account, best.sub_account, False, None


def _tax_category(receipt: Receipt) -> tuple[str, bool, str | None]:
    """(税区分, 要確認か, note) を返す。"""
    if receipt.currency != Currency.JPY:
        # 国外取引は消費税対象外
        return TAX_EXEMPT, False, None

    rates = {round(b.rate_percent) for b in receipt.tax_buckets if b.rate_percent > 0}
    if rates == {10}:
        return TAX_10, False, None
    if rates == {8}:
        return TAX_8_REDUCED, False, None
    if not rates:
        # 税率表記を読めなかった → 主要ケースは 10%。ただし要確認にしておく
        return TAX_10, True, "税率表記を読めず 10% と仮定"
    if rates == {8, 10} or len(rates) > 1:
        # 税率混在は 1 仕訳に落とせない。要確認
        return TAX_UNKNOWN, True, f"税率混在 {sorted(rates)}、要手動按分"
    return TAX_UNKNOWN, True, f"想定外の税率 {sorted(rates)}"


def classify(receipt: Receipt, config: Config) -> Classification:
    account, sub, fallback, acc_note = _pick_account(receipt, config)
    tax, tax_review, tax_note = _tax_category(receipt)

    notes = [n for n in (acc_note, tax_note) if n]
    return Classification(
        debit_account=account,
        debit_sub_account=sub,
        tax_category=tax,
        needs_review=fallback or tax_review,
        review_notes=notes,
    )
