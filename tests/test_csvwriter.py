import csv
from datetime import date
from pathlib import Path

import pytest

from receipt_ledger.csvwriter import (
    MF_COLUMNS,
    CsvFormatError,
    Entry,
    JournalRow,
    Line,
    append_entries,
    append_row,
    append_rows,
    monthly_csv_path,
    next_txn_no,
)

# MF が配布しているインポート用サンプルフォーマット (ダミーデータ)。
# 列仕様の正解として置いてある。
SAMPLE = Path(__file__).parent / "fixtures" / "mf-journal-sample.csv"


def _row(**kw) -> JournalRow:
    base = dict(
        txn_date=date(2024, 5, 10),
        debit_account="会議費",
        debit_sub_account="",
        debit_tax_category="課税仕入 10%",
        amount_jpy=1000,
        credit_account="短期借入金",
        credit_sub_account="立替者",
        merchant="スターバックス",
        summary_detail="1000円 src:a.jpg",
    )
    base.update(kw)
    return JournalRow(**base)


def test_append_creates_header_once(config):
    append_row(config.directories.csv, _row())
    append_row(config.directories.csv, _row(amount_jpy=2000))
    path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == MF_COLUMNS  # ヘッダは 1 回だけ
    assert len(rows) == 3  # header + 2 行


def test_monthly_split(config):
    append_row(config.directories.csv, _row(txn_date=date(2024, 5, 10)))
    append_row(config.directories.csv, _row(txn_date=date(2024, 6, 1)))
    assert monthly_csv_path(config.directories.csv, date(2024, 5, 10)).exists()
    assert monthly_csv_path(config.directories.csv, date(2024, 6, 1)).exists()


def test_fixed_credit_and_summary(config):
    append_row(config.directories.csv, _row(merchant="A店", summary_detail="CAD 12.34 @107.5 (2024-05-10)"))
    path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    with open(path, encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    assert row["貸方勘定科目"] == "短期借入金"
    assert row["貸方補助科目"] == "立替者"
    assert row["借方金額(円)"] == "1000"
    assert row["摘要"] == "A店"  # 摘要は店名のみ
    # 換算根拠などの詳細は仕訳メモ列 (インポート形式の列名は「メモ」ではない)
    assert "CAD 12.34 @107.5" in row["仕訳メモ"]


def test_empty_file_gets_header(config):
    # 0 バイトで存在するファイルにもヘッダを書く (列ずれ防止)
    path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()  # 0 バイト
    append_row(config.directories.csv, _row())
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == MF_COLUMNS
    assert len(rows) == 2


def test_append_rows_batches_by_month(config):
    paths = append_rows(
        config.directories.csv,
        [
            _row(txn_date=date(2024, 5, 10)),
            _row(txn_date=date(2024, 5, 20)),
            _row(txn_date=date(2024, 6, 1)),
        ],
    )
    may = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    jun = monthly_csv_path(config.directories.csv, date(2024, 6, 1))
    assert set(paths) == {may, jun}
    with open(may, encoding="utf-8-sig", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 2  # 5月は 2 行
    with open(jun, encoding="utf-8-sig", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 1


def test_formula_injection_is_neutralized(config):
    append_row(config.directories.csv, _row(merchant="=HYPERLINK(evil)"))
    path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    with open(path, encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    assert row["摘要"].startswith("'=HYPERLINK(evil)")


def test_columns_match_mf_sample_format():
    """列は MF 配布のインポート用サンプルフォーマットと一致していること。

    以前はエクスポート形式 (19 列) を使っており、名前の違う「メモ」列が
    取り込み時に落ちていた。"""
    with open(SAMPLE, encoding="utf-8-sig", newline="") as fh:
        header = next(csv.reader(fh))
    assert header == MF_COLUMNS


def _entry(txn_date=date(2024, 5, 10), **kw) -> Entry:
    """借方 2 行 (費用 + 手数料) / 貸方 1 行 の複合仕訳。"""
    return Entry(
        txn_date=txn_date,
        lines=(
            Line(
                debit_account="備品・消耗品費",
                debit_amount_jpy=kw.get("amount", 8000),
                credit_account="預け金",
                credit_sub_account="Wise",
                credit_amount_jpy=kw.get("amount", 8000) + kw.get("fee", 50),
                summary=kw.get("summary", "Example Store"),
                memo=kw.get("memo", ""),
            ),
            Line(
                debit_account="支払手数料",
                debit_sub_account="Wise",
                debit_amount_jpy=kw.get("fee", 50),
            ),
        ),
    )


def _rows(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_compound_entry_shares_one_txn_no(config):
    append_entries(config.directories.csv, [_entry()], prefix="wise")
    rows = _rows(monthly_csv_path(config.directories.csv, date(2024, 5, 10), "wise"))
    assert len(rows) == 2
    # 複合仕訳の成立条件は「取引No 同一・取引日同一・貸借合計一致」
    assert rows[0]["取引No"] == rows[1]["取引No"]
    assert rows[0]["取引日"] == rows[1]["取引日"]
    debits = int(rows[0]["借方金額(円)"]) + int(rows[1]["借方金額(円)"])
    assert int(rows[0]["貸方金額(円)"]) == debits


def test_continuation_line_uses_zero_not_blank(config):
    """サンプルフォーマットは埋まらない側の金額を空欄ではなく 0 にしている。"""
    append_entries(config.directories.csv, [_entry()], prefix="wise")
    rows = _rows(monthly_csv_path(config.directories.csv, date(2024, 5, 10), "wise"))
    assert rows[1]["貸方勘定科目"] == ""
    assert rows[1]["貸方金額(円)"] == "0"
    assert rows[0]["借方税額"] == rows[0]["貸方税額"] == "0"


def test_separate_entries_get_distinct_txn_no(config):
    """同じ取引日の別取引が 1 つの複合仕訳にマージされないこと。

    取引No が同一だと MF が貸借合計の一致を見てまとめてしまう。"""
    day = date(2024, 5, 10)
    append_entries(config.directories.csv, [_entry(day), _entry(day, amount=2500, fee=15)], "wise")
    rows = _rows(monthly_csv_path(config.directories.csv, day, "wise"))
    assert len(rows) == 4
    assert rows[0]["取引No"] == rows[1]["取引No"]
    assert rows[2]["取引No"] == rows[3]["取引No"]
    assert rows[0]["取引No"] != rows[2]["取引No"]


def test_txn_no_continues_across_appends(config):
    """月ファイルは追記されるので、採番は既存行から引き継ぐ。"""
    day = date(2024, 5, 10)
    append_entries(config.directories.csv, [_entry(day)], "wise")
    append_entries(config.directories.csv, [_entry(day, amount=2500)], "wise")
    rows = _rows(monthly_csv_path(config.directories.csv, day, "wise"))
    assert [r["取引No"] for r in rows] == ["1", "1", "2", "2"]


def test_receipt_rows_also_get_txn_no(config):
    """単一仕訳も取引No を持つ (空欄だと同日の仕訳がマージされうる)。"""
    append_rows(config.directories.csv, [_row(), _row(amount_jpy=2000)])
    rows = _rows(monthly_csv_path(config.directories.csv, date(2024, 5, 10)))
    assert [r["取引No"] for r in rows] == ["1", "2"]


def test_next_txn_no_on_missing_file(tmp_path: Path):
    assert next_txn_no(tmp_path / "nope.csv") == 1


def test_append_to_stale_header_is_refused(config):
    """列を増やしたあと古い月ファイルに追記すると列がずれた行が静かに混ざる。"""
    path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("取引No,取引日,借方勘定科目\n", encoding="utf-8-sig")
    with pytest.raises(CsvFormatError, match="列構成"):
        append_row(config.directories.csv, _row())


def test_long_summary_is_clipped(config):
    append_row(config.directories.csv, _row(merchant="あ" * 300))
    row = next(iter(_rows(monthly_csv_path(config.directories.csv, date(2024, 5, 10)))))
    assert len(row["摘要"]) == 200


def test_entry_balance_helpers():
    assert _entry().is_balanced()
    assert not Entry(date(2024, 5, 10), (Line(debit_amount_jpy=1),)).is_balanced()
