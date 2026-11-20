"""MF クラウド会計「仕訳帳インポート」CSV の生成 (月次・追記)。

列は MF の**インポート用サンプルフォーマット** (`journal_sample.csv`, 27 列) に
一致させている。MF の仕訳帳**エクスポート** (19 列) とは別物で、エクスポート側には
借方税額/貸方税額/MF仕訳タイプ/決算整理仕訳が無く、メモ列の名前も「メモ」
(インポートは「仕訳メモ」) と違う。取り込み時の列自動マッピングのおかげで
エクスポート形式でも取り込めてしまうが、名前の違う列は落ちる。

仕訳の単位:

- 1 レシート = 1 行 (単一仕訳: 借方 = 費用科目 / 貸方 = 短期借入金)。
- Wise の 1 取引 = 複数行 (複合仕訳: 借方 費用 + 支払手数料 / 貸方 預け金)。

取引No は**必ず採番する**。MF は「取引No が同一・取引日が同一・貸借の合計が一致」
の 3 条件を満たす行を 1 つの複合仕訳としてまとめるため、空欄のまま同じ取引日の
仕訳を並べると別々の取引がマージされうる。会計年度で使用済みの番号を指定した
場合は MF 側が「最大取引No+1」に振り直す (実測: 衝突しても取引ごとに別々の番号が
振られ、グループは保たれた) ので、ファイル内で一意でありさえすればよい。

- 取引日の月で receipts-YYYY-MM.csv / wise-YYYY-MM.csv に振り分けて追記する。
- 追記はアトミック (同一月の複数行を 1 回の write で書く) で、途中失敗しても
  行が壊れないようにする。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# MF 仕訳帳インポートのサンプルフォーマット (journal_sample.csv) のヘッダ。
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
    "借方税額",
    "貸方勘定科目",
    "貸方補助科目",
    "貸方部門",
    "貸方取引先",
    "貸方税区分",
    "貸方インボイス",
    "貸方金額(円)",
    "貸方税額",
    "摘要",
    "仕訳メモ",
    "タグ",
    "MF仕訳タイプ",
    "決算整理仕訳",
    "作成日時",
    "作成者",
    "最終更新日時",
    "最終更新者",
]

SUMMARY_SEP = " / "  # 摘要の区切り。dedup が merchant を復元するのに使う

# MF 側の桁数上限 (超過分は MF が黙って切り捨てる)。
_MAX_SUMMARY = 200
_MAX_MEMO = 200


class CsvFormatError(Exception):
    """既存ファイルの列構成が現在の MF_COLUMNS と食い違う。"""


def _safe_cell(value: str) -> str:
    """CSV 数式インジェクション対策。先頭が =+-@ のセルは ' を前置して無害化。

    merchant 等は LLM 由来の任意文字列なので、Excel で開かれた場合に
    `=HYPERLINK(...)` 等が発火しないようにする。"""
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _clip(value: str, limit: int) -> str:
    return value[:limit]


@dataclass(frozen=True)
class Line:
    """複合仕訳の 1 行。借方・貸方はどちらも空にできる。

    MF のサンプルフォーマットでは、貸方だけの行は借方勘定科目を空にして
    借方金額(円) を **0** にする (空欄ではなく 0)。逆も同じ。
    """

    debit_account: str = ""
    debit_sub_account: str = ""
    debit_tax_category: str = ""
    debit_amount_jpy: int = 0
    credit_account: str = ""
    credit_sub_account: str = ""
    credit_tax_category: str = ""
    credit_amount_jpy: int = 0
    summary: str = ""
    memo: str = ""

    def to_mf_dict(self, txn_no: int, txn_date: date) -> dict[str, str]:
        row = dict.fromkeys(MF_COLUMNS, "")
        row.update(
            {
                "取引No": str(txn_no),
                "取引日": txn_date.strftime("%Y/%m/%d"),
                "借方勘定科目": _safe_cell(self.debit_account),
                "借方補助科目": _safe_cell(self.debit_sub_account),
                "借方税区分": self.debit_tax_category,
                "借方金額(円)": str(self.debit_amount_jpy),
                "借方税額": "0",
                "貸方勘定科目": _safe_cell(self.credit_account),
                "貸方補助科目": _safe_cell(self.credit_sub_account),
                "貸方税区分": self.credit_tax_category,
                "貸方金額(円)": str(self.credit_amount_jpy),
                "貸方税額": "0",
                "摘要": _safe_cell(_clip(self.summary, _MAX_SUMMARY)),
                "仕訳メモ": _safe_cell(_clip(self.memo, _MAX_MEMO)),
            }
        )
        return row


@dataclass(frozen=True)
class Entry:
    """1 取引。行が複数なら MF 側で複合仕訳になる。"""

    txn_date: date
    lines: tuple[Line, ...] = field(default_factory=tuple)

    def debit_total(self) -> int:
        return sum(line.debit_amount_jpy for line in self.lines)

    def credit_total(self) -> int:
        return sum(line.credit_amount_jpy for line in self.lines)

    def is_balanced(self) -> bool:
        return self.debit_total() == self.credit_total()


@dataclass(frozen=True)
class JournalRow:
    """レシート 1 枚 = 単一仕訳 1 行。"""

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
        # 摘要は店名のみ (詳細は仕訳メモ列へ)。SUMMARY_SEP を含む店名は潰して
        # dedup の split と衝突しないようにする。
        return self.merchant.replace(SUMMARY_SEP, " ").strip()

    def memo_text(self) -> str:
        return SUMMARY_SEP.join(p for p in (self.summary_detail, self.memo) if p)

    def to_entry(self) -> Entry:
        return Entry(
            txn_date=self.txn_date,
            lines=(
                Line(
                    debit_account=self.debit_account,
                    debit_sub_account=self.debit_sub_account,
                    debit_tax_category=self.debit_tax_category,
                    debit_amount_jpy=self.amount_jpy,
                    credit_account=self.credit_account,
                    credit_sub_account=self.credit_sub_account,
                    credit_tax_category="対象外",
                    credit_amount_jpy=self.amount_jpy,
                    summary=self.summary(),
                    memo=self.memo_text(),
                ),
            ),
        )


def monthly_csv_path(csv_dir: Path, txn_date: date, prefix: str = "receipts") -> Path:
    return csv_dir / f"{prefix}-{txn_date:%Y-%m}.csv"


def _read_header(path: Path) -> list[str] | None:
    """既存ファイルのヘッダ。存在しない・空なら None。"""
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return next(csv.reader(fh), None)


def next_txn_no(path: Path) -> int:
    """そのファイルで次に使う取引No。既存の最大 + 1 (無ければ 1)。

    月ファイルは追記されるので、採番は書き込みのたびに既存行から引き継ぐ。
    会計年度で使用済みの番号なら MF 側が振り直すため、ファイル内で一意で
    あればよい。
    """
    if not path.exists() or path.stat().st_size == 0:
        return 1
    largest = 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                largest = max(largest, int(row.get("取引No", "") or 0))
            except ValueError:
                continue
    return largest + 1


def _check_header(path: Path) -> bool:
    """ヘッダが要るか返す。既存ヘッダが今の列構成と違えば例外。

    列を増やしたあとに古い月ファイルへ追記すると、列がずれた行が静かに
    混ざる。CSV は壊れていないように見えるので、取り込むまで気づけない。
    """
    header = _read_header(path)
    if header is None:
        return True
    if header != MF_COLUMNS:
        raise CsvFormatError(
            f"{path} の列構成が現在の MF_COLUMNS と違うため追記できない。\n"
            f"  既存: {len(header)} 列 {header}\n"
            f"  期待: {len(MF_COLUMNS)} 列\n"
            "別名のファイルに書くか、既存ファイルを退避してから再実行すること。"
        )
    return False


def append_entries(csv_dir: Path, entries: list[Entry], prefix: str = "receipts") -> list[Path]:
    """取引を月ファイルへ追記する。取引No はファイルごとに採番する。

    返り値は書き込んだ月ファイルのパス一覧 (重複なし・入力順)。"""
    csv_dir.mkdir(parents=True, exist_ok=True)

    groups: dict[Path, list[Entry]] = {}
    for entry in entries:
        path = monthly_csv_path(csv_dir, entry.txn_date, prefix)
        groups.setdefault(path, []).append(entry)

    for path, group in groups.items():
        needs_header = _check_header(path)
        txn_no = next_txn_no(path)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=MF_COLUMNS)
        if needs_header:
            writer.writeheader()
        for entry in group:
            for line in entry.lines:
                writer.writerow(line.to_mf_dict(txn_no, entry.txn_date))
            txn_no += 1
        with open(path, "a", encoding="utf-8-sig", newline="") as fh:
            fh.write(buf.getvalue())

    return list(groups.keys())


def append_row(csv_dir: Path, row: JournalRow) -> Path:
    """該当月の CSV に 1 仕訳追記する。ファイルが無ければヘッダ付きで作る。"""
    return append_rows(csv_dir, [row])[0]


def append_rows(csv_dir: Path, rows: list[JournalRow]) -> list[Path]:
    """レシート由来の単一仕訳をまとめて追記する。"""
    return append_entries(csv_dir, [r.to_entry() for r in rows])
