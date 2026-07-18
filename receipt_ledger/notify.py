"""ntfy への通知。URL 未設定なら no-op (ログのみ)。"""

from __future__ import annotations

import logging
from email.header import Header

import requests

from .config import Config

log = logging.getLogger("receipt_ledger.notify")


def _header_value(text: str) -> str:
    """HTTP ヘッダは非 ASCII を安全に運べないので、非 ASCII は RFC 2047 で
    エンコードする (ntfy の Title は日本語を含むため)。"""
    if text.isascii():
        return text
    return Header(text, "utf-8").encode()


def notify(config: Config, title: str, message: str, priority: str = "default", tags: str = "") -> None:
    if not config.ntfy_url:
        log.info("[notify:noop] %s — %s", title, message)
        return
    headers = {"Title": _header_value(title), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if config.ntfy_token:
        headers["Authorization"] = f"Bearer {config.ntfy_token}"
    try:
        requests.post(
            config.ntfy_url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as exc:  # 通知失敗で本処理を止めない
        log.warning("ntfy 通知に失敗: %s", exc)
