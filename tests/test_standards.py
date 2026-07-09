"""繪圖標準系統的單元測試。

驗證重點:
  1. YAML 能被正確解析成 Standard 物件。
  2. 套用到 ezdxf 文件後,圖層/文字型/標註型的屬性確實符合設定。
  3. 「同一套標準、套到不同樓層前綴」會產生正確的、互不衝突的圖層名。

執行:  .venv\\Scripts\\python.exe -m pytest -v
"""
from __future__ import annotations

import ezdxf
import pytest

from src.standards.loader import (
    Standard,
    apply_standard,
    layer_name,
    load_standard,
    new_document,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def standard() -> Standard:
    """讀取專案預設標準檔一次,給整個模組共用。"""
    return load_standard()


@pytest.fixture()
def doc():
    """每個測試給一份乾淨的、含標準線型的新文件。"""
    return new_document()


# ---------------------------------------------------------------------------
# 1) 解析設定檔
# ---------------------------------------------------------------------------
def test_load_standard_basics(standard: Standard) -> None:
    assert standard.name == "default"
    assert standard.units == "MM"
    assert standard.ltscale == 30
    assert standard.layer_separator == "$0$"
    # 梁斷面標示格式慣例有被保留下來。
    assert standard.beam_section_format == "{width}×{depth}"


def test_load_standard_has_all_layers(standard: Standard) -> None:
    codes = {ly.code for ly in standard.layers}
    expected = {
        "COLUMN", "S-RCBMB", "S-RCBMG", "S-SLAB", "S-SLAB2",
        "S-HATCH", "S-HATCH2", "S-TEXTB", "S-TEXTG", "S-THIN",
        "S-LEADER", "S-CLOUD", "DIM", "AXIS",
    }
    assert expected <= codes


def test_layer_colors_and_linetypes(standard: Standard) -> None:
    by_code = {ly.code: ly for ly in standard.layers}
    assert by_code["COLUMN"].color == 1       # 紅
    assert by_code["S-SLAB"].color == 140     # 青
    assert by_code["S-SLAB2"].color == 8      # 灰
    assert by_code["S-LEADER"].color == 2     # 黃
    assert by_code["DIM"].color == 5          # 藍
    # 軸線用 CENTER 線型,其餘結構圖層用實線。
    assert by_code["AXIS"].linetype == "CENTER"
    assert by_code["COLUMN"].linetype == "CONTINUOUS"


# ---------------------------------------------------------------------------
# 2) layer_name 純函式
# ---------------------------------------------------------------------------
def test_layer_name_without_prefix() -> None:
    assert layer_name("COLUMN") == "COLUMN"


def test_layer_name_with_prefix() -> None:
    assert layer_name("COLUMN", "2F建築底圖") == "2F建築底圖$0$COLUMN"


def test_layer_name_custom_separator() -> None:
    assert layer_name("COLUMN", "3F", separator="-") == "3F-COLUMN"


# ---------------------------------------------------------------------------
# 3) 套用到文件(無前綴)
# ---------------------------------------------------------------------------
def test_apply_creates_layers_without_prefix(doc, standard: Standard) -> None:
    created = apply_standard(doc, standard)  # prefix=""

    # 無前綴時,圖層名 == 代碼。
    assert created["COLUMN"] == "COLUMN"
    assert "COLUMN" in doc.layers
    assert "S-SLAB" in doc.layers
    assert "AXIS" in doc.layers


def test_apply_sets_layer_color_and_linetype(doc, standard: Standard) -> None:
    apply_standard(doc, standard)

    assert doc.layers.get("COLUMN").color == 1
    assert doc.layers.get("S-SLAB2").color == 8
    assert doc.layers.get("DIM").color == 5
    # 軸線圖層拿到 CENTER 線型。
    assert doc.layers.get("AXIS").dxf.linetype == "CENTER"


def test_apply_sets_units_and_ltscale(doc, standard: Standard) -> None:
    apply_standard(doc, standard)
    assert doc.units == ezdxf.units.MM
    assert doc.header["$LTSCALE"] == 30


def test_apply_creates_text_style(doc, standard: Standard) -> None:
    apply_standard(doc, standard)
    assert "STRUCT" in doc.styles
    assert doc.styles.get("STRUCT").dxf.font == "kaiu.ttf"


def test_apply_creates_dim_style(doc, standard: Standard) -> None:
    apply_standard(doc, standard)
    assert "STRUCT" in doc.dimstyles
    dim = doc.dimstyles.get("STRUCT")
    assert dim.dxf.dimtxt == 250
    assert dim.dxf.dimasz == 250
    assert dim.dxf.dimdec == 0
    # 標註引用的文字型正確。
    assert dim.dxf.dimtxsty == "STRUCT"


# ---------------------------------------------------------------------------
# 4) 套用到文件(有前綴 + 多樓層)
# ---------------------------------------------------------------------------
def test_apply_with_prefix(doc, standard: Standard) -> None:
    created = apply_standard(doc, standard, prefix="2F建築底圖")

    full = "2F建築底圖$0$COLUMN"
    assert created["COLUMN"] == full
    assert full in doc.layers
    # 加了前綴後,不應該存在「裸代碼」的圖層。
    assert "COLUMN" not in doc.layers
    # 顏色等屬性不因前綴改變。
    assert doc.layers.get(full).color == 1


def test_same_standard_multiple_floor_prefixes(doc, standard: Standard) -> None:
    """同一套標準,套到 2F / 3F / 5F 三個樓層前綴,彼此不衝突且屬性一致。"""
    prefixes = ["2F建築底圖", "3F建築底圖", "5F建築底圖"]

    for p in prefixes:
        apply_standard(doc, standard, prefix=p)

    # 三個樓層各自有一套完整圖層。
    for p in prefixes:
        col = f"{p}$0$COLUMN"
        slab = f"{p}$0$S-SLAB"
        assert col in doc.layers
        assert slab in doc.layers
        # 同一代碼在不同樓層的顏色一致(標準共用)。
        assert doc.layers.get(col).color == 1
        assert doc.layers.get(slab).color == 140

    # 總圖層數 = 標準圖層數 × 樓層數(不含文件內建的 "0"、"Defpoints" 等)。
    n_std = len(standard.layers)
    for p in prefixes:
        present = sum(1 for ly in standard.layers if f"{p}$0${ly.code}" in doc.layers)
        assert present == n_std
