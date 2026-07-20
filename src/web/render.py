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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import (
    BackgroundPolicy,
    ColorPolicy,
    Configuration,
)
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
    doc:給使用者下載的 DXF(平面圖含 A3 競賽圖框+標題欄)。
    preview_doc:給瀏覽器預覽的版本(平面圖「去掉圖框」,讓平面本身放大填滿
      畫面、看得更清楚;None 表示跟 doc 同一份 = 剖面/立面本來就沒圖框)。
    """

    label: str
    kind: str
    filename: str
    doc: "ezdxf.document.Drawing"
    preview_doc: Optional["ezdxf.document.Drawing"] = None


def _new_doc():
    doc = new_document()
    return doc, apply_standard(doc, load_standard())


def build_sheets(building: BuildingSpec) -> list[Sheet]:
    """整棟樓 → 圖紙清單(由下層到上層,多層樓附剖面+立面)。"""
    sheets: list[Sheet] = []
    for fl in building.floors:
        doc, layers = _new_doc()
        # DXF:含圖框 + 圖面表格(面積計算表/門窗表,E4)。
        draw_floor_plan(doc.modelspace(), replace(fl.spec, schedules=True), layers)
        # 預覽:同一份平面圖但拿掉 A3 圖框/標題欄 → 平面本身填滿畫面看得更清楚。
        pdoc, players = _new_doc()
        draw_floor_plan(pdoc.modelspace(),
                        replace(fl.spec, sheet=False, title_block=None), players)
        sheets.append(Sheet(label=fl.label, kind="floor",
                            filename=f"{fl.label}.dxf", doc=doc, preview_doc=pdoc))

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


# 預覽用繪圖設定:把「出圖線粗」放大顯示。真實建築線粗(牆 0.35mm、尺寸線
# 0.15mm…)是給印在 A3 紙上看的,瀏覽器把整張圖縮到螢幕時,那些線只剩不到
# 半個像素 → 糊成看不見(使用者反映「看不清楚」)。AutoCAD 靠「線寬顯示」補償,
# 這裡用 lineweight_scaling 做同一件事:等比例放大線粗(牆仍比尺寸線粗),
# 黑底貼近 CAD 深色畫面。40 倍是實測「清楚但不糊成一團」的值。
_SVG_CONFIG = Configuration(
    lineweight_scaling=40,
    background_policy=BackgroundPolicy.BLACK,
)

# 預覽用替代字型——⚠️ default.yaml 的 STRUCT 樣式用「標楷體」kaiu.ttf(給
# AutoCAD/競賽出圖),但 ezdxf 把 kaiu.ttf 的複雜筆畫中文字(樓梯間/衛浴/
# 客廳…)轉成 SVG 路徑時,字形輪廓會自我相交、破碎成「打勾/裂開」的樣子
# (kaiu.ttf 本身的字型檔問題,只在向量路徑轉換時出現,AutoCAD 原生渲染不會
# 犯這個錯,使用者截圖抓到的就是這個)。實測換成微軟正黑體 msjh.ttc 完全乾淨。
# 只覆寫「送去轉 SVG 的那份文件」的 STRUCT 樣式字型,不動 default.yaml——
# DXF 下載檔仍是標楷體(競賽規範/AutoCAD 顯示都正常,不受影響)。
_PREVIEW_FONT = "msjh.ttc"


@contextmanager
def _preview_font(doc):
    """暫時把 STRUCT 樣式字型換成 msjh.ttc(向量轉換用),離開時還原。

    kaiu.ttf 在「字形→向量路徑」轉換時複雜筆畫中文會破碎(SVG 與 PDF 的
    渲染路徑相同),msjh.ttc 乾淨;DXF 下載檔不受影響(try/finally 還原)。
    """
    struct_style = doc.styles.get("STRUCT")
    original_font = struct_style.dxf.font if struct_style is not None else None
    if struct_style is not None:
        struct_style.dxf.font = _PREVIEW_FONT
    try:
        yield
    finally:
        if struct_style is not None:
            struct_style.dxf.font = original_font


def doc_to_svg(doc) -> str:
    """ezdxf 文件 → SVG 字串(黑底、線粗放大,像 AutoCAD 那樣清楚可讀)。

    Page(0, 0) = 紙張大小自動貼合圖面內容,瀏覽器端再自由縮放。

    ⚠️ 剖面/立面的 Sheet 沒有獨立的 preview_doc,SVG 轉換跟 DXF 下載共用同一份
    doc——字型覆寫用完就還原,不管呼叫順序(先存檔或先轉 SVG)都不會讓下載
    的 DXF 意外變成 msjh.ttc。
    """
    with _preview_font(doc):
        backend = SVGBackend()
        Frontend(RenderContext(doc), backend, config=_SVG_CONFIG).draw_layout(
            doc.modelspace(), finalize=True)
        return backend.get_string(Page(0, 0))


# PDF 出圖設定:白底黑線(印表機/正式交件慣例——紙是白的,線要黑才清楚),
# 線粗適度放大(A3 頁面上跟瀏覽器同樣有「細線縮到看不見」的問題,取 20 倍
# 折衷:牆線清楚、尺寸線不糊)。
_PDF_CONFIG = Configuration(
    lineweight_scaling=20,
    background_policy=BackgroundPolicy.WHITE,
    color_policy=ColorPolicy.BLACK,
)

_A3_INCHES = (420 / 25.4, 297 / 25.4)      # A3 橫式(matplotlib 用英吋)


def docs_to_pdf(docs: list, path) -> None:
    """多份 ezdxf 文件 → 一本 A3 橫式 PDF 圖冊(一份一頁,白底黑線可直接列印)。

    用下載版 doc(平面圖含圖框/標題欄/表格)——PDF 就是「印出來的正式圖」。
    網頁端「點了才做」:從已存檔的 DXF 重新讀回來渲染(見 app.py /pdf 端點),
    生成當下不用等 PDF。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    with PdfPages(path) as pdf:
        for doc in docs:
            fig = plt.figure(figsize=_A3_INCHES)
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_axis_off()
            with _preview_font(doc):
                Frontend(RenderContext(doc), MatplotlibBackend(ax),
                         config=_PDF_CONFIG).draw_layout(
                    doc.modelspace(), finalize=True)
            pdf.savefig(fig, facecolor="white")
            plt.close(fig)


def sheets_to_pdf(sheets: list[Sheet], path) -> None:
    """整套 Sheet → PDF 圖冊(docs_to_pdf 的便利包裝)。"""
    docs_to_pdf([s.doc for s in sheets], path)


def sheet_svg(sheet: Sheet) -> str:
    """一張圖的預覽 SVG——平面圖用去圖框的 preview_doc,其餘用 doc。"""
    return doc_to_svg(sheet.preview_doc or sheet.doc)
