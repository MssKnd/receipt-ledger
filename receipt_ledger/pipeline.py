"""オーケストレーション: 1 ファイル → 抽出 → 検算 → 換算 → 分類 → CSV。

ディレクトリがそのまま状態機械:
  inbox → processed  (全レシート正常に CSV へ)
        → failed     (抽出不可・検算NG・低確信。CSV へは書かない)
        → review     (重複疑い。良い行は CSV へ、ファイルは人手確認へ)
  (Ollama/為替が一時的に不達なら inbox に残して次回リトライ = RETRY)
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from .classify import classify
from .config import Config
from .csvwriter import JournalRow, append_rows
from .dedup import is_duplicate
from .extract import ExtractPermanentError, ExtractTransientError, extract
from .fx import FxPermanentError, FxTransientError, to_jpy, yen
from .models import FX_CONVERTIBLE, Currency, Receipt
from .validate import validate_receipt

log = logging.getLogger("receipt_ledger.pipeline")


class Outcome(str, Enum):
    WRITTEN = "written"
    REVIEW = "review"
    FAILED = "failed"
    RETRY = "retry"  # 一時障害。inbox に残す
    EMPTY = "empty"  # レシートが見つからない


class PermanentReceiptError(Exception):
    """このレシートは自動処理できない (判別不能通貨など)。failed/ へ隔離。"""


class ReviewReceiptError(Exception):
    """自動処理は危険だが読めてはいる (換算許可外の通貨など)。review/ へ隔離。"""


@dataclass
class FileResult:
    path: Path
    outcome: Outcome
    written_rows: int = 0
    reasons: list[str] = field(default_factory=list)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _build_row(receipt: Receipt, config: Config, source_name: str) -> tuple[JournalRow, bool, list[str]]:
    """(行, 要確認か, notes) を返す。通貨換算・分類まで済ませる。

    為替の一時障害は FxTransientError、対応外通貨は PermanentReceiptError を投げる
    (どちらも呼び出し側が捕捉して RETRY / failed に振り分ける)。"""
    txn_date = _parse_date(receipt.date)  # 検算済みなので None ではない
    notes: list[str] = []

    if receipt.currency == Currency.JPY:
        if abs(receipt.total - round(receipt.total)) > 1e-9:
            # 円に小数は無い。日系の海外店 (Fujiya 等) を JPY と誤判定して
            # 外貨額をそのまま円として記帳する事故を防ぐ。
            raise ReviewReceiptError(
                f"JPY なのに総額が小数 (通貨誤判定の疑い): "
                f"{receipt.merchant} {receipt.total:g}"
            )
        amount_jpy = yen(receipt.total)
        origin = None  # 金額列と同じ情報なのでメモには書かない
    elif receipt.currency in FX_CONVERTIBLE:
        if receipt.currency.value not in config.fx_currencies:
            # 換算はできるが許可リスト外 (通貨誤判定の疑い)。誤レートで
            # CSV に載せるより人手確認が安全。
            raise ReviewReceiptError(
                f"通貨 {receipt.currency.value} は自動換算の許可リスト外 "
                f"(誤判定の疑い): {receipt.merchant} {receipt.total:g}"
            )
        amount_jpy, fx = to_jpy(
            receipt.total,
            txn_date,
            receipt.currency.value,
            config.fx_cache_path,
            config.frankfurter_base_url,
        )
        origin = f"{receipt.currency.value} {receipt.total:g} @{fx.rate:g}"
        if fx.rate_date != txn_date.isoformat():
            # 休日フォールバックで取引日とレート日がずれた場合だけ根拠を残す
            origin += f" (レート日 {fx.rate_date})"
    else:
        # OTHER (通貨を判別できなかった) は換算しようがない → 人手確認。
        raise PermanentReceiptError(
            f"通貨を判別できず換算不能: {receipt.merchant} {receipt.total:g}"
        )

    cls = classify(receipt, config)
    notes.extend(cls.review_notes)

    detail_parts = ([origin] if origin else []) + [f"src:{source_name}"]
    if receipt._currency_note:
        detail_parts.append(receipt._currency_note)
    if receipt.invoice_registration_number:
        detail_parts.append(f"インボイス:{receipt.invoice_registration_number}")
    if cls.needs_review:
        detail_parts.append("要確認[" + "; ".join(cls.review_notes) + "]")

    row = JournalRow(
        txn_date=txn_date,
        debit_account=cls.debit_account,
        debit_sub_account=cls.debit_sub_account,
        debit_tax_category=cls.tax_category,
        amount_jpy=amount_jpy,
        credit_account=config.credit_account,
        credit_sub_account=config.credit_sub_account,
        merchant=receipt.merchant,
        summary_detail=" ".join(detail_parts),
    )
    return row, cls.needs_review, notes


def _move(path: Path, dest_dir: Path, subdir_year: date | None = None) -> Path:
    target_dir = dest_dir
    if subdir_year is not None:
        target_dir = dest_dir / f"{subdir_year:%Y}"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / path.name
    # 同名衝突を避ける
    n = 1
    while dest.exists():
        dest = target_dir / f"{path.stem}__{n}{path.suffix}"
        n += 1
    return Path(shutil.move(str(path), str(dest)))


def process_file(path: Path, config: Config) -> FileResult:
    """1 ファイルを処理する。副作用: CSV 追記とファイル移動。

    書き込みは 2 フェーズ: (1) 全レシートの換算・分類を終えて行を確定し、
    (2) 重複でない行だけをまとめて追記する。為替の一時障害は必ず 1 行も書く前に
    起きるので、RETRY 後の再実行で二重計上しない。"""
    dirs = config.directories

    # --- 抽出 (Ollama) ---
    try:
        extraction = extract(path, config)
    except ExtractTransientError as exc:
        log.warning("Ollama 一時障害、リトライに回す: %s (%s)", path.name, exc)
        return FileResult(path, Outcome.RETRY, reasons=[str(exc)])
    except ExtractPermanentError as exc:
        log.error("抽出失敗 (恒久): %s (%s)", path.name, exc)
        _move(path, dirs.failed)
        return FileResult(path, Outcome.FAILED, reasons=[f"抽出失敗: {exc}"])

    receipts = extraction.receipts
    if not receipts:
        _move(path, dirs.failed)
        return FileResult(path, Outcome.EMPTY, reasons=["レシートを検出できなかった"])

    # --- 検算: 1 つでも NG ならファイルごと failed に (CSV へは何も書かない) ---
    fail_reasons: list[str] = []
    for i, r in enumerate(receipts, 1):
        v = validate_receipt(r, config)
        if not v.ok:
            fail_reasons.extend(f"レシート{i}: {reason}" for reason in v.reasons)
    if fail_reasons:
        _move(path, dirs.failed)
        return FileResult(path, Outcome.FAILED, reasons=fail_reasons)

    # --- フェーズ 1: 全行を確定 (換算・分類)。ここでは 1 行も書かない ---
    built: list[tuple[JournalRow, bool, list[str]]] = []
    try:
        for r in receipts:
            built.append(_build_row(r, config, path.name))
    except FxTransientError as exc:
        # 為替 API が一時的に不達 → まだ何も書いていないので安全にリトライ。
        log.warning("為替一時障害、リトライに回す: %s (%s)", path.name, exc)
        return FileResult(path, Outcome.RETRY, reasons=[str(exc)])
    except (FxPermanentError, PermanentReceiptError) as exc:
        # 対応外通貨など。何度やっても直らないので failed へ。
        log.error("換算不能 (恒久): %s (%s)", path.name, exc)
        _move(path, dirs.failed)
        return FileResult(path, Outcome.FAILED, reasons=[str(exc)])
    except ReviewReceiptError as exc:
        # まだ 1 行も書いていない。ファイルごと review へ回して人手確認。
        log.warning("要人手確認 (換算せず): %s (%s)", path.name, exc)
        _move(path, dirs.review)
        return FileResult(path, Outcome.REVIEW, reasons=[str(exc)])

    # --- フェーズ 2: 重複を除いて、残りをまとめて追記 ---
    all_notes: list[str] = []
    review_reasons: list[str] = []
    rows_to_write: list[JournalRow] = []
    for row, _needs_review, notes in built:
        all_notes.extend(notes)
        if is_duplicate(dirs.csv, row.txn_date, row.amount_jpy, row.merchant):
            review_reasons.append(
                f"重複疑い: {row.merchant} {row.txn_date:%Y/%m/%d} {row.amount_jpy}円"
            )
            continue
        rows_to_write.append(row)

    if rows_to_write:
        append_rows(dirs.csv, rows_to_write)
    written = len(rows_to_write)

    if review_reasons:
        _move(path, dirs.review)
        return FileResult(
            path, Outcome.REVIEW, written_rows=written, reasons=review_reasons + all_notes
        )

    _move(path, dirs.processed, subdir_year=_parse_date(receipts[0].date))
    return FileResult(path, Outcome.WRITTEN, written_rows=written, reasons=all_notes)


def _is_stable(path: Path, config: Config, sleep=time.sleep) -> bool:
    """SMB 書き込み途中を避ける: サイズが stable_checks 回連続で不変なら安定。"""
    try:
        last = path.stat().st_size
    except FileNotFoundError:
        return False
    stable = 0
    while stable < config.stable_checks:
        sleep(config.stable_seconds)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last:
            stable += 1
        else:
            stable = 0
            last = size
    return True


def iter_inbox(config: Config):
    """処理対象のファイルを列挙 (隠しファイル・非対象拡張子を除く)。"""
    inbox = config.directories.inbox
    if not inbox.exists():
        return
    for p in sorted(inbox.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in config.allowed_suffixes:
            continue
        yield p


def scan(config: Config, sleep=time.sleep) -> list[FileResult]:
    """inbox を 1 巡処理して結果リストを返す。"""
    results: list[FileResult] = []
    for path in iter_inbox(config):
        if not _is_stable(path, config, sleep=sleep):
            log.info("まだ書き込み中/消失: %s", path.name)
            continue
        log.info("処理開始: %s", path.name)
        try:
            results.append(process_file(path, config))
        except Exception as exc:  # 想定外は failed に隔離して継続
            log.exception("想定外エラー: %s", path.name)
            try:
                _move(path, config.directories.failed)
            except Exception:
                pass
            results.append(FileResult(path, Outcome.FAILED, reasons=[f"想定外: {exc}"]))
    return results
