"""出圖清單(E1)—— BuildingSpec → 一層一張圖(DXF + SVG 雙格式)。

網頁要的東西跟命令列不一樣:
  * DXF 給使用者下載回 AutoCAD 用(跟以前一樣)。
  * SVG 給瀏覽器直接顯示——SVG 是向量格式,跟 DXF 一樣「放大不會糊」,
    瀏覽器原生看得懂,不用裝任何外掛。

比照 building_generator.main 的施工圖慣例:平面圖一層一張,多層樓再
加一張剖面、一張立面。

典型用法::

    from src.web.render import build_sheets, doc_to_svg

    sheets = build_sheets(building)        # [Sheet("B1F"), Sheet("1F"), …]
    svg_text = doc_to_svg(sheets[0].doc)   # "<svg …>…</svg>"
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.layout import Page
from ezdxf.addons.drawing.svg import SVGBackend

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.building_generator import BuildingSpec
from src.drafting.apartment_plan import draw_floor_plan
from src.drafting.section import draw_elevation, draw_section
from src.standards.loader import apply_standard, load_standard, new_document


@dataclass
class Sheet:
    """一張圖 = 顯示名稱 + 種類 + 畫好的 ezdxf 文件。

    label:頁籤/檔名用(1F、B1F、剖面、立面)。
    kind:floor(平面)/ section(剖面)/ elevation(立面),前端分組用。
    filename:下載檔名(ASCII,剖面/立面用英文避免瀏覽器編碼問題)。
    """

    label: str
    kind: str
    filename: str
    doc: "ezdxf.document.Drawing"


def _new_doc():
    doc = new_document()
    return doc, apply_standard(doc, load_standard())


def build_sheets(building: BuildingSpec) -> list[Sheet]:
    """整棟樓 → 圖紙清單(由下層到上層,多層樓附剖面+立面)。"""
    sheets: list[Sheet] = []
    for fl in building.floors:
        doc, layers = _new_doc()
        draw_floor_plan(doc.modelspace(), fl.spec, layers)
        sheets.append(Sheet(label=fl.label, kind="floor",
                            filename=f"{fl.label}.dxf", doc=doc))

    if len(building.floors) > 1:      # 單層樓疊不出剖面/立面,跳過
        doc, layers = _new_doc()
        draw_section(doc.modelspace(), building, layers, axis="x",
                     title="剖面圖 A-A")
        sheets.append(Sheet(label="剖面", kind="section",
                            filename="section.dxf", doc=doc))

        doc, layers = _new_doc()
        draw_elevation(doc.modelspace(), building, layers, side="south",
                       title="南向立面圖")
        sheets.append(Sheet(label="立面", kind="elevation",
                            filename="elevation.dxf", doc=doc))
    return sheets


def doc_to_svg(doc) -> str:
    """ezdxf 文件 → SVG 字串(黑底,模擬 CAD 深色畫面,同 scripts/preview.py)。

    Page(0, 0) = 紙張大小自動貼合圖面內容,瀏覽器端再自由縮放。
    """
    backend = SVGBackend()
    Frontend(RenderContext(doc), backend).draw_layout(doc.modelspace(),
                                                      finalize=True)
    return backend.get_string(Page(0, 0))
