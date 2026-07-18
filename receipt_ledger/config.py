"""設定の読み込み。

環境固有の値 (勘定科目ホワイトリスト・貸方・ntfy・Ollama URL・ディレクトリ) は
TOML ファイルで管理し、環境変数で上書きできる。デプロイ側 (NixOS module) は
この TOML を生成して RECEIPT_LEDGER_CONFIG で指すだけでよい。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AccountRule:
    """借方勘定科目ホワイトリストの 1 エントリ。"""

    account: str  # MF に実在する勘定科目名 (例: 会議費)
    sub_account: str = ""  # 補助科目 (任意)
    hints: tuple[str, ...] = ()  # この科目に寄せる判定ヒント語 (例: 飲食, カフェ)


@dataclass(frozen=True)
class Directories:
    inbox: Path
    processed: Path
    failed: Path
    review: Path
    csv: Path

    def all(self) -> list[Path]:
        return [self.inbox, self.processed, self.failed, self.review, self.csv]


@dataclass(frozen=True)
class Config:
    directories: Directories

    # 貸方は固定 (法人: 立替を役員から借りた形)
    credit_account: str = "短期借入金"
    credit_sub_account: str = ""  # 立替者名など。デプロイ側で設定する

    # 借方推定
    accounts: tuple[AccountRule, ...] = ()
    fallback_account: str = "雑費"

    # 確信度がこれ未満なら failed/ に隔離
    min_confidence: float = 0.55
    # 明細合計と総額の許容誤差 (割合)。税端数を吸収
    checksum_tolerance: float = 0.05

    # Ollama
    ollama_base_url: str = "http://127.0.0.1:11435"
    ollama_model: str = "qwen2.5vl:7b"
    ollama_timeout_s: int = 300
    # コンテキスト長。Ollama 既定の 4096 だと画像トークン+複数レシートの
    # JSON 出力で途中切断され、スキーマ不一致 (failed) になる。
    ollama_num_ctx: int = 16384
    # 画像の長辺 px。複数レシート1枚撮りは大きくすると読み分けが改善する
    # (遅くなる)。
    image_max_edge: int = 2000

    # 為替
    # 自動換算を許す通貨。行動圏の通貨だけに絞ると、VL モデルの通貨誤判定
    # (例: BC 州のレシートを USD と読む) が誤レートで CSV に載る事故を防げる。
    # リスト外の換算可能通貨は review/ に隔離して人手確認。
    fx_currencies: tuple[str, ...] = ("CAD", "USD")
    frankfurter_base_url: str = "https://api.frankfurter.app"
    fx_cache_path: Path = Path("/data/nobackup/receipts/.fx-cache.json")

    # 通知 (未設定なら no-op → ログのみ)
    ntfy_url: str = ""
    ntfy_token: str = ""

    # SMB 書き込み中のファイルを拾わないためのサイズ安定待ち
    stable_seconds: float = 5.0
    stable_checks: int = 3

    # 取り込む拡張子
    allowed_suffixes: tuple[str, ...] = (
        ".jpg",
        ".jpeg",
        ".png",
        ".heic",
        ".heif",
        ".pdf",
        ".webp",
    )

    def account_names(self) -> list[str]:
        return [r.account for r in self.accounts]


def _dirs_from(base: Path) -> Directories:
    return Directories(
        inbox=base / "inbox",
        processed=base / "processed",
        failed=base / "failed",
        review=base / "review",
        csv=base / "csv",
    )


def load_config(path: str | os.PathLike | None = None) -> Config:
    """TOML から Config を組み立てる。

    優先順位: 引数 path > 環境変数 RECEIPT_LEDGER_CONFIG > 既定値のみ。
    さらに一部キーは環境変数で上書きできる (systemd から注入しやすいように)。
    """
    data: dict = {}
    cfg_path = path or os.environ.get("RECEIPT_LEDGER_CONFIG")
    if cfg_path:
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)

    base = Path(
        os.environ.get("RECEIPT_LEDGER_BASE")
        or data.get("base_dir")
        or "/data/nobackup/receipts"
    )
    dirs = _dirs_from(base)

    accounts = tuple(
        AccountRule(
            account=a["account"],
            sub_account=a.get("sub_account", ""),
            hints=tuple(a.get("hints", [])),
        )
        for a in data.get("accounts", [])
    )

    def s(key: str, default: str) -> str:
        return str(os.environ.get(f"RECEIPT_LEDGER_{key.upper()}", data.get(key, default)))

    def f(key: str, default: float) -> float:
        return float(os.environ.get(f"RECEIPT_LEDGER_{key.upper()}", data.get(key, default)))

    def i(key: str, default: int) -> int:
        return int(os.environ.get(f"RECEIPT_LEDGER_{key.upper()}", data.get(key, default)))

    return Config(
        directories=dirs,
        credit_account=s("credit_account", "短期借入金"),
        credit_sub_account=s("credit_sub_account", ""),
        accounts=accounts,
        fallback_account=s("fallback_account", "雑費"),
        min_confidence=f("min_confidence", 0.55),
        checksum_tolerance=f("checksum_tolerance", 0.05),
        ollama_base_url=s("ollama_base_url", "http://127.0.0.1:11435"),
        ollama_model=s("ollama_model", "qwen2.5vl:7b"),
        ollama_timeout_s=i("ollama_timeout_s", 300),
        ollama_num_ctx=i("ollama_num_ctx", 16384),
        image_max_edge=i("image_max_edge", 2000),
        fx_currencies=tuple(data.get("fx_currencies", ["CAD", "USD"])),
        frankfurter_base_url=s("frankfurter_base_url", "https://api.frankfurter.app"),
        fx_cache_path=Path(s("fx_cache_path", str(base / ".fx-cache.json"))),
        ntfy_url=s("ntfy_url", ""),
        ntfy_token=s("ntfy_token", ""),
        stable_seconds=f("stable_seconds", 5.0),
        stable_checks=i("stable_checks", 3),
    )
