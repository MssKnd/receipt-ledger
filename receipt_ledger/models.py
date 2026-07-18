"""VL モデルに抽出させる構造化スキーマ (pydantic)。

このモジュールの pydantic モデルがそのまま Ollama の `format` (JSON schema) に
なる。フィールドの description は日本語で書いておくとモデルの抽出精度が上がる。

重要: 1 枚の画像に複数レシートがあり得るので、トップレベルは `Extraction`
(レシートの配列) にしている。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator


class Currency(str, Enum):
    JPY = "JPY"
    CAD = "CAD"
    USD = "USD"
    OTHER = "OTHER"


# Frankfurter で JPY へ換算できる外貨。OTHER (判別不能) はここに無いので
# 人手確認へ回す (self-heal しない恒久エラー)。
FX_CONVERTIBLE = frozenset({Currency.CAD, Currency.USD})

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month(name: str) -> int | None:
    return _MONTHS.get(name[:3].lower())


def _iso(year: object, month: object, day: object) -> str:
    return f"{int(str(year)):04d}-{int(str(month)):02d}-{int(str(day)):02d}"


def _year(y: str) -> int:
    """2 桁年 (レジ印字の "26" 等) は 2000 年代として解釈する。"""
    n = int(y)
    return n + 2000 if n < 100 else n


# 通貨補正に使うカナダ住所の目印 (州略称・主要都市・国名)。
_CANADA_ADDR = re.compile(
    r"\b(Canada|British Columbia|BC|Vancouver|Burnaby|Richmond|Surrey|Victoria"
    r"|Ontario|Toronto|Alberta|Calgary|Quebec|Montreal)\b",
    re.IGNORECASE,
)


class LineItem(BaseModel):
    """レシート 1 行分の明細。検算 (合計 ≈ 総額) に使う。"""

    description: str = Field(description="品目名。読めない場合は空文字")
    amount: float = Field(description="その品目の金額 (税込か税抜かはレシート表記のまま、通貨単位)")


class TaxBucket(BaseModel):
    """税率別の内訳。国内レシートで 10% / 8% が混在する場合に使う。"""

    rate_percent: float = Field(description="税率(%)。10 または 8。不明なら 0")
    tax_amount: float = Field(description="その税率区分の消費税額。不明なら 0")
    net_amount: float = Field(description="その税率区分の税抜金額。不明なら 0")


class Receipt(BaseModel):
    """1 枚のレシート/領収書。1 レシート = 1 仕訳行に展開される。"""

    merchant: str = Field(description="店名・支払先。読めない場合は 'UNKNOWN'")
    date: Optional[str] = Field(
        default=None,
        description="取引日 (レシート発行日) を YYYY-MM-DD で。読めない場合は null",
    )
    currency: Currency = Field(
        default=Currency.JPY,
        description=(
            "通貨。日本語/円記号なら JPY。カナダ (GST/HST/PST 表記や英語+$) なら CAD。"
            "米国 ($ かつ US 住所/州) なら USD。判別不能なら OTHER"
        ),
    )
    total: float = Field(description="支払総額 (税込、チップも含む実際の支払額)。この通貨単位での金額")
    tip_amount: float = Field(
        default=0.0,
        description="チップ (Tip/Gratuity)。総額に含まれる場合のみ。無ければ 0",
    )
    tax_buckets: list[TaxBucket] = Field(
        default_factory=list,
        description="税率別内訳。読めた範囲で。国外レシートは空でよい",
    )
    line_items: list[LineItem] = Field(
        default_factory=list,
        description="明細行。検算に使うので読める範囲でできるだけ列挙する",
    )
    invoice_registration_number: Optional[str] = Field(
        default=None,
        description="日本のインボイス制度の登録番号 (T + 13 桁)。無ければ null",
    )
    address: str = Field(
        default="",
        description="店舗の住所 (レシートに印字されている範囲で。国・州・都市名を含める)",
    )
    tax_labels: list[str] = Field(
        default_factory=list,
        description="税の名称ラベル。例: GST, PST, HST, Tax, 消費税。印字どおりに",
    )

    # 通貨をコード側で補正したときの根拠 (メモ用)。スキーマには出さない。
    _currency_note: str | None = PrivateAttr(default=None)

    @field_validator("confidence", mode="after")
    @classmethod
    def _normalize_confidence(cls, v: float) -> float:
        """モデルのスケール揺れを 0-1 に寄せる。

        実測: 85.0 (0-100 スケール) と 8.0 (0-10 スケール) の両方が出た。
        1 < v <= 10 は 0-10 スケール、10 < v <= 100 は 0-100 スケールとみなす。"""
        if 10.0 < v <= 100.0:
            return v / 100.0
        if 1.0 < v <= 10.0:
            return v / 10.0
        return v

    @field_validator("date", mode="before")
    @classmethod
    def _normalize_date(cls, v: object) -> object:
        """モデルが返しがちな日付表記を ISO に寄せる。

        `2026年07月15日 12:34` / `2026/7/15` / `01-Feb-2026` / `Feb 10,2026`
        → `2026-07-15` 等。数字だけの `06-11-2026` は月日どちらか判別できない
        限り触らず、validate 側の形式チェックに委ねる (安全側で failed)。"""
        if not isinstance(v, str):
            return v
        m = re.search(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", v)
        if m:
            return _iso(m.group(1), m.group(2), m.group(3))
        # 01-Feb-2026 / 1 Feb 2026 (年は 2 桁もある: "28 Jun 26")
        m = re.search(r"(\d{1,2})[ \-/]([A-Za-z]{3,9})[ \-/,]+(\d{2,4})\b", v)
        if m and _month(m.group(2)):
            return _iso(_year(m.group(3)), _month(m.group(2)), m.group(1))
        # Feb 10, 2026 / Feb 10,2026 / Jun 28, 26
        m = re.search(r"([A-Za-z]{3,9})[ \-/]+(\d{1,2})[ ,/\-]+(\d{2,4})\b", v)
        if m and _month(m.group(1)):
            return _iso(_year(m.group(3)), _month(m.group(1)), m.group(2))
        # 25-12-2026 / 12-25-2026: 13 以上の成分がある場合だけ月日を確定できる。
        # 両方 12 以下の曖昧ケースは _resolve_ambiguous_date (通貨で判断) へ。
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", v)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 and b <= 12:
                return _iso(m.group(3), b, a)
            if b > 12 and a <= 12:
                return _iso(m.group(3), a, b)
        return v

    @model_validator(mode="after")
    def _correct_currency_by_evidence(self) -> "Receipt":
        """レシート上の証拠で通貨判定を補正する。

        日系の海外店 (店名が日本語) を JPY と誤判定する実例があった。
        カナダの税ラベル (GST/PST/HST) やカナダ住所が印字されていれば、
        店名の言語に関係なく CAD である。日付解決 (_resolve_ambiguous_date)
        より先に走る必要があるので定義順をここに置く。"""
        if self.currency == Currency.JPY:
            labels = {label.strip().upper() for label in self.tax_labels}
            evidence = None
            if labels & {"GST", "PST", "HST"}:
                evidence = "税ラベル " + "/".join(sorted(labels & {"GST", "PST", "HST"}))
            elif _CANADA_ADDR.search(self.address or ""):
                evidence = "カナダ住所"
            if evidence:
                object.__setattr__(self, "currency", Currency.CAD)
                self._currency_note = f"通貨を CAD に補正 ({evidence})"
        return self

    @model_validator(mode="after")
    def _resolve_ambiguous_date(self) -> "Receipt":
        """月日が曖昧な数字日付 (06-11-2026) を通貨で解決する。

        日本のレシートは YYYY/MM/DD か和式なので、年が後ろに来る数字だけの
        日付は実質北米のレジ印字 → 外貨レシートに限り北米式 MM-DD-YYYY と
        解釈する。JPY (国内) は判断材料がないので触らず validate で弾く。
        ※ カナダで DD-MM-YYYY を使う店だと月日が入れ替わるリスクは許容
        (同月内なら実害は小さく、要確認は MF 取込時に人が見る)。"""
        if self.date and self.currency != Currency.JPY:
            m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", self.date.strip())
            if m and int(m.group(1)) <= 12 and int(m.group(2)) <= 12:
                object.__setattr__(
                    self, "date", _iso(m.group(3), m.group(1), m.group(2))
                )
        return self
    category_hint: Optional[str] = Field(
        default=None,
        description=(
            "レシート内容から推測される費用の種類を表す短い日本語。"
            "例: 食事, 書籍, 交通, 文房具, 通信。分類の材料に使う"
        ),
    )
    confidence: float = Field(
        default=0.0,
        description="この抽出全体の確信度 0.0〜1.0。数字がぼやけて自信がないほど低く",
    )


class Extraction(BaseModel):
    """画像 1 枚の抽出結果 (レシートの配列)。"""

    receipts: list[Receipt] = Field(
        default_factory=list,
        description="画像内のすべてのレシート。無ければ空配列",
    )


def extraction_json_schema() -> dict:
    """Ollama の `format` に渡す JSON schema を返す。

    全オブジェクトの全プロパティを required にする: Ollama の structured
    output は required でないフィールドを省略でき、その場合 default (null/0.0)
    に落ちる — 実測で date と confidence が常に欠落した。required にしても
    Optional フィールドは anyOf で null を許すので「読めなければ null」は残る。
    """
    schema = Extraction.model_json_schema()

    def _require_all(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                _require_all(v)
        elif isinstance(node, list):
            for v in node:
                _require_all(v)

    _require_all(schema)
    return schema
