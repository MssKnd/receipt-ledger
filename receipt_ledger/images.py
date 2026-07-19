"""画像の正規化。

iPhone アップロードは HEIC が既定なので必須。PDF (スキャン領収書) も PNG に
落とす。Ollama には base64 の JPEG/PNG を渡す。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

# HEIF/HEIC を PIL で開けるように登録 (import 副作用)。未インストールでも
# JPEG/PNG は扱えるよう、失敗は握りつぶす。
try:  # pragma: no cover - 環境依存
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

from PIL import Image

# 長辺をこの px に縮小 (VL モデルの入力上限と速度のため)。複数レシートを
# 1 枚に詰めた写真は解像度を上げると読み分けが改善する (遅くなる) —
# config の image_max_edge で調整できる。
_MAX_EDGE = 2000


def encode_image(img: Image.Image, max_edge: int = _MAX_EDGE) -> str:
    """PIL Image を base64 JPEG に (クロップなどファイルを経由しない画像用)。"""
    return _encode(img, max_edge)


def _encode(img: Image.Image, max_edge: int = _MAX_EDGE) -> str:
    img = img.convert("RGB")
    w, h = img.size
    scale = max_edge / max(w, h)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def to_base64_images(path: Path, max_edge: int = _MAX_EDGE) -> list[str]:
    """画像/PDF を base64 JPEG のリストに正規化する。

    PDF は 1 ページ = 1 要素。通常の画像は 1 要素。すべてのページを 1 回の
    抽出プロンプトにまとめて渡す (1 ファイル = 1 論理的な提出物として扱う)。
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_to_base64(path, max_edge)
    with Image.open(path) as img:
        # マルチフレーム (稀) は 1 枚目のみ
        return [_encode(img, max_edge)]


def _pdf_to_base64(path: Path, max_edge: int = _MAX_EDGE) -> list[str]:
    from pdf2image import convert_from_path

    pages = convert_from_path(str(path), dpi=200)
    return [_encode(p, max_edge) for p in pages]
