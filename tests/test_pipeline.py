import csv
from datetime import date
from pathlib import Path

import pytest

import receipt_ledger.pipeline as pipeline
from receipt_ledger.csvwriter import monthly_csv_path
from receipt_ledger.fx import FxResult
from receipt_ledger.models import Currency, Extraction, Receipt
from receipt_ledger.pipeline import Outcome, process_file


def _mk_file(config, name="r.jpg") -> Path:
    p = config.directories.inbox / name
    p.write_bytes(b"fake")
    return p


@pytest.fixture(autouse=True)
def _no_fx_network(monkeypatch):
    # CAD 換算は固定レートに (ネットワークを触らせない)
    def fake_to_jpy(amount, on, currency, cache_path, base_url):
        return int(round(amount * 100.0)), FxResult(rate=100.0, rate_date=on.isoformat(), source="cache")

    monkeypatch.setattr(pipeline, "to_jpy", fake_to_jpy)


def _patch_extract(monkeypatch, receipts):
    monkeypatch.setattr(pipeline, "extract", lambda path, config: Extraction(receipts=receipts))


def _r(**kw) -> Receipt:
    base = dict(merchant="スターバックス", date="2024-05-10", total=1000.0, confidence=0.9)
    base.update(kw)
    return Receipt(**base)


def test_happy_path_writes_and_moves(config, monkeypatch):
    _patch_extract(monkeypatch, [_r()])
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.WRITTEN
    assert res.written_rows == 1
    assert not p.exists()
    # processed/2024/ に移動
    assert (config.directories.processed / "2024" / "r.jpg").exists()
    csv_path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["借方勘定科目"] == "会議費"


def test_multiple_receipts_one_image(config, monkeypatch):
    _patch_extract(monkeypatch, [_r(total=1000), _r(merchant="書店", total=2000, category_hint="書籍")])
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.WRITTEN
    assert res.written_rows == 2
    csv_path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    accounts = {r["借方勘定科目"] for r in rows}
    assert accounts == {"会議費", "新聞図書費"}


def test_failed_receipt_routes_whole_file_and_writes_nothing(config, monkeypatch):
    _patch_extract(monkeypatch, [_r(), _r(merchant="ぼやけ", confidence=0.1)])
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.FAILED
    assert res.written_rows == 0
    assert (config.directories.failed / "r.jpg").exists()
    # CSV は作られない
    assert not monthly_csv_path(config.directories.csv, date(2024, 5, 10)).exists()


def test_cad_conversion_in_summary(config, monkeypatch):
    _patch_extract(monkeypatch, [_r(merchant="Tim Hortons", currency=Currency.CAD, total=12.34)])
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.WRITTEN
    csv_path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    assert row["借方金額(円)"] == str(round(12.34 * 100.0))
    assert row["借方税区分"] == "対象外"
    assert "CAD 12.34 @100" in row["メモ"]


def test_duplicate_routes_to_review(config, monkeypatch):
    _patch_extract(monkeypatch, [_r()])
    process_file(_mk_file(config, "first.jpg"), config)
    # 同じ内容を再投入
    _patch_extract(monkeypatch, [_r()])
    res = process_file(_mk_file(config, "second.jpg"), config)
    assert res.outcome == Outcome.REVIEW
    assert res.written_rows == 0
    assert (config.directories.review / "second.jpg").exists()


def test_empty_extraction_is_failed(config, monkeypatch):
    _patch_extract(monkeypatch, [])
    res = process_file(_mk_file(config), config)
    assert res.outcome == Outcome.EMPTY
    assert (config.directories.failed / "r.jpg").exists()


def test_ollama_down_is_retry_and_keeps_file(config, monkeypatch):
    from receipt_ledger.extract import ExtractTransientError

    def boom(path, config):
        raise ExtractTransientError("Ollama 不達")

    monkeypatch.setattr(pipeline, "extract", boom)
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.RETRY
    assert p.exists()  # inbox に残る


def test_extract_permanent_error_is_failed(config, monkeypatch):
    from receipt_ledger.extract import ExtractPermanentError

    def boom(path, config):
        raise ExtractPermanentError("スキーマ不一致")

    monkeypatch.setattr(pipeline, "extract", boom)
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.FAILED
    assert (config.directories.failed / "r.jpg").exists()


def test_unknown_currency_is_failed_not_retry(config, monkeypatch):
    # 通貨判別不能 (OTHER) は換算不能 → 永久リトライではなく failed へ
    _patch_extract(monkeypatch, [_r(merchant="謎の海外店", currency=Currency.OTHER, total=50.0)])
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.FAILED
    assert not p.exists()
    assert (config.directories.failed / "r.jpg").exists()
    assert not monthly_csv_path(config.directories.csv, date(2024, 5, 10)).exists()


def test_fx_transient_error_writes_nothing_and_retries(config, monkeypatch):
    # 2 枚中 2 枚目の CAD 換算が一時失敗 → 1 行も書かずに RETRY (二重計上防止)
    from receipt_ledger.fx import FxTransientError

    def boom_fx(amount, on, currency, cache_path, base_url):
        raise FxTransientError("為替 API 不達")

    monkeypatch.setattr(pipeline, "to_jpy", boom_fx)
    _patch_extract(
        monkeypatch,
        [_r(total=1000), _r(merchant="Tim Hortons", currency=Currency.CAD, total=12.34)],
    )
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.RETRY
    assert res.written_rows == 0
    assert p.exists()  # inbox に残る
    # JPY レシート 1 枚目も書かれていない (フェーズ分離の効果)
    assert not monthly_csv_path(config.directories.csv, date(2024, 5, 10)).exists()


def test_scan_skips_unstable(config, monkeypatch):
    _patch_extract(monkeypatch, [_r()])
    _mk_file(config, "r.jpg")
    monkeypatch.setattr(pipeline, "_is_stable", lambda path, config, sleep=None: False)
    results = pipeline.scan(config, sleep=lambda s: None)
    assert results == []


def test_needs_review_marks_summary_but_writes(config, monkeypatch):
    # フォールバック科目 → CSV には書くが摘要に要確認
    _patch_extract(monkeypatch, [_r(merchant="判別不能", category_hint="謎")])
    res = process_file(_mk_file(config), config)
    assert res.outcome == Outcome.WRITTEN
    csv_path = monthly_csv_path(config.directories.csv, date(2024, 5, 10))
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    assert row["借方勘定科目"] == "雑費"
    assert "要確認" in row["メモ"]


def test_disallowed_currency_routes_to_review(config, monkeypatch):
    # 換算許可を CAD だけに絞ると、USD 判定 (誤判定の疑い) は review へ。
    cad_only = config.__class__(**{**config.__dict__, "fx_currencies": ("CAD",)})
    _patch_extract(monkeypatch, [_r(currency=Currency.USD)])
    p = _mk_file(config)
    res = process_file(p, cad_only)
    assert res.outcome == Outcome.REVIEW
    assert res.written_rows == 0
    assert (config.directories.review / "r.jpg").exists()
    assert any("許可リスト外" in x for x in res.reasons)


def test_allowed_currency_still_converts(config, monkeypatch):
    _patch_extract(monkeypatch, [_r(currency=Currency.CAD, total=10.0)])
    p = _mk_file(config)
    res = process_file(p, config)  # 既定は CAD/USD 許可
    assert res.outcome == Outcome.WRITTEN


def test_jpy_with_decimal_total_routes_to_review(config, monkeypatch):
    # 日系海外店を JPY と誤判定 (円に小数は無い) → review へ
    _patch_extract(monkeypatch, [_r(merchant="Fujiya", total=10.45)])
    p = _mk_file(config)
    res = process_file(p, config)
    assert res.outcome == Outcome.REVIEW
    assert res.written_rows == 0
    assert any("小数" in x for x in res.reasons)
