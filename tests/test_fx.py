from datetime import date

import pytest
import requests

from receipt_ledger.fx import (
    FxPermanentError,
    FxTransientError,
    get_rate,
    yen,
)


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload, status=200, raise_exc=None):
        self.payload = payload
        self.status = status
        self.raise_exc = raise_exc
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        self.last_url = url
        self.last_params = params
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResp(self.payload, self.status)


def test_get_rate_hits_api_and_caches(tmp_path):
    cache = tmp_path / "fx.json"
    sess = _FakeSession({"date": "2024-05-10", "rates": {"JPY": 114.5}})
    r1 = get_rate(date(2024, 5, 10), "CAD", "JPY", cache, session=sess)
    assert r1.rate == 114.5
    assert r1.source == "frankfurter"
    assert sess.calls == 1

    # 2 回目はキャッシュ (API を叩かない)
    r2 = get_rate(date(2024, 5, 10), "CAD", "JPY", cache, session=sess)
    assert r2.source == "cache"
    assert sess.calls == 1


def test_weekend_fallback_uses_returned_date(tmp_path):
    cache = tmp_path / "fx.json"
    # 5/11 は土曜。Frankfurter は 5/10 のレートを date で返す
    sess = _FakeSession({"date": "2024-05-10", "rates": {"JPY": 114.5}})
    r = get_rate(date(2024, 5, 11), "CAD", "JPY", cache, session=sess)
    assert r.rate_date == "2024-05-10"
    assert sess.last_url.endswith("/2024-05-11")


def test_same_currency_is_identity(tmp_path):
    r = get_rate(date(2024, 5, 10), "JPY", "JPY", tmp_path / "fx.json")
    assert r.rate == 1.0


def test_to_jpy_rounds(tmp_path, monkeypatch):
    import receipt_ledger.fx as fx

    def fake_get_rate(on, frm, to, cache_path, base_url="", session=None):
        from receipt_ledger.fx import FxResult

        return FxResult(rate=107.5, rate_date="2024-05-10", source="cache")

    monkeypatch.setattr(fx, "get_rate", fake_get_rate)
    jpy, res = fx.to_jpy(12.34, date(2024, 5, 10), "CAD", tmp_path / "fx.json", "")
    assert jpy == 1327  # 12.34 * 107.5 = 1326.55 → ROUND_HALF_UP
    assert res.rate == 107.5


def test_yen_round_half_up():
    # 会計の四捨五入: .5 は切り上げ (banker's rounding ではない)
    assert yen(0.5) == 1
    assert yen(2.5) == 3
    assert yen(100.4) == 100
    assert yen(10.0, 10.05) == 101  # 100.5 → 101


def test_connection_error_is_transient(tmp_path):
    sess = _FakeSession(None, raise_exc=requests.ConnectionError("refused"))
    with pytest.raises(FxTransientError):
        get_rate(date(2024, 5, 10), "CAD", "JPY", tmp_path / "fx.json", session=sess)


def test_5xx_is_transient(tmp_path):
    sess = _FakeSession({}, status=503)
    with pytest.raises(FxTransientError):
        get_rate(date(2024, 5, 10), "CAD", "JPY", tmp_path / "fx.json", session=sess)


def test_4xx_is_permanent(tmp_path):
    # 対応外通貨などで Frankfurter が 404
    sess = _FakeSession({}, status=404)
    with pytest.raises(FxPermanentError):
        get_rate(date(2024, 5, 10), "XYZ", "JPY", tmp_path / "fx.json", session=sess)


def test_missing_rate_key_is_permanent(tmp_path):
    sess = _FakeSession({"date": "2024-05-10", "rates": {}}, status=200)
    with pytest.raises(FxPermanentError):
        get_rate(date(2024, 5, 10), "CAD", "JPY", tmp_path / "fx.json", session=sess)
