"""Keep requested conditions separate from design choices; measure the final plan.

Counts are exact unless the user explicitly says at least/at most. Nothing here
changes geometry or claims that the implemented checks cover construction design.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from src.design.report import JsonReport

LABELS = {
    "floors_above": "地上樓層", "basements": "地下樓層", "bedrooms": "臥室數",
    "car_spaces": "汽車位", "garage": "車庫", "patio": "天井",
    "want_study": "書房", "want_elder_room": "一樓孝親房",
    "master_corner": "主臥方位", "kitchen_side": "廚房方位",
    "site_width_m": "可用面寬（米）", "site_depth_m": "可用進深（米）",
    "units_per_row": "每排戶數", "corridor_width_m": "走廊寬（米）",
}
MENTIONS = {
    "floors_above": r"(?<!地下)[一二兩三四五六七八九十\d]+\s*(?:層|樓層)|加蓋|樓層",
    "basements": r"地下|B\d", "bedrooms": r"[一二兩三四五六七八九十\d]+\s*(?:間)?(?:臥室|房)|臥室",
    "car_spaces": r"[一二兩三四五六七八九十\d]+\s*(?:個|台|輛|部)?(?:汽車位|車位|車)|車位|[汽台輛部]車|停車|車庫", "garage": r"車庫", "patio": r"天井",
    "want_study": r"書房|工作室|閱讀室", "want_elder_room": r"孝親|長輩|父母房|多代同堂",
    "master_corner": r"主臥", "kitchen_side": r"廚房|餐廚",
    "site_width_m": r"面寬|寬|[×x*]", "site_depth_m": r"進深|深|[×x*]",
    "units_per_row": r"戶|每排", "corridor_width_m": r"走廊",
}
REQUIRED = re.compile(r"必須|一定|(?<!非)必要|不可取消|不能取消|不能少|必需")


def evidence(text: str, name: str) -> str:
    return next((s.strip() for s in re.split(r"[，,。；;\n]", text)
                 if re.search(MENTIONS[name], s, re.I)), "")


def preference_for(quote: str, name: str) -> bool:
    """A preference cue must belong to this condition, not its neighbour."""
    target = re.search(MENTIONS[name], quote, re.I)
    if not target:
        return False
    before, after = quote[:target.start()], quote[target.end():]
    prefix = re.search(r"(?:最好|盡量|希望有|能有就|偏好)\s*(?:有|要)?$", before)
    suffix = re.match(r"\s*(?:可有可無|可取消|可以取消|非必要)", after)
    return bool(prefix or suffix) and not REQUIRED.search(quote)


def comparison_for(quote: str, name: str) -> str:
    target = re.search(MENTIONS[name], quote, re.I)
    if not target:
        return "eq"
    before, after = quote[:target.start()], quote[target.end():]
    if re.search(r"(?:至少|不低於|不少於)\s*$", before) or re.match(r"\s*以上", after):
        return "min"
    if re.search(r"(?:至多|最多|不超過)\s*$", before) or re.match(r"\s*以下", after):
        return "max"
    return "eq"


def literal_count(quote: str, name: str):
    """Cross-check unambiguous literal counts without replacing the NL parser.

    Relative edits (加一房), multiple different counts, and vague wording remain
    the parser's job. A clear 四房 must never be reported as 三房 after parsing.
    """
    num = r"([一二兩三四五六七八九十\d]+)"
    patterns = {
        "bedrooms": num + r"\s*(?:間)?(?:臥室|房)",
        "floors_above": num + r"\s*(?:層樓|層|樓層)",
        "basements": r"地下\s*" + num + r"\s*層",
        "car_spaces": num + r"\s*(?:個|台|輛|部)?(?:汽車位|車位|車)",
    }
    if name not in patterns:
        return None
    if name == "car_spaces" and "雙車位" in quote:
        return 2
    values = []
    for match in re.finditer(patterns[name], quote):
        prefix = quote[:match.start()].rstrip()
        if re.search(r"增加|加|多|減少|減|少", prefix[-2:]) and not prefix.endswith(("至少", "至多", "最多", "不少於")):
            continue
        if name == "floors_above" and prefix.endswith("地下"):
            continue
        token = match[1]
        digits = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9}
        if token.isdecimal():
            value = int(token)
        elif token in digits:
            value = digits[token]
        elif "十" in token and token.count("十") == 1:
            hi, lo = token.split("十")
            if (hi and hi not in digits) or (lo and lo not in digits):
                continue
            value = digits.get(hi, 1) * 10 + digits.get(lo, 0)
        else:
            continue
        values.append(value)
    return values[0] if values and len(set(values)) == 1 else None


def cancelled(quote: str, name: str) -> bool:
    target = re.search(MENTIONS[name], quote, re.I)
    if not target or re.search(r"(?:不可|不能|不得|不可以|不要|可以|可)\s*取消", quote):
        return False
    before, after = quote[:target.start()], quote[target.end():]
    return bool(re.search(r"(?:不要|取消|不設|不需要)\s*(?:設置|保留|有)?\s*$", before)
                or re.match(r"\s*(?:取消|不要(?:了)?|不需要(?:了)?)(?:$|[，,。；;])", after))


def prepare_data(data: dict, text: str, base: dict | None = None) -> dict:
    """Bind priorities to literal user evidence, preserving untouched fields on edits.

    A model cannot demote a required condition merely by returning preferred.
    Legacy parser payloads remain conservative: every non-null value is required.
    """
    out = dict(data)
    details = {d.get("field"): d for d in (out.get("requirement_details") or [])
               if isinstance(d, dict) and d.get("field") in LABELS}
    previous = {r["field"]: r for r in (base or {}).get("_requirements", [])}
    records = []
    corrections = []
    for name in LABELS:
        detail = details.get(name, {})
        quote = detail.get("quote", "")
        # Evidence must name this field and actually occur in the current request.
        if not (isinstance(quote, str) and quote and quote in text
                and re.search(MENTIONS[name], quote, re.I)):
            quote = evidence(text, name)
        # Do not let a shortened model quote omit a nearby 必須 / 至少 qualifier.
        quote = evidence(text, name) or quote
        if base is not None and not quote:
            out[name] = base.get(name)
            if name in previous:
                records.append(dict(previous[name]))
                continue
        value = out.get(name)
        literal = literal_count(quote, name)
        if literal is not None and value != literal:
            corrections.append({"field": name, "parsed": value, "literal": literal, "source": quote})
            value = out[name] = literal
        if cancelled(quote, name):
            if name in {"garage", "patio", "want_study", "want_elder_room"}:
                value = out[name] = False
            elif name in {"basements", "car_spaces"}:
                value = out[name] = 0
        if value is None:
            continue
        if name in {"floors_above", "basements", "bedrooms", "car_spaces", "units_per_row"}:
            if type(value) is not int or value < 0 or (name in {"floors_above", "units_per_row"} and value == 0):
                raise ValueError(LABELS[name] + "必須是有效整數")
        elif name in {"site_width_m", "site_depth_m", "corridor_width_m"}:
            import math
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError(LABELS[name] + "必須是正數")
        elif name in {"garage", "patio", "want_study", "want_elder_room"} and type(value) is not bool:
            raise ValueError(LABELS[name] + "必須為是或否")
        priority = ("preferred" if preference_for(quote, name)
                    else "required")
        operator = "eq"
        if name in {"bedrooms", "car_spaces", "floors_above", "basements"}:
            operator = comparison_for(quote, name)
        if name in {"site_width_m", "site_depth_m"}:
            # Envelope is a limit; deep sites may deliberately leave a yard.
            operator = "max"
        records.append({"field": name, "label": LABELS[name], "expected": value,
                        "priority": priority, "operator": operator,
                        "source": quote or "解析欄位（未附原句，保守視為必要）"})
    if base is not None and not any(evidence(text, k) for k in ("site_width_m", "site_depth_m")):
        out["dimension_basis"] = base.get("dimension_basis")
    out["_requirements"] = records
    out["_parse_corrections"] = corrections
    return out


@dataclass
class RequirementReport(JsonReport):
    items: list = field(default_factory=list)

    @property
    def ok(self):
        return all(i["status"] == "met" for i in self.items if i["priority"] == "required")

    def to_dict(self):
        return {"ok": self.ok, "items": self.items,
                "required_met": sum(i["status"] == "met" and i["priority"] == "required" for i in self.items),
                "required_total": sum(i["priority"] == "required" for i in self.items)}

    def summary(self):
        unmet = [i["label"] + "：" + i["detail"] for i in self.items if i["status"] != "met"]
        return "需求已滿足" if not unmet else "；".join(unmet)


def check_requirements(building, requirements: list, *, basis: str = "building") -> RequirementReport:
    """Count actual rooms/levels/parked cars, never echo the requested options.

    Parking is a count of car placements, not a swept-path/driving feasibility test.
    Orientation uses a centroid approximation. Conditions without a common
    measurement, such as apartment unit counts, are reported as unverified.
    """
    floors = list(building.floors)
    rooms = [(f, r) for f in floors for r in f.spec.rooms]
    beds = [(f, r) for f, r in rooms if r.kind in {"bedroom", "master_bedroom"}
            and "孝親" not in r.name]
    garages = [(f, r) for f, r in rooms if r.kind in {"garage", "parking"}]
    studies = [(f, r) for f, r in rooms if r.kind == "study"]
    elders = [(f, r) for f, r in rooms if f.level == 1 and
              (r.kind == "elder_room" or "孝親" in r.name)]
    patios = [(f, r) for f, r in rooms if r.kind == "patio"]
    cars = [(f, p) for f in floors for p in f.spec.fixtures if getattr(p, "name", None) == "car"]
    actual = {"floors_above": sum(f.level > 0 for f in floors),
              "basements": sum(f.level < 0 for f in floors), "bedrooms": len(beds),
              "car_spaces": len(cars), "garage": bool(garages), "patio": bool(patios),
              "want_study": bool(studies), "want_elder_room": bool(elders)}
    # Axis spans describe the generated building envelope, in millimetres.
    if floors:
        actual["site_width_m"] = max(sum(f.spec.x_spacings) for f in floors) / 1000
        actual["site_depth_m"] = max(sum(f.spec.y_spacings) for f in floors) / 1000
    # Use room polygons against the common envelope, never the requested input.
    from shapely.geometry import Polygon
    from src.design.layout.plan_check import building_env
    for key, selected in (("master_corner", [(f, r) for f, r in rooms if r.kind == "master_bedroom" or "主臥" in r.name]),
                          ("kitchen_side", [(f, r) for f, r in rooms if r.kind == "kitchen"])):
        sides = []
        for fl, room in selected:
            x0, y0, x1, y1 = building_env(fl.spec)
            center = Polygon(room.points).centroid
            sides.append(("N" if center.y >= (y0 + y1) / 2 else "S") +
                         ("E" if center.x >= (x0 + x1) / 2 else "W"))
        if sides:
            actual[key] = "/".join(sorted(set(sides)))
    refs = {"bedrooms": beds, "garage": garages, "patio": patios,
            "want_study": studies, "want_elder_room": elders}
    result = RequirementReport()
    for r in requirements:
        item = dict(r)
        value = actual.get(r["field"])
        item["actual"] = value
        expected = r["expected"]
        if value is None:
            item.update(status="unverified", detail="尚無可靠的圖面量測，不能視為已滿足")
        else:
            op = r.get("operator", "eq")
            if r["field"] == "kitchen_side":
                good = all(expected in side for side in value.split("/"))
            else:
                good = (value >= expected if op == "min" else value <= expected + 0.001
                        if op == "max" else value == expected)
            item.update(status="met" if good else "unmet",
                        detail=f"要求{ {'eq': '等於', 'min': '至少', 'max': '不超過'}[op]} {expected}，實際 {value}")
            if r["field"] in {"site_width_m", "site_depth_m"}:
                item["detail"] += "（量測建築軸線外框；不代表退縮／建蔽率已核准）"
            if r["field"] == "car_spaces":
                item["detail"] += "（圖面汽車配置數，不含轉彎軌跡驗證）"
        item["evidence"] = [{"floor": f.label, "room": room.name,
                             "room_index": next(j for j, candidate in enumerate(f.spec.rooms) if candidate is room)}
                            for f, room in refs.get(r["field"], [])]
        result.items.append(item)
    return result


def option_locks(requirements: list) -> dict:
    mapping = {"floors_above": "floors", "bedrooms": "bedrooms", "garage": "garage", "patio": "patio"}
    locks = {mapping[r["field"]]: r["expected"] for r in requirements
             if r["priority"] == "required" and r["field"] in mapping and r.get("operator", "eq") == "eq"}
    parking = next((r for r in requirements if r["field"] == "car_spaces" and r["priority"] == "required"), None)
    if parking is not None and parking["expected"] > 0:
        locks.setdefault("garage", True)
    return locks


def contract_note(requirements: list) -> str:
    import json
    return ("\n\n本案條件（required 不可刪除或降低，preferred 才可退讓；"
            "排不下應回報，不能用改需求提高分數）：\n"
            + json.dumps(requirements, ensure_ascii=False)) if requirements else ""
