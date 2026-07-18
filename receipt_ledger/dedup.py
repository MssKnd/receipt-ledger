"""重複レシート検出。

(取引日, 借方金額(円), 店名) を当月+前月の CSV と照合する。同一なら重複疑い。
正当な同店同日同額の 2 会計は人間が review/ から戻す運用。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .csvwriter import SUMMARY_SEP, monthly_csv_path


def _prev_month(d: date) -> date:
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


def _norm(s: str) -> str:
    return s.strip().replace(SUMMARY_SEP, " ").casefold()


def dedup_key(txn_date: date, amount_jpy: int, merchant: str) -> tuple[str, int, str]:
    return (txn_date.strftime("%Y/%m/%d"), amount_jpy, _norm(merchant))


def _keys_in_csv(path: Path) -> set[tuple[str, int, str]]:
    keys: set[tuple[str, int, str]] = set()
    if not path.exists():
        return keys
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                amount = int(r.get("借方金額(円)", "0") or 0)
            except ValueError:
                continue
            summary = r.get("摘要", "")
            merchant = summary.split(SUMMARY_SEP, 1)[0] if summary else ""
            keys.add((r.get("取引日", ""), amount, _norm(merchant)))
    return keys


def is_duplicate(
    csv_dir: Path, txn_date: date, amount_jpy: int, merchant: str
) -> bool:
    """当月・前月の CSV に同じ (日付,金額,店名) があれば True。"""
    key = dedup_key(txn_date, amount_jpy, merchant)
    keys: set[tuple[str, int, str]] = set()
    keys |= _keys_in_csv(monthly_csv_path(csv_dir, txn_date))
    keys |= _keys_in_csv(monthly_csv_path(csv_dir, _prev_month(txn_date)))
    return key in keys
