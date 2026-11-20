"""Wise 履歴 → wise-YYYY-MM.csv の書き込み。

レシートとは別ファイルに出す。理由は 2 つ:

- MF への取込単位と人の確認単位を分けたい。
- `dedup.py` が receipts-*.csv を読んで (日付, 借方金額, 店名) を重複キーに
  するので、同じファイルに混ぜると Wise の行がレシートの重複判定を汚す。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..csvwriter import append_entries
from . import ledger
from .journal import WiseEntry, to_csv_entry, to_entries
from .parse import Skipped, parse_file

CSV_PREFIX = "wise"


@dataclass(frozen=True)
class ImportResult:
    written: list[WiseEntry]
    already_imported: list[WiseEntry]
    skipped: list[Skipped]
    paths: list[Path]

    @property
    def review_count(self) -> int:
        return sum(1 for e in self.written if e.needs_review)

    @property
    def total_jpy(self) -> int:
        return sum(e.credit_amount_jpy for e in self.written)


def run(path: Path, config: Config, *, dry_run: bool = False) -> ImportResult:
    txns, skipped = parse_file(path)
    entries = to_entries(txns, config)

    seen = ledger.imported_txn_ids(config.directories.csv)
    fresh = [e for e in entries if e.txn.txn_id not in seen]
    already = [e for e in entries if e.txn.txn_id in seen]

    paths: list[Path] = []
    if fresh and not dry_run:
        # 取引日の古い順に書く。取引No が時系列に並ぶほうが後から追いやすい。
        ordered = sorted(fresh, key=lambda e: (e.txn.txn_date, e.txn.txn_id))
        paths = append_entries(
            config.directories.csv,
            [to_csv_entry(e) for e in ordered],
            prefix=CSV_PREFIX,
        )

    return ImportResult(written=fresh, already_imported=already, skipped=skipped, paths=paths)
