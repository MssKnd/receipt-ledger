"""Ollama VL モデルによるレシート抽出。

Ollama の `/api/chat` に画像 (base64) と `format` (JSON schema) を渡し、
`Extraction` に厳密パースする。structured output なのでパース失敗は構造的に
起きにくいが、モデルがスキーマを外した場合に備えて ValidationError は上位へ。
"""

from __future__ import annotations

import json
from pathlib import Path

import requests
from pydantic import ValidationError

from .config import Config
from .images import to_base64_images
from .models import Extraction, extraction_json_schema


class ExtractError(RuntimeError):
    """抽出の失敗の基底。"""


class ExtractTransientError(ExtractError):
    """一時障害 (Ollama 不達・5xx・応答が JSON でない)。RETRY → inbox に残す。"""


class ExtractPermanentError(ExtractError):
    """恒久障害 (壊れた画像・4xx=モデル名誤り・スキーマ不一致)。failed/ へ。"""

_SYSTEM_PROMPT = (
    "あなたは日本の経理担当者向けのレシート読み取り専用アシスタントです。"
    "画像に写っているレシート・領収書を正確に読み取り、指定された JSON スキーマで"
    "返してください。推測で数字を作らないこと。読めない項目は null か空にすること。"
)

_USER_PROMPT = (
    "この画像に写っているすべてのレシート/領収書を読み取ってください。"
    "1 枚の画像に複数のレシートがある場合は、それぞれ別の要素として receipts 配列に入れてください。"
    "各レシートについて、店名・住所・取引日・通貨・支払総額・税率別内訳・明細・"
    "税の名称ラベル (GST, PST, 消費税 など印字どおり)・インボイス登録番号を読み取ってください。"
    "通貨は、日本語や円なら JPY としてください。"
    "住所にカナダの州・都市 (BC, ON, Vancouver, Toronto など) がある、"
    "または GST/HST/PST の記載があれば、$ 表記でも必ず CAD としてください。"
    "USD は米国の住所や US$ が明記されている場合だけです。判別できなければ OTHER。"
    "金額はレシートに書かれた通貨単位の数値そのままにしてください (換算しない)。"
    "total は実際に支払った金額 (チップが加算されていればチップ込み) を入れ、"
    "チップ (Tip/Gratuity) の記載があれば tip_amount にも入れてください。"
)


def extract(path: Path, config: Config) -> Extraction:
    # 画像の読み込み失敗 = 壊れた/非対応ファイル = 恒久障害。
    try:
        images = to_base64_images(path, config.image_max_edge)
    except Exception as exc:  # PIL の UnidentifiedImageError, PDF 変換失敗など
        raise ExtractPermanentError(f"画像を読めない: {exc}") from exc
    return extract_images(images, config)


def extract_images(images: list[str], config: Config) -> Extraction:
    """base64 画像リストを 1 回の抽出として Ollama に投げる。

    セグメンテーション後のクロップ単位抽出はこちらを直接呼ぶ。"""
    payload = {
        "model": config.ollama_model,
        "format": extraction_json_schema(),
        "stream": False,
        "options": {"temperature": 0, "num_ctx": config.ollama_num_ctx},
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_PROMPT, "images": images},
        ],
    }

    # 接続不能・タイムアウト = 一時障害 (Ollama ホストのスリープ等)。
    try:
        resp = requests.post(
            f"{config.ollama_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=config.ollama_timeout_s,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise ExtractTransientError(f"Ollama 不達: {exc}") from exc
    except requests.RequestException as exc:
        raise ExtractTransientError(f"Ollama 通信エラー: {exc}") from exc

    # フォワーダ越し構成では Ollama ホストのダウン中に 502/503 が出るのが最頻の一時障害。
    if resp.status_code >= 500:
        raise ExtractTransientError(f"Ollama サーバエラー {resp.status_code}")
    if resp.status_code >= 400:
        # モデル名の間違いなど設定ミス。リトライで直らない。
        raise ExtractPermanentError(f"Ollama が {resp.status_code} を返した")

    # 応答が JSON でない (HTML エラーページを 200 で返す等) は一時障害扱い。
    try:
        content = resp.json()["message"]["content"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ExtractTransientError(f"Ollama 応答が不正: {exc}") from exc

    # ここまで来て中身がスキーマに合わない = モデルの出力不良 = 恒久障害。
    try:
        return Extraction.model_validate_json(_strip_content(content))
    except (ValidationError, ValueError) as exc:
        raise ExtractPermanentError(f"抽出結果がスキーマ不一致: {exc}") from exc


def _strip_content(content: str) -> str:
    """まれにモデルが ```json フェンスを付けるので剥がす。"""
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
    # 念のため最初の { から最後の } まで
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1:
        content = content[start : end + 1]
    json.loads(content)  # 妥当性の早期チェック
    return content
