"""為替換算 (Frankfurter API, ECB 公表レート)。

- 取引日 (レシート日付) のレートを使う。
- Frankfurter は指定日にデータが無い (土日祝) 場合、直前の営業日のレートを返し、
  レスポンスの `date` に実際に使った日付が入る。この日付を換算根拠として残す。
- 取得済みレートは (通貨ペア, 要求日付) をキーにローカル JSON へキャッシュ。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import requests


@dataclass(frozen=True)
class FxResult:
    rate: float  # 1 単位の外貨 = rate 円
    rate_date: str  # 実際に適用されたレートの日付 (YYYY-MM-DD)
    source: str  # "cache" | "frankfurter"


class FxError(RuntimeError):
    """為替換算の失敗の基底。"""


class FxTransientError(FxError):
    """一時的な失敗 (接続不能・5xx)。リトライで回復し得る → inbox に残す。"""


class FxPermanentError(FxError):
    """恒久的な失敗 (対応外通貨の 4xx・レスポンス構造欠落)。

    何度リトライしても直らないので failed/ に隔離する。"""


def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True))
    tmp.replace(path)


def get_rate(
    on: date,
    frm: str,
    to: str,
    cache_path: Path,
    base_url: str = "https://api.frankfurter.app",
    session: requests.Session | None = None,
) -> FxResult:
    """`on` 日付での frm→to レートを返す。"""
    if frm == to:
        return FxResult(rate=1.0, rate_date=on.isoformat(), source="cache")

    key = f"{frm}:{to}:{on.isoformat()}"
    cache = _load_cache(cache_path)
    if key in cache:
        c = cache[key]
        return FxResult(rate=c["rate"], rate_date=c["rate_date"], source="cache")

    getter = (session or requests).get
    url = f"{base_url.rstrip('/')}/{on.isoformat()}"

    # 一時障害 (接続不能・タイムアウト・5xx) と恒久障害 (対応外通貨の 4xx・
    # レスポンス構造欠落) を分けて投げる。前者は RETRY、後者は failed。
    try:
        resp = getter(url, params={"from": frm, "to": to}, timeout=30)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise FxTransientError(f"為替 API 接続不能 {frm}->{to} {on}: {exc}") from exc
    except requests.RequestException as exc:
        raise FxTransientError(f"為替取得失敗 {frm}->{to} {on}: {exc}") from exc

    status = getattr(resp, "status_code", 200)
    if status >= 500:
        raise FxTransientError(f"為替 API サーバエラー {status} {frm}->{to} {on}")
    if status >= 400:
        # 対応外の通貨や不正な日付など。リトライしても直らない。
        raise FxPermanentError(f"為替 API が {status} を返した {frm}->{to} {on}")

    try:
        body = resp.json()
        rate = float(body["rates"][to])
        rate_date = body.get("date", on.isoformat())
    except (KeyError, ValueError, TypeError) as exc:
        raise FxPermanentError(f"為替レスポンスが不正 {frm}->{to} {on}: {exc}") from exc

    cache[key] = {"rate": rate, "rate_date": rate_date}
    _save_cache(cache_path, cache)
    return FxResult(rate=rate, rate_date=rate_date, source="frankfurter")


def yen(amount: float, rate: float = 1.0) -> int:
    """金額 × レートを円 (整数) に丸める。会計なので四捨五入 (ROUND_HALF_UP)。"""
    value = Decimal(str(amount)) * Decimal(str(rate))
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_jpy(amount: float, on: date, currency: str, cache_path: Path, base_url: str) -> tuple[int, FxResult]:
    """外貨金額を円 (整数, 四捨五入) に換算し、根拠 FxResult を返す。"""
    fx = get_rate(on, currency, "JPY", cache_path, base_url)
    return yen(amount, fx.rate), fx
