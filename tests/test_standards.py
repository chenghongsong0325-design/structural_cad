"""繪圖標準系統的單元測試(依競賽「圖層規定」)。

驗證重點:
  1. YAML 能被正確解析成 Standard 物件。
  2. 競賽規範的 10 個圖層:代碼、顏色、線型、出圖線粗都符合規定。
  3. 套用到文件後,圖層屬性(含 lineweight)、文字型、標註型正確。
  4. 別名(layer_aliases):語意代碼(COLUMN/A-WALL…)對應到規範圖層(COL/WALL…)。
  5. 「同一套標準、套到不同樓層前綴」會產生正確、互不衝突的圖層名。

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
    return load_standard()


@pytest.fixture()
def doc():
    return new_document()


# ---------------------------------------------------------------------------
# 1) 解析設定檔
# ---------------------------------------------------------------------------
def test_load_standard_basics(standard: Standard) -> None:
    assert standard.name == "default"
    assert standard.units == "MM"
    assert standard.ltscale == 30
    assert standard.layer_separator == "$0$"
    assert standard.beam_section_format == "{width}×{depth}"


def test_has_all_spec_layers(standard: Standard) -> None:
    codes = {ly.code for ly in standard.layers}
    spec = {
        "TEXT", "DIM", "DW", "HIDDEN", "COL",
        "WALL", "HANDRAIL", "BORDER", "ARCH", "OTHER",
    }
    assert spec <= codes


def test_spec_layer_colors(standard: Standard) -> None:
    by_code = {ly.code: ly for ly in standard.layers}
    assert by_code["TEXT"].color == 1       # 紅
    assert by_code["DIM"].color == 4        # 青
    assert by_code["DW"].color == 3         # 綠
    assert by_code["HIDDEN"].color == 5     # 藍
    assert by_code["COL"].color == 6        # 洋紅
    assert by_code["WALL"].color == 2       # 黃
    assert by_code["HANDRAIL"].color == 9
    assert by_code["BORDER"].color == 70
    assert by_code["ARCH"].color == 10
    assert by_code["OTHER"].color == 7      # 白/黑


def test_spec_layer_linetypes(standard: Standard) -> None:
    by_code = {ly.code: ly for ly in standard.layers}
    assert by_code["HIDDEN"].linetype == "HIDDEN"
    assert by_code["BORDER"].linetype == "PHANTOM"
    assert by_code["ARCH"].linetype == "CENTER"
    assert by_code["WALL"].linetype == "CONTINUOUS"


def test_spec_layer_lineweights(standard: Standard) -> None:
    """出圖線粗(1/100 mm):0.25→25、0.15→15、0.35→35、0.50→50。"""
    by_code = {ly.code: ly for ly in standard.layers}
    assert by_code["TEXT"].lineweight == 25
    assert by_code["DIM"].lineweight == 15
    assert by_code["COL"].lineweight == 50
    assert by_code["WALL"].lineweight == 35
    assert by_code["OTHER"].lineweight == 15


# ---------------------------------------------------------------------------
# 2) layer_name 純函式
# ---------------------------------------------------------------------------
def test_layer_name_without_prefix() -> None:
    assert layer_name("COL") == "COL"


def test_layer_name_with_prefix() -> None:
    assert layer_name("COL", "2F建築底圖") == "2F建築底圖$0$COL"


def test_layer_name_custom_separator() -> None:
    assert layer_name("COL", "3F", separator="-") == "3F-COL"


# ---------------------------------------------------------------------------
# 3) 套用到文件(無前綴)
# ---------------------------------------------------------------------------
def test_apply_creates_spec_layers(doc, standard: Standard) -> None:
    created = apply_standard(doc, standard)
    assert created["COL"] == "COL"
    for code in ("TEXT", "DIM", "DW", "HIDDEN", "COL", "WALL", "HANDRAIL", "BORDER", "ARCH", "OTHER"):
        assert code in doc.layers


def test_apply_sets_color_and_linetype(doc, standard: Standard) -> None:
    apply_standard(doc, standard)
    assert doc.layers.get("COL").color == 6
    assert doc.layers.get("WALL").color == 2
    assert doc.layers.get("DIM").color == 4
    assert doc.layers.get("HIDDEN").dxf.linetype == "HIDDEN"
    assert doc.layers.get("BORDER").dxf.linetype == "PHANTOM"
    assert doc.layers.get("ARCH").dxf.linetype == "CENTER"


def test_apply_sets_lineweight(doc, standard: Standard) -> None:
    apply_standard(doc, standard)
    assert doc.layers.get("COL").dxf.lineweight == 50
    assert doc.layers.get("WALL").dxf.lineweight == 35
    assert doc.layers.get("TEXT").dxf.lineweight == 25
    assert doc.layers.get("DIM").dxf.lineweight == 15


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
    assert dim.dxf.dimtxsty == "STRUCT"


# ---------------------------------------------------------------------------
# 4) 別名:語意代碼 → 規範圖層
# ---------------------------------------------------------------------------
def test_aliases_resolve_to_spec_layers(doc, standard: Standard) -> None:
    created = apply_standard(doc, standard)
    assert created["COLUMN"] == created["COL"]    # 柱
    assert created["A-WALL"] == created["WALL"]   # 牆
    assert created["A-DOOR"] == created["DW"]     # 門 → 開口
    assert created["A-GLAZ"] == created["DW"]     # 窗 → 開口
    assert created["A-TEXT"] == created["TEXT"]   # 建築文字
    assert created["S-TEXTB"] == created["TEXT"]  # 結構文字
    assert created["S-THIN"] == created["OTHER"]  # 細線 → 其他


def test_aliases_follow_prefix(doc, standard: Standard) -> None:
    created = apply_standard(doc, standard, prefix="2F建築底圖")
    assert created["COLUMN"] == "2F建築底圖$0$COL"
    assert created["A-WALL"] == "2F建築底圖$0$WALL"


# ---------------------------------------------------------------------------
# 5) 樓層前綴 + 多樓層
# ---------------------------------------------------------------------------
def test_apply_with_prefix(doc, standard: Standard) -> None:
    created = apply_standard(doc, standard, prefix="2F建築底圖")
    full = "2F建築底圖$0$WALL"
    assert created["WALL"] == full
    assert full in doc.layers
    assert "WALL" not in doc.layers
    assert doc.layers.get(full).color == 2


def test_same_standard_multiple_floor_prefixes(doc, standard: Standard) -> None:
    prefixes = ["2F建築底圖", "3F建築底圖", "5F建築底圖"]
    for p in prefixes:
        apply_standard(doc, standard, prefix=p)

    for p in prefixes:
        col = f"{p}$0$COL"
        wall = f"{p}$0$WALL"
        assert col in doc.layers
        assert wall in doc.layers
        assert doc.layers.get(col).color == 6
        assert doc.layers.get(wall).color == 2
