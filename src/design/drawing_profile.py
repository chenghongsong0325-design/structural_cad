"""澄跡製圖設定與數量計算。輸入 cm，幾何仍使用 mm；不修改原始方案。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import unary_union

from src.design.column_design import column_footprints
from src.drafting.apartment_plan import build_grid, resolve_columns
from src.drafting.wall_join import merged_wall_footprint


class RoomCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    floor: str = Field(min_length=1, max_length=12)
    name: str = Field(min_length=1, max_length=40)
    index: int | None = Field(default=None, ge=0)
    width_cm: float | None = Field(default=None, gt=0, le=10000)
    depth_cm: float | None = Field(default=None, gt=0, le=10000)
    position: Literal["auto", "front", "back", "left", "right"] = "auto"


class DrawingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = True
    floor_height_cm: float = Field(default=360, ge=240, le=600)
    ceiling_height_cm: float = Field(default=280, ge=200, le=500)
    exterior_wall_cm: float = Field(default=20, ge=12, le=50)
    interior_wall_cm: float = Field(default=12, ge=10, le=30)
    column_cm: float = Field(default=40, ge=30, le=100)
    door_height_cm: float = Field(default=210, ge=190, le=350)
    window_height_cm: float = Field(default=150, ge=120, le=300)
    window_sill_cm: float = Field(default=90, ge=0, le=200)
    shutter_height_cm: float = Field(default=240, ge=200, le=400)
    bathroom_tile_cm: float = Field(default=240, ge=0, le=400)
    window_style: Literal["sliding", "awning", "fixed"] = "sliding"
    rooms: list[RoomCheck] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def heights(self):
        if self.ceiling_height_cm > self.floor_height_cm:
            raise ValueError("天花高度不可超過樓層高度")
        if self.bathroom_tile_cm > self.ceiling_height_cm:
            raise ValueError("浴室壁磚高度不可超過天花高度")
        if max(self.door_height_cm, self.shutter_height_cm,
               self.window_sill_cm + self.window_height_cm) > self.floor_height_cm:
            raise ValueError("門窗頂部不可超過樓層高度")
        return self


def exterior_indices(spec):
    """以房間聯集邊界識別外牆，包含天井的外露牆。"""
    envelope = unary_union([Polygon(r.points) for r in spec.rooms if r.kind != "patio"])
    if envelope.is_empty:
        raise ValueError("缺少房間邊界，無法判定外牆")
    return {i for i, w in enumerate(spec.walls)
            if LineString([w.start, w.end]).intersection(envelope.boundary.buffer(2)).length
            > w.length * .5}


def apply_profile(building, options: DrawingOptions):
    """複製後套用實際尺寸；呼叫端仍須通過所有原有驗證。"""
    from src.design.layout.fixture_fix import push_fixtures_out_of_walls, clear_fixtures_off_columns, trim_counters_at_columns
    from src.design.layout.narrow_house import shift_openings_off_columns
    from src.design.layout.door_rules import repair_doors
    from src.design.layout.graph_layout import _declutter_for_circulation
    from src.design.building_generator import check_column_alignment

    result = deepcopy(building)
    height = options.floor_height_cm * 10
    for fl in result.floors:
        spec = fl.spec
        fl.elevation = fl.elevation / result.floor_height * height
        exterior = exterior_indices(spec)
        for i, wall in enumerate(spec.walls):
            wall.thickness = 10 * (options.exterior_wall_cm if i in exterior else options.interior_wall_cm)
        # 明確的繪圖預設；不得再稱為結構概算或審定尺寸。
        spec.column_size = options.column_cm * 10
        for stair in spec.stairs:
            # 與既有 code_check 的每級上限 200mm 相同；盡量保留原級數。
            count = max(stair.steps_per_flight, math.ceil(height / (2 * 200)))
            tread = stair.flight_run / count
            if tread < 210:
                raise ValueError(f"{fl.label} 樓梯空間放不下 {options.floor_height_cm:g}cm 層高所需踏步；請加大基地或降低層高")
            stair.steps_per_flight = count
            stair.tread = tread
        for wp in spec.windows:
            wp.window.style = options.window_style
        shift_openings_off_columns(spec)
        bounds = unary_union([Polygon(r.points) for r in spec.rooms]).bounds
        repair_doors(spec, bounds[0], bounds[1], bounds[2], fl.level)
        push_fixtures_out_of_walls(spec)
        clear_fixtures_off_columns(spec)
        trim_counters_at_columns(spec)
        _declutter_for_circulation(spec, allow_remove=False)
    result.floor_height = height
    alignment = check_column_alignment(result)
    if alignment:
        raise ValueError("指定柱尺寸後，上下樓柱位無法對齊：" + "；".join(alignment[:3]))
    return result


def net_rooms(spec):
    """關閉門窗缺口後扣牆、扣實際柱體；保持 L 形及凹角，不用包圍矩形冒充。"""
    walls = merged_wall_footprint([replace(w, openings=[]) for w in spec.walls])
    obstacles = unary_union([walls, *column_footprints(spec)])
    result = []
    for room in spec.rooms:
        geom = Polygon(room.points).difference(obstacles)
        if geom.is_empty or geom.geom_type != "Polygon":
            raise ValueError(f"{room.name} 的室內淨空間已被牆柱切斷，請調整尺寸或更換方案")
        result.append(geom)
    return result


def _opening_type(placement, is_window):
    if is_window:
        return {"sliding": "橫拉窗", "awning": "上懸窗", "fixed": "固定窗"}[placement.window.style]
    door = placement.door
    if "捲" in door.label and door.sliding:
        return "捲門"
    return "橫拉門" if door.sliding else "平開門"


@dataclass
class DrawingReport:
    data: dict

    def to_dict(self):
        return deepcopy(self.data)

    def summary(self):
        return f"澄跡製圖：{len(self.data['floors'])} 層、{len(self.data['openings'])} 種門窗，尺寸 cm、比例 1:50"


def build_report(building, options: DrawingOptions) -> DrawingReport:
    data = {"profile": "chengji", "version": 1, "settings": options.model_dump(),
            "units": "cm", "scale": "1:50", "floors": [], "openings": [],
            "steps": ["確認尺寸與自動配置假設", "軸線及柱", "牆體", "窗", "門與捲門", "家具", "室名與淨面積", "尺寸與標題", "材料與裝修數量表", "門窗立面"],
            "notes": ["房間位置由現有配置引擎安排；下方淨尺寸及位置是生成結果。指定條件會另行核對，未符合就停止出圖。",
                      "淨面積扣除牆厚與柱體；周長依室內淨輪廓，計算時封閉門窗洞口，不扣洞口。開放房間的分界按房間分區計算。",
                      "天花按各室淨地板投影計算，包含車庫、浴室及樓梯區；天井與陽台不列入。斜梯底等特殊面另計。",
                      "柱尺寸為使用者指定的製圖值，並非結構計算結果。門窗高度為本次指定值；採光檢核仍採既有保守的120cm窗高。",
                      "窗簾盒只計客廳、餐廳及臥室外窗，每窗左右各加15cm，1台尺=100/3.3cm。",
                      "房間淨尺寸表的寬、深是淨輪廓外包尺寸；凹形房間以淨面積與逐段周長為準。"]}
    if any(fl.spec.under_stair_wc for fl in building.floors):
        data["notes"].append("樓梯下廁所目前是既有圖面的虛線註記，尚無完整牆體與淨高，未另外計浴室壁磚與粉刷；其地板投影已包含在樓梯區，不重複加總。")
    data["notes"].append("柱軸以實際柱心繪製；既有外推柱可能偏離外牆中心線。這次保留引擎柱位，未強行移柱。")
    catalog, counters = {}, {"D": 0, "W": 0, "SD": 0}
    column_codes = {}
    for fl in building.floors:
        spec = fl.spec
        nets = net_rooms(spec)
        floor = {"label": fl.label, "rooms": [], "openings": [], "columns": [], "walls": [], "quantities": []}
        envelope = unary_union([Polygon(r.points) for r in spec.rooms]).bounds
        exterior = exterior_indices(spec)
        for index, (room, net) in enumerate(zip(spec.rooms, nets)):
            bx0, by0, bx1, by1 = net.bounds
            center = net.representative_point()
            included = room.kind not in {"patio", "balcony", "terrace", "void"}
            edges = [math.dist(a, b)/10 for ring in [net.exterior, *net.interiors]
                     for a, b in zip(list(ring.coords), list(ring.coords)[1:])]
            floor["rooms"].append({"index": index, "name": room.name, "kind": room.kind,
                "included": included, "area_m2": net.area/1e6, "ping": net.area/1e6/3.30578,
                "perimeter_cm": net.length/10, "edges_cm": edges,
                "width_cm": (bx1-bx0)/10, "depth_cm": (by1-by0)/10,
                "rectangular": abs(net.area - (bx1-bx0)*(by1-by0)) < 1,
                "position": ("前" if center.y < (envelope[1]+envelope[3])/2 else "後") +
                            ("左" if center.x < (envelope[0]+envelope[2])/2 else "右"),
                "label_point": [center.x, center.y]})
        for is_window, placements in ((False, spec.doors), (True, spec.windows)):
            for p in placements:
                wall = spec.walls[p.wall_index]
                opening = wall.openings[p.opening_index]
                typ = _opening_type(p, is_window)
                prefix = "W" if is_window else "SD" if typ == "捲門" else "D"
                h = options.window_height_cm if is_window else options.shutter_height_cm if prefix == "SD" else options.door_height_cm
                sill = options.window_sill_cm if is_window else 0
                # 開向不同也分型，避免同一個立面代碼有兩種開法。
                direction = "" if is_window or p.door.sliding else f"{p.door.hinge}/{p.door.swing}"
                key = (prefix, typ, round(opening.width/10, 4), h, sill, direction)
                if key not in catalog:
                    counters[prefix] += 1
                    item = dict(code=f"{prefix}{counters[prefix]}", type=typ, width_cm=opening.width/10,
                                height_cm=h, sill_cm=sill, direction=direction, count=0)
                    catalog[key] = item
                    data["openings"].append(item)
                item = catalog[key]
                item["count"] += 1
                floor["openings"].append({**item, "count": 1, "wall_index": p.wall_index, "opening_index": p.opening_index})
        for col in resolve_columns(spec, build_grid(spec)):
            key = tuple(round(v, 3) for v in col.center)
            code = column_codes.setdefault(key, f"C{len(column_codes)+1}")
            floor["columns"].append(dict(code=code, width_cm=col.width/10, depth_cm=col.depth/10,
                count=1, x_cm=col.center[0]/10, y_cm=col.center[1]/10, color="黃底紅框", note="指定製圖尺寸"))
        for ext, name in ((True, "外牆"), (False, "內牆")):
            selected = [w for i, w in enumerate(spec.walls) if (i in exterior) == ext]
            # 同線重複段只計一次；T/L交會按各中心線長度。
            length = unary_union([LineString([w.start, w.end]) for w in selected]).length / 1000
            floor["walls"].append(dict(type=name, thickness_cm=options.exterior_wall_cm if ext else options.interior_wall_cm,
                length_m=length, color="黃" if ext else "褐", note="中心線長度；未扣門窗；不含陽台矮牆與電梯附屬牆"))
        _quantities(floor, spec, exterior, options)
        data["floors"].append(floor)
    _check_rooms(data, options)
    return DrawingReport(data)


def _check_rooms(data, options):
    for req in options.rooms:
        floors = [f for f in data["floors"] if f["label"] == req.floor]
        matches = [r for f in floors for r in f["rooms"] if r["name"] == req.name
                   and (req.index is None or r["index"] == req.index)]
        if len(matches) != 1:
            raise ValueError(f"{req.floor}/{req.name} 無法唯一辨識，請核對室名；同名房間請填房間序號")
        r = matches[0]
        for field in ("width_cm", "depth_cm"):
            desired = getattr(req, field)
            if desired is not None and (not r["rectangular"] or abs(r[field]-desired) > 1):
                raise ValueError(f"{req.floor}/{req.name} 未符合指定淨尺寸（容差1cm）；實際外包淨寬×深為 {r['width_cm']:.1f}×{r['depth_cm']:.1f}cm" + ("，且為凹形空間" if not r["rectangular"] else ""))
        position = {"front": "前", "back": "後", "left": "左", "right": "右"}.get(req.position)
        if position and position not in r["position"]:
            raise ValueError(f"{req.floor}/{req.name} 未符合指定位置：實際為{r['position']}方")


def _quantities(floor, spec, exterior, options):
    rooms = [r for r in floor["rooms"] if r["included"]]
    bath = [r for r in rooms if r["kind"] in {"bathroom", "toilet", "wc"} or any(s in r["name"] for s in ("浴", "廁"))]
    dry = [r for r in rooms if r not in bath]
    kitchen = [r for r in rooms if r["kind"] == "kitchen" or "廚" in r["name"]]
    dry_p, bath_p = (sum(r["perimeter_cm"] for r in group)/100 for group in (dry, bath))
    h, ceiling, tile = (v/100 for v in (options.floor_height_cm, options.ceiling_height_cm, options.bathroom_tile_cm))
    def add(name, formula, value, unit="m²", note="不扣門窗洞口"):
        floor["quantities"].append(dict(item=name, formula=formula, value=value, unit=unit,
            ping=value/3.30578 if unit == "m²" else None, note=note))
    add("非浴室牆面粉刷", f"{dry_p:.4f}m × {h:g}m", dry_p*h)
    add("室內牆面油漆", f"{dry_p:.4f}m × {ceiling:g}m + {bath_p:.4f}m × ({ceiling:g}-{tile:g})m", dry_p*ceiling + bath_p*(ceiling-tile))
    add("浴室牆面粉刷", f"{bath_p:.4f}m × {h:g}m", bath_p*h)
    add("浴室壁磚", f"{bath_p:.4f}m × {tile:g}m", bath_p*tile)
    for label, selected in (("浴室地磚", bath), ("廚房地磚", kitchen), ("天花板", rooms), ("天花油漆", rooms)):
        add(label, " + ".join(f"{r['area_m2']:.4f}" for r in selected) + " m²" if selected else "0 m²",
            sum(r["area_m2"] for r in selected), note=f"室內淨面積；天花高度{options.ceiling_height_cm:g}cm")
    curtain_widths = []
    for op in floor["openings"]:
        if not op["code"].startswith("W") or op["wall_index"] not in exterior:
            continue
        wall = spec.walls[op["wall_index"]]
        opening = wall.openings[op["opening_index"]]
        point = Point(wall.point_at(opening.position))
        eligible = [r for r in spec.rooms if r.kind in {"living", "dining", "bedroom", "master"}
                    or any(s in r.name for s in ("客廳", "餐廳", "臥室"))]
        if any(Polygon(r.points).distance(point) <= wall.thickness/2 + 2 for r in eligible):
            curtain_widths.append(op["width_cm"])
    add("窗簾盒", (" + ".join(f"({w:g}+30)" for w in curtain_widths) or "0") + " cm ÷ (100/3.3)",
        sum(w+30 for w in curtain_widths)/(100/3.3), "台尺", "客廳／餐廳／臥室外窗；每窗兩側各15cm")
