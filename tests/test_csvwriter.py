import csv
from datetime import date

from receipt_ledger.csvwriter import (
    MF_COLUMNS,
    JournalRow,
    append_row,
    append_rows,
    monthly_csv_path,
)


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
    assert "CAD 12.34 @107.5" in row["メモ"]  # 換算根拠などの詳細はメモ列


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
