import csv
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from receipt_ledger.config import MerchantRule, WiseConfig
from receipt_ledger.csvwriter import monthly_csv_path
from receipt_ledger.wise import ledger
from receipt_ledger.wise.importer import CSV_PREFIX, run

FIXTURE = Path(__file__).parent / "fixtures" / "wise-sample.csv"


@pytest.fixture
def wise_config(config):
    return replace(
        config,
        wise=WiseConfig(
            merchants=(
                MerchantRule(match="Example Store", account="備品・消耗品費", summary="Store"),
                MerchantRule(match="Example SaaS", account="支払手数料", sub_account="Vendor"),
                MerchantRule(match="Example Contractor", account="業務委託料"),
            )
        ),
    )


def _rows(csv_dir: Path, day: date):
    path = monthly_csv_path(csv_dir, day, CSV_PREFIX)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_writes_to_wise_prefixed_file_not_receipts(wise_config):
    result = run(FIXTURE, wise_config)
    csv_dir = wise_config.directories.csv
    assert result.paths
    assert all(p.name.startswith("wise-") for p in result.paths)
    # レシート側の dedup が読む receipts-*.csv を汚さないこと
    assert not list(csv_dir.glob("receipts-*.csv"))


def test_compound_entry_is_written(wise_config):
    run(FIXTURE, wise_config)
    rows = _rows(wise_config.directories.csv, date(2026, 7, 3))
    head = next(r for r in rows if r["摘要"] == "Store")
    tail = rows[rows.index(head) + 1]
    assert head["取引No"] == tail["取引No"]
    assert head["借方勘定科目"] == "備品・消耗品費"
    assert head["借方金額(円)"] == "8000"
    assert (head["貸方勘定科目"], head["貸方補助科目"]) == ("預け金", "Wise")
    assert head["貸方金額(円)"] == "8050"
    assert (tail["借方勘定科目"], tail["借方補助科目"]) == ("支払手数料", "Wise")
    assert tail["借方金額(円)"] == "50"
    assert tail["貸方金額(円)"] == "0"


def test_memo_carries_txn_id_for_dedup(wise_config):
    run(FIXTURE, wise_config)
    rows = _rows(wise_config.directories.csv, date(2026, 7, 3))
    memo = next(r["仕訳メモ"] for r in rows if r["摘要"] == "Store")
    assert "CARD_TRANSACTION-1000000001" in memo
    assert "Wise実レート" in memo


def test_second_import_writes_nothing(wise_config):
    first = run(FIXTURE, wise_config)
    assert len(first.written) == 5

    second = run(FIXTURE, wise_config)
    assert second.written == []
    assert len(second.already_imported) == 5
    assert second.paths == []

    # 行数が増えていないこと (MF は重複を弾かないのでここで止めるしかない)
    rows = _rows(wise_config.directories.csv, date(2026, 7, 3))
    assert len([r for r in rows if r["摘要"] == "Store"]) == 1


def test_dry_run_writes_nothing(wise_config):
    result = run(FIXTURE, wise_config, dry_run=True)
    assert result.written
    assert result.paths == []
    assert not list(wise_config.directories.csv.glob("*.csv"))


def test_same_day_transactions_get_distinct_txn_no(wise_config):
    """同一取引日の別取引がマージされないこと。

    MF は「取引No 同一・取引日同一・貸借合計一致」で複合仕訳にまとめる。"""
    run(FIXTURE, wise_config)
    rows = _rows(wise_config.directories.csv, date(2026, 6, 21))
    # 06/21 と 06/23 は別ファイルではなく同じ 2026-06 に入る
    june = _rows(wise_config.directories.csv, date(2026, 6, 1))
    heads = [r for r in june if r["貸方勘定科目"]]
    assert len({r["取引No"] for r in heads}) == len(heads)
    assert rows == june


def test_review_entries_are_marked_in_summary_and_memo(config):
    """科目が決まらない取引も書くが、摘要とメモに要確認を残す。"""
    result = run(FIXTURE, replace(config, wise=WiseConfig(), accounts=(), fallback_account="雑費"))
    assert result.review_count == len(result.written)
    rows = _rows(config.directories.csv, date(2026, 7, 3))
    head = next(r for r in rows if r["借方勘定科目"] == "雑費")
    assert "要確認" in head["摘要"]
    assert "要確認" in head["仕訳メモ"]


def test_ledger_reads_ids_back_from_generated_csv(wise_config):
    run(FIXTURE, wise_config)
    found = ledger.imported_txn_ids(wise_config.directories.csv)
    assert "CARD_TRANSACTION-1000000001" in found
    assert "TRANSFER-2000000001" in found
    # 仕訳にしなかった行は台帳に載らない (あとで手当てできるように)
    assert "TRANSFER-2000000002" not in found


def test_ledger_ignores_missing_dir(tmp_path: Path):
    assert ledger.imported_txn_ids(tmp_path / "nope") == set()


def test_totals(wise_config):
    result = run(FIXTURE, wise_config)
    assert result.total_jpy == sum(e.credit_amount_jpy for e in result.written)
    assert len(result.skipped) == 3
