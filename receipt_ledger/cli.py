"""コマンドライン入口。

  receipt-ledger scan            inbox を 1 巡処理 (systemd から呼ぶ本命)
  receipt-ledger process FILE    単一ファイルを処理 (デバッグ用)
  receipt-ledger extract FILE    抽出結果 JSON を表示 (モデル確認用)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from .config import load_config
from .extract import extract
from .notify import notify
from .pipeline import Outcome, process_file, scan


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _summarize(results) -> str:
    counts = Counter(r.outcome for r in results)
    written = sum(r.written_rows for r in results)
    parts = [f"{o.value}:{counts[o]}" for o in Outcome if counts[o]]
    return f"{len(results)}件処理 / {written}行追記 / " + (", ".join(parts) or "変化なし")


# 人手の確認が要る結果 (通知の詳細に出す + high 優先度にする)。
_ATTENTION = (Outcome.FAILED, Outcome.REVIEW, Outcome.EMPTY)
# RETRY は詳細には出すが (滞留に気づけるように)、優先度は上げない (一時障害は
# 次回スキャンで自然回復し得るため通知疲れを避ける)。
_DETAIL = _ATTENTION + (Outcome.RETRY,)


def _notify_summary(config, results) -> None:
    if not results:
        return
    counts = Counter(r.outcome for r in results)
    attention = sum(counts[o] for o in _ATTENTION)
    summary = _summarize(results)
    detail_lines = [
        f"[{r.outcome.value}] {r.path.name}: {'; '.join(r.reasons)}"
        for r in results
        if r.outcome in _DETAIL
    ]
    message = summary
    if detail_lines:
        message += "\n" + "\n".join(detail_lines)
    priority = "high" if attention else "low"
    tags = "warning" if attention else "receipt"
    notify(config, "レシート処理", message, priority=priority, tags=tags)


def cmd_scan(args) -> int:
    config = load_config(args.config)
    results = scan(config)
    print(_summarize(results))
    _notify_summary(config, results)
    return 0


def cmd_process(args) -> int:
    config = load_config(args.config)
    result = process_file(Path(args.file), config)
    print(f"{result.outcome.value} rows={result.written_rows} {'; '.join(result.reasons)}")
    _notify_summary(config, [result])
    return 0 if result.outcome in (Outcome.WRITTEN, Outcome.REVIEW) else 1


def cmd_extract(args) -> int:
    config = load_config(args.config)
    extraction = extract(Path(args.file), config)
    print(json.dumps(extraction.model_dump(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="receipt-ledger")
    p.add_argument("-c", "--config", help="設定 TOML パス (省略時は RECEIPT_LEDGER_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="inbox を 1 巡処理")
    sp.set_defaults(func=cmd_scan)

    pp = sub.add_parser("process", help="単一ファイルを処理")
    pp.add_argument("file")
    pp.set_defaults(func=cmd_process)

    ep = sub.add_parser("extract", help="抽出結果 JSON を表示")
    ep.add_argument("file")
    ep.set_defaults(func=cmd_extract)
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
