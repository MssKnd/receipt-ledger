from __future__ import annotations

from pathlib import Path

import pytest

from receipt_ledger.config import AccountRule, Config, Directories


@pytest.fixture
def config(tmp_path: Path) -> Config:
    base = tmp_path / "receipts"
    dirs = Directories(
        inbox=base / "inbox",
        processed=base / "processed",
        failed=base / "failed",
        review=base / "review",
        csv=base / "csv",
    )
    for d in dirs.all():
        d.mkdir(parents=True, exist_ok=True)
    return Config(
        directories=dirs,
        accounts=(
            AccountRule("会議費", hints=("カフェ", "コーヒー", "スターバックス", "食事")),
            AccountRule("新聞図書費", hints=("書店", "書籍", "本")),
            AccountRule("旅費交通費", hints=("タクシー", "交通", "JR", "電車")),
        ),
        fallback_account="雑費",
        fx_cache_path=base / ".fx-cache.json",
        min_confidence=0.55,
        checksum_tolerance=0.05,
    )
