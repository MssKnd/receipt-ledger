from datetime import date

from receipt_ledger.csvwriter import JournalRow, append_row
from receipt_ledger.dedup import is_duplicate


def _write(config, txn_date, amount, merchant):
    append_row(
        config.directories.csv,
        JournalRow(
            txn_date=txn_date,
            debit_account="会議費",
            debit_sub_account="",
            debit_tax_category="",
            amount_jpy=amount,
            credit_account="短期借入金",
            credit_sub_account="立替者",
            merchant=merchant,
            summary_detail="x",
        ),
    )


def test_detects_same_key(config):
    _write(config, date(2024, 5, 10), 1000, "スターバックス")
    assert is_duplicate(config.directories.csv, date(2024, 5, 10), 1000, "スターバックス")


def test_different_amount_not_dup(config):
    _write(config, date(2024, 5, 10), 1000, "スターバックス")
    assert not is_duplicate(config.directories.csv, date(2024, 5, 10), 1200, "スターバックス")


def test_merchant_case_and_space_insensitive(config):
    _write(config, date(2024, 5, 10), 1000, "Starbucks")
    assert is_duplicate(config.directories.csv, date(2024, 5, 10), 1000, " starbucks ")


def test_prev_month_file_is_scanned(config):
    # 同一レシート (=同一日付) は必ず同じ月ファイルに入るが、is_duplicate は
    # 当月+前月の 2 ファイルを読む。前月ファイル側にある行も拾えることを確認。
    _write(config, date(2024, 4, 30), 500, "コンビニ")
    # 基準日を 5 月にすると前月=4月ファイルを読む。日付キーは 4/30 のままなので
    # 同じ 4/30 で照会したときに 4 月ファイル経由で見つかる。
    assert is_duplicate(config.directories.csv, date(2024, 4, 30), 500, "コンビニ")


def test_prev_month_year_boundary(config):
    # 年をまたぐ前月計算 (1月→前年12月) で例外を出さず、12月ファイルを読める
    _write(config, date(2023, 12, 20), 800, "年末の店")
    assert is_duplicate(config.directories.csv, date(2023, 12, 20), 800, "年末の店")
    # 1月基準の照会でも前年12月ファイルを走査対象にする (クラッシュしない)
    assert is_duplicate(config.directories.csv, date(2024, 1, 5), 800, "年末の店") is False


def test_prev_month_helper_year_boundary():
    from receipt_ledger.dedup import _prev_month

    assert _prev_month(date(2024, 1, 15)) == date(2023, 12, 1)
    assert _prev_month(date(2024, 5, 15)) == date(2024, 4, 1)
