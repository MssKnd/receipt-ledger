"""Wise の取引 → MF の複合仕訳。

既存の帳簿 (仕訳帳エクスポート 2026-01〜05) の 23 取引は例外なくこの形だった:

    借方1  費用科目 / 補助科目       = Source amount (after fees)
    借方2  支払手数料 / Wise         = Source fee amount
    貸方   預け金 / Wise             = 上記の合計   ← 1 行目にのみ記載
    摘要                              ← 1 行目にのみ記載

税区分・インボイス列は既存の手入力が全行空だったので、ここでも空で出して
MF の科目デフォルトに委ねる。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..classify import best_rule, normalize
from ..config import Config
from ..csvwriter import SUMMARY_SEP, Entry, Line
from .parse import WiseTxn

# 摘要に付ける要確認マーク。レシート側と同じ運用にする。
REVIEW_MARK = "要確認"


@dataclass(frozen=True)
class DebitLine:
    account: str
    sub_account: str
    amount_jpy: int


@dataclass(frozen=True)
class WiseEntry:
    """MF の複合仕訳 1 組 (借方 n 行 / 貸方 1 行)。"""

    txn: WiseTxn
    debits: tuple[DebitLine, ...]
    credit_account: str
    credit_sub_account: str
    summary: str
    memo: str
    needs_review: bool
    review_notes: tuple[str, ...]

    @property
    def credit_amount_jpy(self) -> int:
        return sum(d.amount_jpy for d in self.debits)

    def is_balanced(self) -> bool:
        return self.credit_amount_jpy == self.txn.total_jpy


def _merchant_rule(txn: WiseTxn, config: Config):
    """Wise 専用の店名上書きルール。最長一致を採る (短い語の巻き込み防止)。"""
    haystack = normalize(txn.merchant)
    matched = [r for r in config.wise.merchants if r.match and normalize(r.match) in haystack]
    return max(matched, key=lambda r: len(r.match)) if matched else None


def _pick_account(txn: WiseTxn, config: Config) -> tuple[str, str, str, str | None]:
    """(科目, 補助科目, 摘要, note) を返す。note が非 None なら要確認。

    Wise の Category 列は単体では使えない (実データでは外注費の送金と小売店での
    買い物がどちらも "General" だった)。店名を主キーにする。

    確信を持って無印で通すのは `[[wise.merchants]]` に登録された取引先だけ。
    共通ホワイトリストは日本語のレジ印字向けにヒントを調整したものなので、
    Wise の英語取引先名に対しては「たまたま 1 語含む」程度の根拠しか無い
    (実測: 同じヒント語を 2 科目が持っていると、同点タイが定義順で解決されて
    無関係な科目に化けた)。弱い根拠のまま無印で確定させると、人が preview で
    誤りを止めるという本機能の安全設計を迂回してしまう。
    """
    rule = _merchant_rule(txn, config)
    if rule is not None:
        return rule.account, rule.sub_account, rule.summary or txn.merchant, None

    best = best_rule(config, txn.merchant, txn.category)
    if best is not None:
        return (
            best.account,
            best.sub_account,
            txn.merchant,
            f"共通ホワイトリスト由来の弱い一致 ({best.account})。"
            f"確定するなら [[wise.merchants]] に {txn.merchant} を登録すること",
        )

    return (
        config.fallback_account,
        "",
        txn.merchant,
        f"科目を自動判定できずフォールバック ({txn.merchant} / {txn.category})",
    )


def to_entry(txn: WiseTxn, config: Config) -> WiseEntry:
    account, sub_account, summary, note = _pick_account(txn, config)

    debits = [DebitLine(account, sub_account, txn.amount_jpy)]
    if txn.fee_jpy:
        debits.append(
            DebitLine(config.wise.fee_account, config.wise.fee_sub_account, txn.fee_jpy)
        )

    notes = [n for n in (note,) if n]
    memo = " / ".join(p for p in (txn.fx_note(), txn.txn_id) if p)

    return WiseEntry(
        txn=txn,
        debits=tuple(debits),
        credit_account=config.wise.credit_account,
        credit_sub_account=config.wise.credit_sub_account,
        summary=summary,
        memo=memo,
        needs_review=bool(notes),
        review_notes=tuple(notes),
    )


def to_entries(txns: list[WiseTxn], config: Config) -> list[WiseEntry]:
    return [to_entry(t, config) for t in txns]


def to_csv_entry(entry: WiseEntry) -> Entry:
    """MF に書く形へ。借方 n 行 / 貸方は 1 行目にのみ載せる。

    仕訳メモに Wise の取引 ID を残す。これが二重取り込み防止の鍵になる
    (ledger.imported_txn_ids がここから読み戻す)。税区分は空にして MF の
    科目デフォルトに委ねる (既存の手入力仕訳が全行空欄なのに合わせた)。
    """
    memo_parts = [entry.memo]
    if entry.needs_review:
        memo_parts.append(SUMMARY_SEP.join([REVIEW_MARK, *entry.review_notes]))
    memo = SUMMARY_SEP.join(p for p in memo_parts if p)

    summary = f"{entry.summary} ({REVIEW_MARK})" if entry.needs_review else entry.summary

    lines = []
    for i, debit in enumerate(entry.debits):
        head = i == 0
        lines.append(
            Line(
                debit_account=debit.account,
                debit_sub_account=debit.sub_account,
                debit_amount_jpy=debit.amount_jpy,
                credit_account=entry.credit_account if head else "",
                credit_sub_account=entry.credit_sub_account if head else "",
                credit_amount_jpy=entry.credit_amount_jpy if head else 0,
                summary=summary if head else "",
                memo=memo if head else "",
            )
        )
    return Entry(txn_date=entry.txn.txn_date, lines=tuple(lines))
