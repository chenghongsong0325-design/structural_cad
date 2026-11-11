from types import SimpleNamespace
from xml.etree import ElementTree as ET
import re

import ezdxf

from src.web.render import build_sheets, doc_to_svg
from src.drafting.apartment_plan import FloorPlanSpec
from src.drafting.room import Room
from src.drafting.wall import Wall
from src.standards.loader import apply_standard, load_standard


def test_white_high_contrast_svg_preserves_download_color_and_font():
    doc = ezdxf.new("R2010", setup=True)
    apply_standard(doc, load_standard())
    doc.styles.new("CJ-FONT", dxfattribs={"font":"Microsoft JhengHei"})
    line = doc.modelspace().add_line((0,0),(4000,12000),dxfattribs={"color":1})
    doc.modelspace().add_text("主臥室樓梯間",dxfattribs={"style":"CJ-FONT","height":200})
    svg = doc_to_svg(doc)
    root = ET.fromstring(svg)
    paper = root.find("{http://www.w3.org/2000/svg}rect")
    assert paper.attrib["fill"].lower() in {"white", "#ffffff"}
    assert "#ff0000" not in svg.lower()
    widths = [float(w) for w in re.findall(r"stroke-width: ([\d.]+)", svg)]
    assert widths and min(widths) >= 700 and max(widths) <= 2000
    assert line.dxf.color == 1
    assert doc.styles.get("CJ-FONT").dxf.font == "Microsoft JhengHei"


def test_floor_preview_omits_outlying_notes_and_tables_but_export_keeps_them():
    points = [(0,0),(4000,0),(4000,12000),(0,12000)]
    spec = FloorPlanSpec(site_boundary=points, setback=0, x_spacings=[4000],y_spacings=[12000],
                         rooms=[Room("主臥室",points,kind="bedroom")],
                         walls=[Wall(a,b) for a,b in zip(points, points[1:]+points[:1])],
                         floor_label="2F", lot_note="基地說明獨立標記", schedules=True)
    sheet = build_sheets(SimpleNamespace(floors=[SimpleNamespace(label="2F",spec=spec)]))[0]
    export_text = [e.dxf.text for e in sheet.doc.modelspace().query("TEXT")]
    preview_text = [e.dxf.text for e in sheet.preview_doc.modelspace().query("TEXT")]
    assert "2F" in export_text and "2F" not in preview_text
    assert any("基地說明獨立標記" in t for t in export_text)
    assert not any("基地說明獨立標記" in t for t in preview_text)
    assert "主臥室" in preview_text
    assert not list(sheet.preview_doc.modelspace().query('LWPOLYLINE[layer=="BORDER"]'))
    assert spec.floor_label == "2F" and spec.schedules and spec.lot_note
