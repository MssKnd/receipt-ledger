"""セグメンテーション (複数レシート写真の分割) のテスト。

合成画像: 暗い背景に白い矩形 = レシート、で輪郭検出の基本動作を確認する。
"""

from PIL import Image, ImageDraw

from receipt_ledger.segment import split_receipts


def _photo(rects, size=(1600, 900), bg=(60, 45, 35)):
    """暗背景 + 白矩形の合成写真。rects は (x, y, w, h) のリスト。"""
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    for x, y, w, h in rects:
        d.rectangle([x, y, x + w, y + h], fill=(245, 245, 240))
        # レシートらしく印字風の線を描く (単色塗りつぶしすぎ対策ではなく見た目)
        for ty in range(y + 20, y + h - 10, 28):
            d.line([x + 15, ty, x + w - 15, ty], fill=(80, 80, 80), width=3)
    return img


def test_three_receipts_are_split():
    img = _photo([(60, 60, 300, 700), (450, 80, 320, 650), (860, 70, 300, 720)])
    crops = split_receipts(img)
    assert len(crops) == 3
    for c in crops:
        # 縦長に正規化され、レシートより極端に小さくない
        assert c.height >= c.width
        assert c.width > 200 and c.height > 500


def test_single_receipt_falls_back():
    img = _photo([(400, 60, 400, 780)])
    assert split_receipts(img) == []


def test_bright_background_falls_back():
    # 白背景に白レシート — 前景分離できないので分割しない
    img = _photo([(60, 60, 300, 700), (450, 80, 320, 650)], bg=(250, 250, 250))
    assert split_receipts(img) == []


def test_rotated_receipts_are_deskewed():
    base = _photo([(500, 100, 320, 640)], size=(1300, 900))
    rotated = base.rotate(12, fillcolor=(60, 45, 35))
    img = Image.new("RGB", (2600, 900), (60, 45, 35))
    img.paste(base, (0, 0))
    img.paste(rotated, (1300, 0))
    crops = split_receipts(img)
    assert len(crops) == 2
    for c in crops:
        assert c.height >= c.width


def _vertical_fused_pair():
    """縦積みで密着した 2 枚 (境界は数 px の隙間のみ)。CLOSE で 1 blob に融合する。"""
    img = Image.new("RGB", (900, 1400), (60, 45, 35))
    d = ImageDraw.Draw(img)
    d.rectangle([100, 60, 800, 660], fill=(245, 245, 240))
    d.rectangle([100, 664, 800, 1264], fill=(245, 245, 240))
    return img


def test_vertical_fused_pair_falls_back_in_normal_mode():
    # 縦積み融合はエスカレーションしない (接写の誤分割対策) → 丸ごと処理
    assert split_receipts(_vertical_fused_pair()) == []


def test_vertical_fused_pair_splits_in_refine_mode():
    # refine (クロップ再分割) は向き不問で Canny エスカレーション → 2 枚に割れる
    subs = split_receipts(_vertical_fused_pair(), refine=True)
    assert len(subs) == 2


def test_refine_drops_edge_sliver():
    # クロップ端に隣のレシートの細い写り込み → refine は本体 2 枚だけ返す
    img = _photo(
        [(60, 60, 500, 700), (620, 60, 500, 700), (1180, 60, 40, 700)],
        size=(1260, 830),
    )
    assert len(split_receipts(img, refine=True)) == 2
