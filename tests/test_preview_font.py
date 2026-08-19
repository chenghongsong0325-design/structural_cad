"""預覽字型替身(`src/drafting/preview_font.py`)。

守兩件事:

  1. **轉圖時中文不能是標楷體** —— kaiu.ttf 轉向量路徑會把複雜筆畫的字畫碎
     (使用者截圖抓到「樓梯間」「衛浴」裂開)。
  2. **DXF 下載檔仍然是標楷體** —— 那是競賽/AutoCAD 要的字型,不可以被預覽
     的替身汙染。所以替身一定要還原,而且丟例外也要還原。

⚠️ 這裡不比對畫出來的像素(太脆弱),而是釘住「送去轉圖的那一刻,樣式的字型是誰」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ezdxf
import pytest

from src.drafting.preview_font import PREVIEW_FONT, preview_font
from src.standards.loader import apply_standard, load_standard

KAI = "kaiu.ttf"


def _doc():
    doc = ezdxf.new("R2010", setup=True)
    apply_standard(doc, load_standard())
    return doc


def _font(doc):
    return doc.styles.get("STRUCT").dxf.font


def test_standard_still_ships_kaiu():
    """底線:專案交出去的字型是標楷體。這條先掛,下面兩條才有意義。"""
    assert _font(_doc()) == KAI


def test_inside_the_block_chinese_is_not_kaiu():
    doc = _doc()
    with preview_font(doc):
        assert _font(doc) == PREVIEW_FONT
        assert _font(doc) != KAI


def test_font_is_restored_afterwards():
    """★ 下載的 DXF 不可以變成正黑體。"""
    doc = _doc()
    with preview_font(doc):
        pass
    assert _font(doc) == KAI


def test_font_is_restored_even_if_rendering_blows_up():
    """★ 轉圖失敗(圖太大、後端出錯)也不能把字型留在替身狀態 ——
    Sheet 的 doc 是共用的,下一次存檔就會存錯字型。"""
    doc = _doc()
    with pytest.raises(RuntimeError):
        with preview_font(doc):
            raise RuntimeError("轉圖爆了")
    assert _font(doc) == KAI


def test_document_without_the_struct_style_is_left_alone():
    """手寫的小測試檔沒有 STRUCT 樣式,不該炸掉。"""
    doc = ezdxf.new("R2010")
    with preview_font(doc):
        pass


def test_the_png_preview_script_uses_it():
    """★ 這條是這次 bug 的本體:網頁的 SVG/PDF 有包,PNG 預覽腳本漏了,
    所以人看到的圖中文全是裂的。誰把它拿掉就要紅。"""
    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "preview_plan.py").read_text(encoding="utf-8")
    assert "with preview_font(doc):" in src


def test_the_web_renderer_uses_it_for_both_svg_and_pdf():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "web" / "render.py").read_text(encoding="utf-8")
    assert src.count("with preview_font(doc):") == 2
