"""繪圖標準系統:用設定檔管理圖層/文字型/標註型/線型,依客戶切換。"""
from __future__ import annotations

from .loader import (
    DEFAULT_LAYER_SEPARATOR,
    DEFAULT_STANDARD_PATH,
    DimStyleDef,
    LayerDef,
    LinetypeDef,
    Standard,
    TextStyleDef,
    apply_standard,
    layer_name,
    load_standard,
    new_document,
)

__all__ = [
    "DEFAULT_LAYER_SEPARATOR",
    "DEFAULT_STANDARD_PATH",
    "DimStyleDef",
    "LayerDef",
    "LinetypeDef",
    "Standard",
    "TextStyleDef",
    "apply_standard",
    "layer_name",
    "load_standard",
    "new_document",
]
