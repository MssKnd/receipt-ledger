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
from .extract import ExtractPermanentError, ExtractTransientError, extract_images
from .fx import FxPermanentError, FxTransientError, to_jpy, yen
from .images import encode_image, to_base64_images
from .models import FX_CONVERTIBLE, Currency, Receipt
from .segment import split_receipts
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


@dataclass(eq=False)
class _Unit:
    """抽出の単位。クロップ 1 枚、または丸ごと 1 ファイル。"""

    label: str  # "" = 丸ごと、"r1"… = クロップ、"r1a"… = 再分割クロップ
    b64: list[str]
    crop: object | None = None  # PIL.Image (クロップ時のみ)。隔離保存に使う
    refined: bool = False  # 再分割済み (これ以上再分割しない)

    def source_name(self, path: Path) -> str:
        return f"{path.name}#{self.label}" if self.label else path.name


def _to_units(path: Path, config: Config) -> list[_Unit]:
    """ファイルを抽出ユニットに分解する。

    画像 (非 PDF) はセグメンテーションを試み、2 枚以上のレシート領域が
    検出できたらクロップ単位。それ以外は従来どおり丸ごと 1 ユニット。"""
    if config.image_segmentation and path.suffix.lower() != ".pdf":
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            crops = split_receipts(ImageOps.exif_transpose(img))
        if len(crops) >= 2:
            return [
                _Unit(f"r{i}", [encode_image(c, config.image_max_edge)], crop=c)
                for i, c in enumerate(crops, 1)
            ]
    return [_Unit("", to_base64_images(path, config.image_max_edge))]


def _refine_unit(u: _Unit, config: Config) -> list[_Unit] | None:
    """抽出・検算に失敗したクロップの再分割 (融合クロップの自己修復)。

    クロップに 2 枚のレシートが融合したまま残ると、VL モデルが隣の
    レシートと明細・総額を混線させて検算 NG になる (実測: 密着した 2 枚で
    片方の明細+もう片方の総額が 1 レシートに合成された)。失敗したクロップに
    限り向き不問のエスカレーション付き再分割を試し、2 枚以上に割れたら
    サブクロップとして抽出し直す。再帰は 1 段のみ。"""
    if u.refined or u.crop is None:
        return None
    subs = split_receipts(u.crop, refine=True)
    if len(subs) < 2:
        return None
    log.info("%s: 融合疑いのクロップを %d 枚に再分割してリトライ", u.label, len(subs))
    return [
        _Unit(
            f"{u.label}{chr(96 + i)}",  # r3 → r3a, r3b, …
            [encode_image(s, config.image_max_edge)],
            crop=s,
            refined=True,
        )
        for i, s in enumerate(subs, 1)
    ]


def _save_crop(crop, dest_dir: Path, stem: str, label: str) -> None:
    """隔離用にクロップ画像を書き出す (同名衝突は連番)。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem}-{label}.jpg"
    n = 1
    while dest.exists():
        dest = dest_dir / f"{stem}-{label}__{n}.jpg"
        n += 1
    crop.save(dest, format="JPEG", quality=90)


def process_file(path: Path, config: Config) -> FileResult:
    """1 ファイルを処理する。副作用: CSV 追記とファイル移動。

    書き込みは 2 フェーズ: (1) 全ユニットの抽出・検算・換算・分類を終えて
    行を確定し、(2) 重複でない行だけをまとめて追記する。一時障害 (Ollama/
    為替) は必ず 1 行も書く前に起きるので、RETRY 後の再実行で二重計上しない。

    複数レシート写真はクロップ単位のユニットに分割され、恒久的に処理できない
    ユニットはクロップ画像として failed//review/ に隔離される (正常ユニットは
    巻き添えにならない)。丸ごと 1 ユニットのときは従来どおりファイル自体を
    failed//review/ に移す。"""
    dirs = config.directories

    # --- ユニット分解 (画像を読めない = 恒久障害) ---
    try:
        units = _to_units(path, config)
    except Exception as exc:  # PIL の UnidentifiedImageError, PDF 変換失敗など
        log.error("画像を読めない: %s (%s)", path.name, exc)
        _move(path, dirs.failed)
        return FileResult(path, Outcome.FAILED, reasons=[f"画像を読めない: {exc}"])
    single = len(units) == 1

    # --- 抽出 + 検算 + 行確定 (ユニット毎)。一時障害は即 RETRY (書き込みゼロ) ---
    unit_fail: list[tuple[_Unit, list[str]]] = []
    unit_review: list[tuple[_Unit, list[str]]] = []
    built: list[tuple[_Unit, list[tuple[JournalRow, bool, list[str]]]]] = []
    empty = False
    queue = list(units)
    while queue:
        u = queue.pop(0)
        prefix = f"{u.label}: " if u.label else ""
        try:
            receipts = extract_images(u.b64, config).receipts
        except ExtractTransientError as exc:
            log.warning("Ollama 一時障害、リトライに回す: %s (%s)", path.name, exc)
            return FileResult(path, Outcome.RETRY, reasons=[str(exc)])
        except ExtractPermanentError as exc:
            if subs := _refine_unit(u, config):
                queue.extend(subs)
                continue
            unit_fail.append((u, [f"{prefix}抽出失敗: {exc}"]))
            continue
        if not receipts:
            if subs := _refine_unit(u, config):
                queue.extend(subs)
                continue
            unit_fail.append((u, [f"{prefix}レシートを検出できなかった"]))
            empty = True
            continue

        reasons = []
        for i, r in enumerate(receipts, 1):
            v = validate_receipt(r, config)
            if not v.ok:
                reasons.extend(f"{prefix}レシート{i}: {x}" for x in v.reasons)
        if reasons:
            if subs := _refine_unit(u, config):
                queue.extend(subs)
                continue
            unit_fail.append((u, reasons))
            continue

        try:
            rows = [_build_row(r, config, u.source_name(path)) for r in receipts]
        except FxTransientError as exc:
            # 為替 API が一時的に不達 → まだ何も書いていないので安全にリトライ。
            log.warning("為替一時障害、リトライに回す: %s (%s)", path.name, exc)
            return FileResult(path, Outcome.RETRY, reasons=[str(exc)])
        except (FxPermanentError, PermanentReceiptError) as exc:
            unit_fail.append((u, [f"{prefix}{exc}"]))
            continue
        except ReviewReceiptError as exc:
            unit_review.append((u, [f"{prefix}{exc}"]))
            continue
        built.append((u, rows))

    # --- フェーズ 2: 重複を除いて、残りをまとめて追記 ---
    all_notes: list[str] = []
    rows_to_write: list[JournalRow] = []
    for u, rows in built:
        dup_reasons = []
        for row, _needs_review, notes in rows:
            all_notes.extend(notes)
            if is_duplicate(dirs.csv, row.txn_date, row.amount_jpy, row.merchant):
                dup_reasons.append(
                    f"重複疑い: {row.merchant} {row.txn_date:%Y/%m/%d} {row.amount_jpy}円"
                )
                continue
            rows_to_write.append(row)
        if dup_reasons:
            unit_review.append((u, dup_reasons))

    if rows_to_write:
        append_rows(dirs.csv, rows_to_write)
    written = len(rows_to_write)

    fail_reasons = [r for _, rs in unit_fail for r in rs]
    review_reasons = [r for _, rs in unit_review for r in rs]

    # --- 隔離とファイル移動 ---
    if single:
        # 従来セマンティクス: ファイル自体を failed / review / processed へ。
        if fail_reasons:
            _move(path, dirs.failed)
            outcome = Outcome.EMPTY if empty else Outcome.FAILED
            return FileResult(path, outcome, written_rows=written, reasons=fail_reasons)
        if review_reasons:
            _move(path, dirs.review)
            return FileResult(
                path, Outcome.REVIEW, written_rows=written,
                reasons=review_reasons + all_notes,
            )
        _move(path, dirs.processed, subdir_year=built[0][1][0][0].txn_date)
        return FileResult(path, Outcome.WRITTEN, written_rows=written, reasons=all_notes)

    # 複数ユニット: 不良クロップだけを隔離し、元ファイルは processed へ。
    for u, _ in unit_fail:
        _save_crop(u.crop, dirs.failed, path.stem, u.label)
    for u, _ in unit_review:
        _save_crop(u.crop, dirs.review, path.stem, u.label)
    year = built[0][1][0][0].txn_date if built else date.today()
    _move(path, dirs.processed, subdir_year=year)

    if fail_reasons:
        outcome = Outcome.FAILED
    elif review_reasons:
        outcome = Outcome.REVIEW
    else:
        outcome = Outcome.WRITTEN
    return FileResult(
        path, outcome, written_rows=written, reasons=fail_reasons + review_reasons + all_notes
    )


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
