"""extract のエラー分類 (一時=RETRY / 恒久=failed) のテスト。

requests.post とイメージ変換をモックして、HTTP ステータス・応答形状ごとに
正しい例外型が出ることを確認する。"""

from pathlib import Path

import pytest
import requests

import receipt_ledger.extract as extract_mod
from receipt_ledger.extract import (
    ExtractPermanentError,
    ExtractTransientError,
    extract,
)


class _Resp:
    def __init__(self, status=200, payload=None, json_raises=False):
        self.status_code = status
        self._payload = payload
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def config(tmp_path):
    from receipt_ledger.config import Config, Directories

    d = Directories(
        inbox=tmp_path, processed=tmp_path, failed=tmp_path, review=tmp_path, csv=tmp_path
    )
    return Config(directories=d, ollama_model="m", ollama_base_url="http://x")


@pytest.fixture(autouse=True)
def _fake_images(monkeypatch):
    monkeypatch.setattr(extract_mod, "to_base64_images", lambda path, max_edge=2000: ["b64"])


def _ok_content(monkeypatch, resp):
    monkeypatch.setattr(extract_mod.requests, "post", lambda *a, **k: resp)


def test_connection_error_is_transient(config, monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(extract_mod.requests, "post", boom)
    with pytest.raises(ExtractTransientError):
        extract(Path("x.jpg"), config)


def test_5xx_is_transient(config, monkeypatch):
    _ok_content(monkeypatch, _Resp(status=502))
    with pytest.raises(ExtractTransientError):
        extract(Path("x.jpg"), config)


def test_4xx_is_permanent(config, monkeypatch):
    _ok_content(monkeypatch, _Resp(status=404))  # モデル名間違い等
    with pytest.raises(ExtractPermanentError):
        extract(Path("x.jpg"), config)


def test_non_json_response_is_transient(config, monkeypatch):
    # フォワーダが HTML エラーページを 200 で返す等
    _ok_content(monkeypatch, _Resp(status=200, json_raises=True))
    with pytest.raises(ExtractTransientError):
        extract(Path("x.jpg"), config)


def test_bad_schema_is_permanent(config, monkeypatch):
    # 200 で JSON だが中身がスキーマ不一致 (receipts が配列でない)
    payload = {"message": {"content": '{"receipts": "not-a-list"}'}}
    _ok_content(monkeypatch, _Resp(status=200, payload=payload))
    with pytest.raises(ExtractPermanentError):
        extract(Path("x.jpg"), config)


def test_bad_image_is_permanent(config, monkeypatch):
    def boom(path, max_edge=2000):
        raise OSError("broken image")

    monkeypatch.setattr(extract_mod, "to_base64_images", boom)
    with pytest.raises(ExtractPermanentError):
        extract(Path("x.jpg"), config)


def test_happy_path_parses(config, monkeypatch):
    payload = {
        "message": {
            "content": '{"receipts": [{"merchant": "店", "total": 100, "currency": "JPY"}]}'
        }
    }
    _ok_content(monkeypatch, _Resp(status=200, payload=payload))
    result = extract(Path("x.jpg"), config)
    assert len(result.receipts) == 1
    assert result.receipts[0].merchant == "店"
