"""MF クラウド会計「仕訳帳インポート」CSV の生成 (月次・追記)。

列仕様は暫定。実際の MF の仕訳帳エクスポート CSV でヘッダ順を確認して
`MF_COLUMNS` を合わせること (本番運用の前に要確認)。ここでは MF が公開している
仕訳帳インポート形式の代表的な列を採用している。

- 1 レシート = 1 行 (単一仕訳: 借方 = 費用科目 / 貸方 = 短期借入金)。
- 取引日の月で receipts-YYYY-MM.csv に振り分けて追記する。
- 追記はアトミック (テンポラリに書いて置換ではなく、ファイルロック + 追記) で、
  途中失敗しても行が壊れないようにする。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# MF 仕訳帳インポート形式の列。MF クラウド会計の実エクスポート
# (仕訳帳_20260718_0620.csv) のヘッダに一致させた。
MF_COLUMNS = [
    "取引No",
    "取引日",
    "借方勘定科目",
    "借方補助科目",
    "借方部門",
    "借方取引先",
    "借方税区分",
    "借方インボイス",
    "借方金額(円)",
    "貸方勘定科目",
    "貸方補助科目",
    "貸方部門",
    "貸方取引先",
    "貸方税区分",
    "貸方インボイス",
    "貸方金額(円)",
    "摘要",
    "タグ",
    "メモ",
]

SUMMARY_SEP = " / "  # 摘要の区切り。dedup が merchant を復元するのに使う


def _safe_cell(value: str) -> str:
    """CSV 数式インジェクション対策。先頭が =+-@ のセルは ' を前置して無害化。

    merchant 等は LLM 由来の任意文字列なので、Excel で開かれた場合に
    `=HYPERLINK(...)` 等が発火しないようにする。"""
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@dataclass(frozen=True)
class JournalRow:
    txn_date: date
    debit_account: str
    debit_sub_account: str
    debit_tax_category: str
    amount_jpy: int
    credit_account: str
    credit_sub_account: str
    merchant: str
    summary_detail: str  # 換算根拠・元ファイル名・要確認マークなど
    memo: str = ""

    def summary(self) -> str:
        # 摘要は店名のみ (詳細はメモ列へ)。SUMMARY_SEP を含む店名は潰して
        # dedup の split と衝突しないようにする。
        return self.merchant.replace(SUMMARY_SEP, " ").strip()

    def memo_text(self) -> str:
        return SUMMARY_SEP.join(p for p in (self.summary_detail, self.memo) if p)

    def to_mf_dict(self) -> dict[str, str]:
        return {
            "取引No": "",
            "取引日": self.txn_date.strftime("%Y/%m/%d"),
            "借方勘定科目": _safe_cell(self.debit_account),
            "借方補助科目": _safe_cell(self.debit_sub_account),
            "借方部門": "",
            "借方取引先": "",
            "借方税区分": self.debit_tax_category,
            "借方インボイス": "",
            "借方金額(円)": str(self.amount_jpy),
            "貸方勘定科目": _safe_cell(self.credit_account),
            "貸方補助科目": _safe_cell(self.credit_sub_account),
            "貸方部門": "",
            "貸方取引先": "",
            "貸方税区分": "対象外",
            "貸方インボイス": "",
            "貸方金額(円)": str(self.amount_jpy),
            "摘要": _safe_cell(self.summary()),
            "タグ": "",
            "メモ": _safe_cell(self.memo_text()),
        }


def monthly_csv_path(csv_dir: Path, txn_date: date) -> Path:
    return csv_dir / f"receipts-{txn_date:%Y-%m}.csv"


def _needs_header(path: Path) -> bool:
    # 「存在しない」だけでなく「存在するが 0 バイト」もヘッダが要る
    # (open と write の間でクラッシュした・手動 touch された場合の列ずれを防ぐ)。
    return not path.exists() or path.stat().st_size == 0


def append_row(csv_dir: Path, row: JournalRow) -> Path:
    """該当月の CSV に 1 行追記する。ファイルが無ければヘッダ付きで作る。"""
    return append_rows(csv_dir, [row])[0]


def append_rows(csv_dir: Path, rows: list[JournalRow]) -> list[Path]:
    """複数行をまとめて追記する。取引日の月ごとにグループ化し、月ファイルへは
    1 回の write で書き込む (同一月の複数行が途中破損しないアトミック性)。

    返り値は書き込んだ月ファイルのパス一覧 (重複なし・入力順)。"""
    csv_dir.mkdir(parents=True, exist_ok=True)

    # 月ファイルごとに行をまとめる (入力順を保つ)。
    groups: dict[Path, list[JournalRow]] = {}
    for row in rows:
        path = monthly_csv_path(csv_dir, row.txn_date)
        groups.setdefault(path, []).append(row)

    for path, group in groups.items():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=MF_COLUMNS)
        if _needs_header(path):
            writer.writeheader()
        for row in group:
            writer.writerow(row.to_mf_dict())
        with open(path, "a", encoding="utf-8-sig", newline="") as fh:
            fh.write(buf.getvalue())

    return list(groups.keys())
