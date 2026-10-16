"""清單順序製圖：共用實際幾何與型號表，模型輸出 cm，紙空間固定 1:50。"""
from __future__ import annotations

import math
from dataclasses import replace

import ezdxf
from ezdxf import bbox, transform
from ezdxf.math import Matrix44
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import Polygon
from shapely.ops import unary_union

from src.design.drawing_profile import exterior_indices, net_rooms
from src.drafting.apartment_plan import build_grid, resolve_columns, draw_under_stair_wc, draw_patio_marks
from src.drafting.balcony_elevator import balcony_walls, elevator_walls, draw_balcony_railing, draw_elevator_symbol
from src.drafting.fixtures import Counter, draw_counter, place_fixture
from src.drafting.gridlines import GridSystem, GridLine, alpha_labels, draw_grid
from src.drafting.label_space import LabelSpace, relax_room_labels, relax_flight_labels, text_box
from src.drafting.members import column_corners
from src.drafting.room import Room
from src.drafting.stair import draw_u_stair
from src.drafting.wall_join import merged_wall_footprint, _iter_polygons
from src.standards.loader import apply_standard, load_standard, new_document


PALETTE = {"CJ-AXIS": (1, 13), "CJ-COLUMN": (2, 13), "CJ-EXT": (2, 13),
           "CJ-INT": (34, 13), "CJ-OUTLINE": (1, 13), "CJ-OPENING": (5, 18),
           "CJ-FURNITURE": (6, 13), "CJ-TEXT": (7, 13), "CJ-DIM": (7, 13)}
PROFILE_MARKER = 105015


def new_profile_doc():
    doc = new_document()
    layers = apply_standard(doc, load_standard())
    for name, (color, weight) in PALETTE.items():
        doc.layers.new(name, dxfattribs={"color": color, "lineweight": weight})
    doc.layers.get("CJ-AXIS").dxf.linetype = "CENTER" if "CENTER" in doc.linetypes else "CONTINUOUS"
    doc.styles.new("CJ-FONT", dxfattribs={"font": "Microsoft JhengHei"})
    layers.update({"AXIS": "CJ-AXIS", "S-TEXTB": "CJ-AXIS", "A-TEXT": "CJ-TEXT",
                   "OTHER": "CJ-FURNITURE", "A-DOOR": "CJ-OPENING", "A-GLAZ": "CJ-OPENING"})
    return doc, layers


def text(msp, value, x, y, height=140, layer="CJ-TEXT", center=False):
    ent = msp.add_text(str(value), dxfattribs={"height": height, "style": "CJ-FONT", "layer": layer})
    ent.set_placement((x, y), align=TextEntityAlignment.MIDDLE_CENTER if center else TextEntityAlignment.MIDDLE_LEFT)
    return ent


def line(msp, a, b, layer="CJ-DIM", dashed=False):
    attr = {"layer": layer}
    if dashed:
        attr["linetype"] = "DASHED" if "DASHED" in msp.doc.linetypes else "CONTINUOUS"
        attr["ltscale"] = 30
    return msp.add_line(a, b, dxfattribs=attr)


def rect(msp, x, y, w, h, layer="CJ-OPENING", dashed=False):
    attr = {"layer": layer}
    if dashed:
        attr.update(linetype="DASHED", ltscale=30)
    return msp.add_lwpolyline([(x,y), (x+w,y), (x+w,y+h), (x,y+h)], close=True, dxfattribs=attr)


def solid(msp, geom, layer, outline=False):
    for poly in _iter_polygons(geom):
        hatch = msp.add_hatch(dxfattribs={"layer": layer})
        hatch.set_solid_fill(color=256)
        hatch.paths.add_polyline_path(list(poly.exterior.coords), is_closed=True, flags=1)
        for ring in poly.interiors:
            hatch.paths.add_polyline_path(list(ring.coords), is_closed=True, flags=0)
        if outline:
            for ring in [poly.exterior, *poly.interiors]:
                msp.add_lwpolyline(list(ring.coords), close=True, dxfattribs={"layer": "CJ-OUTLINE"})


def dimension(msp, a, b, offset, vertical=False):
    """尺寸文字以 cm 顯示；保留可編輯的線與文字。"""
    if vertical:
        p, q = (offset, a[1]), (offset, b[1])
    else:
        p, q = (a[0], offset), (b[0], offset)
    for source, target in ((a,p), (b,q)):
        line(msp, source, target)
        line(msp, (target[0]-60,target[1]-60), (target[0]+60,target[1]+60))
    line(msp, p, q)
    value = abs(b[1]-a[1] if vertical else b[0]-a[0])/10
    ent = text(msp, f"{value:g}", (p[0]+q[0])/2+(140 if vertical else 0), (p[1]+q[1])/2+(0 if vertical else 140), center=True)
    if vertical:
        ent.dxf.rotation = 90


def finish(doc, title):
    """轉 cm 後設置實際比例視口；PDF 由相同紙張與中心座標重建。"""
    msp = doc.modelspace()
    extent = bbox.extents(msp, fast=True)
    text(msp, title + "｜尺寸 cm｜1:50", extent.extmin.x, extent.extmin.y-650, height=200)
    # 既有剖面 DIMENSION 的量測倍率是 mm→cm。轉單位前轉為可編輯線／字，
    # 避免 AutoCAD REGEN 再套一次倍率，將360cm錯算成36cm。
    for dim in list(msp.query("DIMENSION")):
        dim.explode()
    # 保留所有 INSERT 圖塊，縮放插入本身而非炸開。
    log = transform.inplace(msp, Matrix44.scale(.1))
    if len(log):
        raise ValueError("圖面單位轉換失敗，停止輸出")
    doc.units = ezdxf.units.CM
    extent = bbox.extents(msp, fast=True)
    required = (extent.size.x/5+20, extent.size.y/5+20)
    formats = [(420,297), (297,420), (594,420), (420,594), (841,594), (594,841), (1189,841), (841,1189)]
    width, height = next(((w,h) for w,h in formats if w>=required[0] and h>=required[1]),
                         (math.ceil(required[0]), math.ceil(required[1])))
    cx, cy = extent.center.x, extent.center.y
    psp = doc.layouts.get("Layout1")
    psp.page_setup(size=(width,height), margins=(0,0,0,0), units="mm")
    psp.add_viewport(center=(width/2,height/2), size=(width-20,height-20),
                     view_center_point=(cx,cy), view_height=(height-20)*5,
                     dxfattribs={"flags": 16384})
    doc.header["$USERI1"] = PROFILE_MARKER
    doc.header["$USERI2"] = width
    doc.header["$USERI3"] = height
    doc.header["$USERR1"] = cx
    doc.header["$USERR2"] = cy
    doc.header["$LUNITS"] = 2
    return doc


def _opening(msp, spec, item, layers):
    wi, oi = item["wall_index"], item["opening_index"]
    wall = spec.walls[wi]
    op = wall.openings[oi]
    name = f"CJ-{item['code']}-{wi}-{oi}"
    block = msp.doc.blocks.new(name)
    # 以世界座標建立 block，可編輯且完整保留原有的門扇開向。
    if item["code"].startswith("W") and item["type"] != "上懸窗":
        placement = next(p for p in spec.windows if (p.wall_index,p.opening_index)==(wi,oi))
        placement.window.place_in_wall(block, wall, op, layers)
    elif item["type"] not in {"捲門", "上懸窗"}:
        placement = next(p for p in spec.doors if (p.wall_index,p.opening_index)==(wi,oi))
        placement.door.place_in_wall(block, wall, op, layers)
    else:
        x, y = wall.point_at(op.position-op.width/2)
        ux, uy = wall.unit_vector
        nx, ny = wall.normal_vector
        def at(a,b): return (x+ux*a+nx*b, y+uy*a+ny*b)
        for a in (0,op.width):
            line(block, at(a,-wall.thickness/2), at(a,wall.thickness/2), "CJ-OPENING")
        for b in (-wall.thickness/2,wall.thickness/2):
            line(block, at(0,b), at(op.width,b), "CJ-OPENING", dashed=item["type"]=="捲門")
        line(block, at(0,0), at(op.width,0), "CJ-OPENING")
        if item["type"] == "上懸窗":
            line(block, at(0,0), at(op.width/2,300), "CJ-OPENING", True)
            line(block, at(op.width/2,300), at(op.width,0), "CJ-OPENING", True)
    msp.add_blockref(name, (0,0), dxfattribs={"layer": "CJ-OPENING"})


def floor_doc(fl, report):
    doc, layers = new_profile_doc()
    msp, spec = doc.modelspace(), fl.spec
    columns = resolve_columns(spec, build_grid(spec))
    # 柱軸取實際柱心，並不為畫軸線而移動結構柱。
    xs = sorted({c.center[0] for c in columns})
    ys = sorted({c.center[1] for c in columns})
    grid = GridSystem([GridLine(str(i+1),v) for i,v in enumerate(xs)],
                      [GridLine(s,v) for s,v in zip(alpha_labels(len(ys)),ys)]) if xs and ys else build_grid(spec)
    draw_grid(msp, grid, layers, bubble_offset=1800)
    for index, col in enumerate(columns):
        block = doc.blocks.new(f"CJ-COLUMN-{index}")
        solid(block, Polygon(column_corners(col)), "CJ-COLUMN", outline=True)
        msp.add_blockref(block.name, (0,0), dxfattribs={"layer": "CJ-COLUMN"})
    rings = [column_corners(c) for c in columns]
    exterior = exterior_indices(spec)
    ext = merged_wall_footprint([w for i,w in enumerate(spec.walls) if i in exterior], rings)
    inner_walls = [w for i,w in enumerate(spec.walls) if i not in exterior]
    for elev in spec.elevators: inner_walls += elevator_walls(elev)
    for bal in spec.balconies: inner_walls += balcony_walls(bal)
    inside = merged_wall_footprint(inner_walls, rings).difference(ext)
    solid(msp, ext, "CJ-EXT")
    solid(msp, inside, "CJ-INT")
    for poly in _iter_polygons(unary_union([ext, inside])):
        for ring in [poly.exterior, *poly.interiors]:
            msp.add_lwpolyline(list(ring.coords), close=True, dxfattribs={"layer": "CJ-OUTLINE"})
    for item in sorted(report["openings"], key=lambda r: not r["code"].startswith("W")):
        _opening(msp, spec, item, layers)
    for stair in spec.stairs: draw_u_stair(msp, stair, layers)
    if spec.under_stair_wc: draw_under_stair_wc(msp, spec.under_stair_wc, layers)
    draw_patio_marks(msp, spec, layers)
    for elev in spec.elevators: draw_elevator_symbol(msp, elev, layers)
    for bal in spec.balconies: draw_balcony_railing(msp, bal, layers)
    for i, fx in enumerate(spec.fixtures):
        if isinstance(fx, Counter):
            block = doc.blocks.new(f"CJ-COUNTER-{i}")
            draw_counter(block, fx, layers)
            msp.add_blockref(block.name, (0,0), dxfattribs={"layer": "CJ-FURNITURE"})
        else: place_fixture(msp, fx, layers)
    groups = []
    for original, net, room in zip(spec.rooms, net_rooms(spec), report["rooms"]):
        x,y = room["label_point"]
        ents = [text(msp, room["name"], x,y+180, center=True),
                text(msp, f"淨 {room['area_m2']:.2f}m²", x,y, height=110, center=True),
                text(msp, f"{room['ping']:.2f}坪", x,y-160, height=110, center=True)]
        groups.append((Room(original.name, list(net.exterior.coords)[:-1]),ents))
    space = LabelSpace()
    space.occupy_lines(msp)
    relax_flight_labels(msp, space)
    bounds = unary_union([Polygon(r.points) for r in spec.rooms]).bounds
    cx,cy = (bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2
    for item in report["openings"]:
        w = spec.walls[item["wall_index"]]
        op = w.openings[item["opening_index"]]
        x,y = w.point_at(op.position)
        nx,ny = w.normal_vector
        ux,uy = w.unit_vector
        sign = 1 if (x-cx)*nx+(y-cy)*ny>=0 else -1
        candidates = []
        for side in (sign,-sign):
            for distance in (450,700,950):
                for slide in (0,350,-350):
                    tx,ty = x+nx*side*distance+ux*slide,y+ny*side*distance+uy*slide
                    candidates.append((text_box(item['code'],140,tx,ty,TextEntityAlignment.MIDDLE_CENTER),(tx,ty)))
        picked = space.take(candidates)
        tx,ty = (picked or candidates[0])[1]
        msp.add_circle((tx,ty),180,dxfattribs={"layer":"CJ-OPENING"})
        text(msp, item["code"],tx,ty,height=140,layer="CJ-OPENING",center=True)
    for c in report["columns"]:
        text(msp, c["code"], c["x_cm"]*10+300,c["y_cm"]*10+300,height=120)
    space.occupy_lines(msp)
    relax_room_labels(msp, groups, space, text_height=140)
    bounds = unary_union([Polygon(r.points) for r in spec.rooms]).bounds
    x0,y0,x1,y1 = bounds
    dimension(msp, (x0,y0), (x1,y0), y0-1100)
    dimension(msp, (x1,y0), (x1,y1), x1+1100, vertical=True)
    for a,b in zip(grid.x_axes,grid.x_axes[1:]): dimension(msp,(a.position,y1),(b.position,y1),y1+900)
    for a,b in zip(grid.y_axes,grid.y_axes[1:]): dimension(msp,(x0,a.position),(x0,b.position),x0-900,vertical=True)
    return finish(doc, f"{fl.label} 平面圖｜澄跡智庫流程")


def table_doc(title, headings, rows, widths):
    doc, _ = new_profile_doc()
    msp = doc.modelspace()
    for row_index, row in enumerate([headings, *rows]):
        x, y = 0, -row_index*650
        for value, width in zip(row, widths):
            rect(msp,x,y-650,width,650,"CJ-DIM")
            # 長算式換行；固定字高，避免逐欄縮字不可讀。
            capacity = max(4,int((width-180)/145))
            value = str(value)
            chunks = [value[i:i+capacity] for i in range(0,len(value),capacity)] or [""]
            for n, chunk in enumerate(chunks[:3]): text(msp,chunk,x+90,y-190-n*175,height=130)
            if value in {"黃底紅框", "黃", "褐"}:
                solid(msp,Polygon([(x+width-430,y-480),(x+width-130,y-480),(x+width-130,y-280),(x+width-430,y-280)]),
                      "CJ-INT" if value=="褐" else "CJ-EXT",outline=True)
            if len(chunks)>3:
                raise ValueError("表格文字過長，請縮短房間名稱或拆分數量明細")
            x += width
    return finish(doc,title)


def elevation_doc(item):
    doc,_ = new_profile_doc()
    msp = doc.modelspace()
    w,h,s = (item[k]*10 for k in ("width_cm","height_cm","sill_cm"))
    rect(msp,0,s,w,h)
    rect(msp,50,s+50,w-100,h-100)
    typ = item["type"]
    if typ == "捲門":
        rect(msp,-70,s-30,70,h+30); rect(msp,w,s-30,70,h+30)
        rect(msp,-70,s+h,w+140,300,dashed=True)
        for y in range(100,int(h),100): line(msp,(50,s+y),(w-50,s+y),"CJ-OPENING")
    elif typ in {"橫拉窗", "橫拉門"}:
        rect(msp,60,s+60,w*.52-60,h-120)
        rect(msp,w*.48,s+90,w*.52-60,h-180)
        for a,b in ((w*.2,w*.4),(w*.8,w*.6)):
            y=s+h*.5
            line(msp,(a,y),(b,y),"CJ-OPENING")
            sign=1 if b>a else -1
            line(msp,(b-sign*70,y-50),(b,y),"CJ-OPENING")
            line(msp,(b-sign*70,y+50),(b,y),"CJ-OPENING")
    elif typ == "上懸窗":
        line(msp,(50,s+50),(w/2,s+h-50),"CJ-OPENING",True)
        line(msp,(w/2,s+h-50),(w-50,s+50),"CJ-OPENING",True)
        line(msp,(w/2-80,s+100),(w/2+80,s+100),"CJ-OPENING")
    elif typ == "平開門":
        # 平面 >=100cm 為子母門，此處同步兩扇及鉸鏈側。
        left = item["direction"].startswith("left")
        split = w*.68 if left else w*.32
        spans = [(0,split),(split,w)] if w>=1000 else [(0,w)]
        if len(spans)>1: line(msp,(split,s+50),(split,s+h-50),"CJ-OPENING")
        for i,(a,b) in enumerate(spans):
            hinge_left = left if len(spans)==1 else i==0
            hinge,handle = (a,b-100) if hinge_left else (b,a+100)
            line(msp,(hinge,s+50),(handle,s+h*.5),"CJ-OPENING",True)
            line(msp,(handle,s+h*.5),(hinge,s+h-50),"CJ-OPENING",True)
            line(msp,(handle-50,s+1000),(handle+50,s+1000),"CJ-OPENING")
    line(msp,(-500,0),(w+500,0))
    text(msp,"完成地坪 ±0",-500,-200,height=110)
    dimension(msp,(0,s+h),(w,s+h),s+h+600)
    dimension(msp,(w,s),(w,s+h),w+500,vertical=True)
    if s: dimension(msp,(0,0),(0,s),-450,vertical=True)
    text(msp,f"{item['code']} {typ} {item['width_cm']:g}×{item['height_cm']:g}cm",w/2,-600,180,"CJ-OPENING",True)
    text(msp,f"窗台 {item['sill_cm']:g}cm｜數量 {item['count']}｜開向見平面",w/2,-900,120,center=True)
    return finish(doc,f"{item['code']} 門窗立面")


def build_profile_sheets(building, report):
    from src.web.render import Sheet
    sheets = []
    def add(label, kind, filename, doc): sheets.append(Sheet(label,kind,filename,doc))
    for fl, floor in zip(building.floors, report["floors"]):
        add(fl.label,"floor",f"{fl.label}.dxf",floor_doc(fl,floor))
        rows = [[r['index'],r['name'],r['position'],f"{r['width_cm']:.1f}×{r['depth_cm']:.1f}",f"{r['area_m2']:.3f}",f"{r['perimeter_cm']:.1f}"] for r in floor["rooms"]]
        add(fl.label+" 淨面積","schedule",f"{fl.label}_rooms.dxf",table_doc(fl.label+" 室內淨尺寸（凹形為外包寬深）",["序號","室名","位置","寬×深 cm","淨面積 m²","周長 cm"],rows,[900,2800,1100,3200,2100,2100]))
        rows = [[r['item'],r['formula'],f"{r['value']:.3f}",r['unit'],"—" if r['ping'] is None else f"{r['ping']:.3f}",r['note']] for r in floor["quantities"]]
        add(fl.label+" 數量","schedule",f"{fl.label}_quantities.dxf",table_doc(fl.label+" 裝修數量（不扣門窗）",["項目","計算式","數量","單位","坪","備註"],rows,[3000,10000,1800,1200,1600,4000]))
        rows = [[c['code'],f"{c['width_cm']:g}×{c['depth_cm']:g}",c['count'],f"({c['x_cm']:g}, {c['y_cm']:g})",c['color'],c['note']] for c in floor['columns']]
        rows += [[w['type'],f"厚{w['thickness_cm']:g}",f"{w['length_m']:.3f}m","中心線總長",w['color'],"未扣門窗"] for w in floor['walls']]
        add(fl.label+" 柱牆","schedule",f"{fl.label}_structure.dxf",table_doc(fl.label+" 柱與牆表（尺寸 cm）",["編號／類型","尺寸","數量／長度","柱心座標 cm","顏色","備註"],rows,[2000,2000,2400,4000,1800,2800]))
        edges = [[r['index'],r['name'],str(i+1),f"{length:.2f}"] for r in floor['rooms'] for i,length in enumerate(r['edges_cm'])]
        for page,start in enumerate(range(0,len(edges),28),1):
            add(f"{fl.label} 周長{page}","schedule",f"{fl.label}_perimeter_{page}.dxf",table_doc(fl.label+" 淨周長逐段明細",["室序號","室名","邊序號","長度 cm"],edges[start:start+28],[1400,3200,1600,2400]))
    rows = [[o['code'],o['type'],f"{o['width_cm']:g}",f"{o['height_cm']:g}",f"{o['sill_cm']:g}",o['count']] for o in report['openings']]
    add("門窗表","schedule","openings.dxf",table_doc("全棟門窗表",["編號","類型","寬 cm","高 cm","窗台 cm","數量"],rows,[1800,2400,1800,1800,1800,1400]))
    for item in report['openings']:
        add(item['code']+" 立面","opening_elevation",f"opening_{item['code']}.dxf",elevation_doc(item))
    if len(building.floors)>1:
        from src.drafting.section import draw_section, draw_elevation
        doc,layers = new_profile_doc()
        doc.dimstyles.get("STRUCT").dxf.dimlfac = .1
        draw_section(doc.modelspace(),building,layers,axis="x",title="示意剖面 A-A")
        add("剖面","section","section.dxf",finish(doc,"示意剖面 A-A"))
        doc,layers = new_profile_doc()
        sizes = {(f['label'],o['wall_index'],o['opening_index']): (o['sill_cm']*10,o['height_cm']*10)
                 for f in report['floors'] for o in f['openings']}
        draw_elevation(doc.modelspace(),building,layers,side="south",title="南向立面",opening_sizes=sizes)
        add("立面","elevation","elevation.dxf",finish(doc,"南向立面"))
    return sheets
