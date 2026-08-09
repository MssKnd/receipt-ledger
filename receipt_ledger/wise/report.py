"""仕訳案の人向け表示。

CSV に書く前に人が 1 か月分を目で検証するための出力。会計判断の誤りが MF の
帳簿に流れ込む前に止めるのが目的なので、要確認と要手動を目立たせる。
"""

from __future__ import annotations

from .journal import WiseEntry
from .parse import Skipped


def _entry_lines(entry: WiseEntry) -> list[str]:
    mark = " ⚠" if entry.needs_review else ""
    head, *rest = entry.debits
    lines = [
        f"{entry.txn.txn_date:%Y/%m/%d}  "
        f"借 {head.account}"
        f"{'/' + head.sub_account if head.sub_account else ''} {head.amount_jpy:>9,}  "
        f"/  貸 {entry.credit_account}/{entry.credit_sub_account} "
        f"{entry.credit_amount_jpy:>9,}   {entry.summary}{mark}"
    ]
    for d in rest:
        lines.append(
            f"{'':12}  借 {d.account}"
            f"{'/' + d.sub_account if d.sub_account else ''} {d.amount_jpy:>9,}"
        )
    if entry.memo:
        lines.append(f"{'':14}memo: {entry.memo}")
    for note in entry.review_notes:
        lines.append(f"{'':14}⚠ {note}")
    return lines


def _skipped_lines(skipped: list[Skipped], title: str) -> list[str]:
    out = [f"=== {title} {len(skipped)} 件 ==="]
    for s in skipped:
        when = f"{s.txn_date:%Y/%m/%d}" if s.txn_date else "----/--/--"
        out.append(f"{when}  {s.merchant or '(取引先なし)':<34} {s.reason}")
    out.append("")
    return out


def render(
    entries: list[WiseEntry],
    skipped: list[Skipped],
    *,
    already_imported: list[WiseEntry] | None = None,
    written_paths: list | None = None,
) -> str:
    already = already_imported or []
    out: list[str] = []

    if entries:
        heading = "書き込んだ仕訳" if written_paths else "仕訳案"
        out.append(f"=== {heading} {len(entries)} 件 ===")
        for entry in sorted(entries, key=lambda e: e.txn.txn_date):
            out += _entry_lines(entry)
            out.append("")

    if already:
        out.append(f"=== 取り込み済みのため除外 {len(already)} 件 ===")
        for e in sorted(already, key=lambda e: e.txn.txn_date):
            out.append(f"{e.txn.txn_date:%Y/%m/%d}  {e.summary:<34} {e.txn.txn_id}")
        out.append("")

    if skipped:
        out += _skipped_lines(skipped, "仕訳にしなかった行 (要手動確認)")

    review = sum(1 for e in entries if e.needs_review)
    unbalanced = [e for e in entries if not e.is_balanced()]
    total = sum(e.credit_amount_jpy for e in entries)
    out.append(
        f"{'書き込み' if written_paths else '仕訳案'} {len(entries)} 件 "
        f"(要確認 {review} 件) / 取り込み済み {len(already)} 件 / "
        f"スキップ {len(skipped)} 件 / 貸方合計 {total:,} 円"
    )
    if unbalanced:
        # 起きないはずだが、起きたら CSV に書く前に必ず気づきたい
        out.append(
            f"!! 貸借不一致 {len(unbalanced)} 件: " + ", ".join(e.txn.txn_id for e in unbalanced)
        )
    if written_paths:
        out += [f"書き込み先: {p}" for p in written_paths]
        out.append("MF は同じ内容を二度取り込んでも弾かないので、取込は 1 回だけにすること。")
    else:
        out.append("(CSV は書き込んでいません)")
    return "\n".join(out)
