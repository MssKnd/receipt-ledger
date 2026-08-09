"""二重取り込みの防止。

MF は「インポートするたびに新しい仕訳が登録されます。登録済みの仕訳を更新する
ことはできません」(公式ガイド) で、同じ内容を二度入れても弾かれない (実測: 同一
内容の CSV を 2 回入れたら取引No 240/241 と 242/243 の 4 取引になった)。防止は
こちら側の責務。

Wise には `CARD_TRANSACTION-<数字>` という完全な一意キーがあるので、日付と
金額と店名で突き合わせるレシート側の dedup より確実に判定できる。取り込み済みの
判定は**生成した wise-*.csv 自体**を台帳として行う。別の状態ファイルを持つと
CSV と食い違ったときにどちらが正かが分からなくなるため。ID は仕訳メモ列に
書き込まれる。
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

# parse.Kind の値に対応する ID の形。仕訳メモの中から拾う。
_TXN_ID = re.compile(r"\b(?:CARD_TRANSACTION|TRANSFER)-\d+\b")

WISE_CSV_GLOB = "wise-*.csv"


def txn_ids_in_memo(memo: str) -> set[str]:
    return set(_TXN_ID.findall(memo or ""))


def imported_txn_ids(csv_dir: Path) -> set[str]:
    """生成済みの wise-*.csv に既に載っている Wise 取引 ID。

    月ファイルを跨いで集める。Wise のエクスポートは数か月分を含むので、
    当月+前月しか見ないレシート側の dedup とは範囲が違う。
    """
    found: set[str] = set()
    if not csv_dir.exists():
        return found
    for path in sorted(csv_dir.glob(WISE_CSV_GLOB)):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                found |= txn_ids_in_memo(row.get("仕訳メモ", ""))
    return found
