"""複数レシート写真のセグメンテーション。

1 枚の写真に複数レシートを詰めると VL モデルが隣のレシートと数値を混線
させたり見落とす (実測: 6 枚詰めで 3/6 正解)。OpenCV の輪郭検出で
レシート単位のクロップに分割し、1 クロップ = 1 抽出にすることで
解像度の希釈・混線・出力途中切断を構造的に避ける。

前提は「暗めの背景 (机など) に明るいレシートを並べた写真」。
それ以外 (白背景・検出 1 枚以下) は空リストを返し、呼び出し側が
従来の丸ごと 1 回抽出にフォールバックする。
"""

from __future__ import annotations

import logging

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger("receipt_ledger.segment")

# 検出用の作業解像度 (長辺)。クロップ自体は元解像度から切り出す。
_DETECT_EDGE = 1200
# レシートとみなす最小面積 (画像全体に対する割合)。
_MIN_AREA_FRAC = 0.02
# 二値化マスクがこれ以上画像を覆う場合は「明るい背景」とみなして諦める
# (背景まで前景判定されており輪郭分離が信用できない)。
_MAX_MASK_FRAC = 0.85
# クロップの余白 (辺の長さに対する割合)。輪郭の食い込みで文字が欠けないように。
_PAD_FRAC = 0.02
# 1 検出領域が画像に占めてよい最大割合。これを超えたら隣接レシートの融合か
# 背景の誤検出とみなす (→ Canny パスへエスカレーション)。
_MAX_RECT_FRAC = 0.5
# パス 2 (Canny 切断) で許す最大領域数。これを超えたら印字を切った断片化。
_MAX_REGIONS = 8


def _detect_rects(m, erode_px: int, extra_pad: int = 0):
    """マスクから (レシート矩形リスト, 融合blobの寸法 or None) を返す。

    巨大領域 (画像の _MAX_RECT_FRAC 超) を見つけたら rects=None と
    その (幅, 高さ) を返し、呼び出し側がエスカレーションを判断する。"""
    erode_k = cv2.getStructuringElement(cv2.MORPH_RECT, (erode_px, erode_px))
    eroded = cv2.erode(m, erode_k)
    contours, _ = cv2.findContours(eroded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = _MIN_AREA_FRAC * m.size
    max_area = _MAX_RECT_FRAC * m.size
    restore = 2 * (erode_px + extra_pad)
    rects = []
    for c in contours:
        if cv2.contourArea(c) < min_area:
            continue
        (cx, cy), (rw, rh), angle = cv2.minAreaRect(c)
        if rw * rh > max_area:
            return None, (rw, rh)
        rects.append(((cx, cy), (rw + restore, rh + restore), angle))
    return rects, None


def split_receipts(img: Image.Image) -> list[Image.Image]:
    """写真をレシート単位のクロップ (傾き補正済み) に分割する。

    2 枚以上を検出できた場合のみクロップのリストを返す。分割しない方が
    よい場合 (検出 0〜1 枚・背景が明るい・輪郭が不明瞭) は [] を返す。
    """
    rgb = np.array(img.convert("RGB"))
    h, w = rgb.shape[:2]
    scale = _DETECT_EDGE / max(h, w)
    small = cv2.resize(rgb, (int(w * scale), int(h * scale))) if scale < 1.0 else rgb

    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    mask_frac = float(np.count_nonzero(mask)) / mask.size
    if mask_frac > _MAX_MASK_FRAC:
        log.info("背景が明るく前景分離できない (mask %.0f%%) — 分割せず丸ごと処理", mask_frac * 100)
        return []

    # レシート内の暗い印字や折り目でマスクが割れるのを閉じる (小さめに —
    # 大きい CLOSE は隣接レシート同士を融合させる)。
    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k)

    # パス 1: erode で細い接続を切る (隣接だが密着していない配置向け)。
    erode_px = max(3, _DETECT_EDGE // 100)
    rects, fused = _detect_rects(mask, erode_px)

    # パス 2: 完全密着で 1 blob に融合した場合、レシート境界の影/エッジを
    # Canny で切ってから再検出する。境界線の無い単独レシートの接写を
    # 刻まないよう、「融合 blob が横長」のときだけ発動する (密着は横並びが
    # 実態。縦積みや接写は従来の丸ごと処理へ)。
    if fused is not None and fused[0] > fused[1]:
        edges = cv2.Canny(blur, 40, 120)
        edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        cut = cv2.bitwise_and(mask, cv2.bitwise_not(edges))
        cut = cv2.morphologyEx(cut, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        rects, fused = _detect_rects(cut, 8, extra_pad=3)
        if rects is not None and len(rects) > _MAX_REGIONS:
            # 断片化しすぎ = 境界でなく印字を切っている。信用しない。
            rects = None

    if rects is None or len(rects) < 2:
        n = len(rects) if rects else 0
        log.info("レシート領域を分割できず (%d 件/融合=%s) — 丸ごと処理", n, fused is not None)
        return []

    # 左上から読む順序に寄せる (中心座標で列→行ソート)。
    rects.sort(key=lambda r: (round(r[0][0] / (mask.shape[1] / 4)), r[0][1]))

    crops = []
    for rect in rects:
        crop = _warp_crop(rgb, rect, 1.0 / scale if scale < 1.0 else 1.0)
        if crop is not None:
            crops.append(Image.fromarray(crop))
    if len(crops) < 2:
        return []
    log.info("%d 枚のレシート領域に分割", len(crops))
    return crops


def _warp_crop(rgb: np.ndarray, rect, upscale: float) -> np.ndarray | None:
    """minAreaRect を元解像度に引き伸ばし、透視変換でデスキューして切り出す。"""
    (cx, cy), (rw, rh), angle = rect
    if rw <= 0 or rh <= 0:
        return None
    box = cv2.boxPoints(((cx * upscale, cy * upscale), (rw * upscale, rh * upscale), angle))

    # boxPoints の 4 点を 左上/右上/右下/左下 に並べ替える。
    s = box.sum(axis=1)
    d = np.diff(box, axis=1).ravel()
    tl, br = box[np.argmin(s)], box[np.argmax(s)]
    tr, bl = box[np.argmin(d)], box[np.argmax(d)]
    src = np.array([tl, tr, br, bl], dtype=np.float32)

    out_w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if out_w < 40 or out_h < 40:
        return None

    pad_x, pad_y = int(out_w * _PAD_FRAC), int(out_h * _PAD_FRAC)
    dst = np.array(
        [
            [pad_x, pad_y],
            [out_w + pad_x, pad_y],
            [out_w + pad_x, out_h + pad_y],
            [pad_x, out_h + pad_y],
        ],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(src, dst)
    # 向きは回さない: 写真が正位置で撮られていればコーナー順序 (tl/tr/br/bl)
    # ベースの warp は文字の向きを保つ。縦長強制の回転は、隣接レシートが
    # 融合した横長クロップで文字を横倒しにし、抽出を壊す実測があった。
    return cv2.warpPerspective(
        rgb, m, (out_w + 2 * pad_x, out_h + 2 * pad_y), borderValue=(255, 255, 255)
    )
