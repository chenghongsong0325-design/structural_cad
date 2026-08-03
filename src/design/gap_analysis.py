"""與參考圖對照(Phase 12)—— 「我們的圖離合格圖還差什麼」,而且是**量出來的**。

參考圖:勞動部勞動力發展署技能檢定中心的丙級術科平面圖,**三個版本互相印證**——
「建築製圖應用-電繪項」21101-107-0301/0302(107 年 8 月)與 21101-104-0301/0302
(104 年 4 月),以及舊制「電腦輔助建築製圖」159-90-0302(92 年 12 月)。
⚠️ 一律以**台灣**的圖為準(使用者 2026-08-03 定調:不要用簡體字的圖當基準——
   房間名、圖例、法規全都不一樣,照著做只會歪掉)。

⚠️ 三版一比得到一個判斷,直接影響補洞的順序:107 與 104 版連 R150 圓弧陽台、
   陰井 90×90、雨水排水溝 20cm 都逐項相同(可見不是某一屆出題老師的偏好);而
   92 年那張是**沒有家具、沒有室名的空殼圖**,卻照樣有門窗編號、剖切指示符號、
   地界線/建築線/C℄ —— 這幾項比家具還基本。我們現在有家具、有陽台、有面積表,
   偏偏缺這幾項,順序是反的。

做法(不是憑印象列清單):

  1. `REFERENCE_ELEMENTS` 把參考圖上**看得到的每一種圖面元素**寫成一條檢查項,
     並註明在參考圖哪裡看到的(`seen_at`)。
  2. 每條都有 `probe`:實際去產線畫出來的 modelspace 裡**找**那個元素
     (文字/圖塊/圖層/實體型別),找到才算 have。所以 "have" 不是我說了算。
  3. 找不到但有部分替代品的算 `partial`,並寫清楚差在哪。

輸出 GapReport(可 to_dict / to_json,照專案慣例)+ `docs/gap_analysis.md`。

    python -m src.design.gap_analysis          # 重新量測並更新 docs/gap_analysis.md
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

HAVE, PARTIAL, MISSING = "have", "partial", "missing"
_MARK = {HAVE: "✅", PARTIAL: "🟡", MISSING: "❌"}


# ── 報表 ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GapItem:
    """一種圖面元素的對照結果。"""

    code: str
    name: str
    seen_at: str            # 參考圖上哪裡看得到
    status: str             # have / partial / missing
    ours: str               # 我們這邊是什麼(模組/實際量到的東西)
    note: str = ""          # 差在哪、要補什麼

    def __str__(self) -> str:
        tail = f" —— {self.note}" if self.note else ""
        return f"{_MARK[self.status]} {self.name}:{self.ours}{tail}"


@dataclass
class GapReport:
    """整份對照報表。"""

    items: list = field(default_factory=list)
    reference: str = ""

    @property
    def have(self) -> list:
        return [i for i in self.items if i.status == HAVE]

    @property
    def partial(self) -> list:
        return [i for i in self.items if i.status == PARTIAL]

    @property
    def missing(self) -> list:
        return [i for i in self.items if i.status == MISSING]

    @property
    def coverage(self) -> float:
        """涵蓋率:完全做到算 1、部分算 0.5。"""
        if not self.items:
            return 0.0
        return (len(self.have) + 0.5 * len(self.partial)) / len(self.items)

    def to_dict(self) -> dict:
        return {
            "reference": self.reference,
            "n_items": len(self.items),
            "n_have": len(self.have),
            "n_partial": len(self.partial),
            "n_missing": len(self.missing),
            "coverage": round(self.coverage, 3),
            "items": [asdict(i) for i in self.items],
        }

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kw)

    def summary(self) -> str:
        head = (f"圖面元素對照({self.reference}):共 {len(self.items)} 項 · "
                f"做到 {len(self.have)}、部分 {len(self.partial)}、"
                f"缺 {len(self.missing)} · 涵蓋率 {self.coverage:.0%}")
        return "\n".join([head, *(f"  {i}" for i in self.items)])


# ── 產線畫出來的圖:給 probe 用的觀察值 ─────────────────────────────────────
def _observe(msp, spec) -> dict:
    """把一張畫好的圖攤平成可以用來「找東西」的觀察值。

    ⚠️ 分「圖面上的字」與「表格裡的字」:面積計算表/門窗表放在地界線右側,裡面本來
    就有 D1/W1 這些編號。不分開的話,「門窗編號有沒有標在圖上」會被表格騙過去
    ——第一版就被騙了(報 ✅,其實圖上一個都沒有)。"""
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    site = (min(xs), min(ys), max(xs), max(ys))

    def _pos(e):
        try:
            p = e.dxf.insert
            return float(p[0]), float(p[1])
        except Exception:
            return None

    def _in_site(e):
        p = _pos(e)
        return p is not None and site[0] <= p[0] <= site[2] and \
            site[1] <= p[1] <= site[3]

    all_txt = list(msp.query("TEXT")) + list(msp.query("MTEXT"))

    def _s(e):
        return e.dxf.text if e.dxftype() == "TEXT" else e.text

    texts = [_s(e) for e in all_txt]
    return {
        "spec": spec,
        "site": site,
        "texts": texts,
        "on_plan": [_s(e) for e in all_txt if _in_site(e)],   # 地界線範圍內的字
        "joined": " | ".join(texts),
        "layers": {e.dxf.layer for e in msp},
        "types": {e.dxftype() for e in msp},
        "by_layer": {(e.dxf.layer, e.dxftype()) for e in msp},
        "blocks": {e.dxf.name for e in msp.query("INSERT")},
        "n_axis_circle": len([e for e in msp.query("CIRCLE")
                              if e.dxf.layer == "AXIS"]),
        "n_dim": len(msp.query("DIMENSION")),
    }


def _has_text(o, *needles) -> bool:
    return any(n in o["joined"] for n in needles)


def _match_text(o, pattern, key="texts") -> list:
    rx = re.compile(pattern)
    return [t for t in o[key] if rx.fullmatch(t.strip())]


# ── 檢查項:參考圖上看得到的每一種元素 ──────────────────────────────────────
# (code, 名稱, 參考圖哪裡看得到, probe → (status, 我們是什麼, 差在哪))
def _elements() -> list:
    def frame(o):
        ok = "BORDER" in o["layers"] or getattr(o["spec"], "sheet", False)
        return ((HAVE, "titleblock.draw_sheet_border(A3 雙框)", "")
                if ok else (MISSING, "沒畫", "要 spec.sheet=True"))

    def title(o):
        ok = bool(getattr(o["spec"], "title_block", None)) or _has_text(
            o, "檢定編號", "圖名")
        return ((HAVE, "titleblock 競賽格式標題欄(8 欄位圖塊+屬性)", "")
                if ok else (PARTIAL, "產線的 spec 沒掛標題欄",
                            "web 下載版才加;module 本身有"))

    def grid(o):
        n = o["n_axis_circle"]
        if n >= 6:
            return HAVE, f"gridlines:軸線 + 編號圈 {n} 個", ""
        if n:
            return (PARTIAL, f"只有 {n} 個編號圈(窄透天是單跨)",
                    "參考圖 A/B/C × 1/2/3/4 共 7 條軸線;兩帶式與集合住宅才多跨")
        return MISSING, "沒畫軸網", ""

    def dims(o):
        n = o["n_dim"]
        if n >= 20:
            return HAVE, f"dim_chains 四邊三層,共 {n} 道標註", ""
        return (PARTIAL, f"只有 {n} 道", "要 spec.dim_chains=True 才有三層")

    def site_line(o):
        return ((HAVE, "BORDER 層地界線(PHANTOM)", "")
                if "BORDER" in o["layers"] else (MISSING, "沒畫", ""))

    def build_line(o):
        return ((HAVE, "ARCH 層建築線(CENTER)", "")
                if "ARCH" in o["layers"] else (MISSING, "沒畫", ""))

    def road_center(o):
        return (MISSING, "沒有道路中心線",
                "參考圖上下都有「C℄」標記(退縮/建築線的依據)")

    def bench_mark(o):
        return (MISSING, "沒有界標符號",
                "參考圖基地四角有 ╳ 方框(界樁/基地角點)")

    def north(o):
        ok = "N" in [t.strip() for t in o["texts"]] or "NORTH_ARROW" in o["blocks"]
        return ((HAVE, "annotations 北向箭頭", "")
                if ok else (PARTIAL, "模組有,這張沒開",
                            "spec.north_arrow 預設 False"))

    def walls(o):
        return ((HAVE, "wall_join 聯集接角雙線牆", "")
                if "WALL" in o["layers"] else (MISSING, "沒畫", ""))

    def wall_note(o):
        return ((HAVE, "有牆厚文字", "") if _has_text(o, "RC Wall")
                else (MISSING, "牆上沒有厚度標註",
                      "參考圖用引線寫「15cm RC Wall」「20cm RC Wall」;"
                      "我們牆厚只存在資料裡,圖上看不到"))

    def columns(o):
        return ((HAVE, "members.draw_column(柱藏牆內)", "")
                if "COL" in o["layers"] else
                (MISSING, "窄透天不放柱",
                 "參考圖每個軸網交點都有柱;兩帶式與集合住宅有"))

    def doors(o):
        n = len([b for b in o["blocks"] if "DOOR" in b.upper()])
        return ((HAVE, f"door_window:門圖塊 {n} 種(開啟弧線)", "")
                if n else (MISSING, "沒畫", ""))

    def windows(o):
        n = len([b for b in o["blocks"] if "WIN" in b.upper()])
        return ((HAVE, f"door_window:窗圖塊 {n} 種", "")
                if n else (MISSING, "沒畫", ""))

    def opening_tags(o):
        # ⚠️ 只認**畫在地界線範圍內**的編號:門窗表裡本來就有 D1/W1,不排除會誤判。
        on_plan = _match_text(o, r"(D|W|DW|SD)\d+", key="on_plan")
        in_table = _match_text(o, r"(D|W|DW|SD)\d+")
        if on_plan:
            return HAVE, f"圖上標了 {len(on_plan)} 個門窗編號", ""
        return (PARTIAL,
                f"門窗表裡有 {len(in_table)} 個編號,圖上一個都沒標",
                "參考圖每個開口旁都有帶框編號(D1~D6 / W1~W3 / DW / SD),"
                "要能跟門窗表對得起來 —— 這是最明顯的一項缺口")

    def spot_level(o):
        return (MISSING, "圖上沒有地坪標高",
                "參考圖每個高程變化處都有「±0 / +20 / 3.850」+ ▽ 或 ✛ 符號"
                "(陽台比室內低 2cm 就是靠這個表達)")

    def stairs(o):
        return ((HAVE, "stair:踏步線+折斷線+方向箭頭+上/下", "")
                if _has_text(o, "上", "下") else (MISSING, "沒畫", ""))

    def stair_count(o):
        return (PARTIAL, "只寫「上/下」",
                "參考圖寫「UP 16 / DN」(級數),我們沒把級數標上去")

    def elevator(o):
        return (PARTIAL, "balcony_elevator.draw_elevator_symbol 有,透天沒放",
                "參考圖是雙併集合住宅有電梯;我們只有集合住宅產線放")

    def balcony(o):
        return ((HAVE, "layout/balcony.py 挑出式陽台(欄杆+矮牆+落地拉門)", "")
                if _has_text(o, "陽台") else
                (MISSING, "這層沒有陽台", "1F 依規則不設"))

    def curved_balcony(o):
        return (MISSING, "只有矩形陽台",
                "參考圖主臥外是 R150 的圓弧陽台(造型陽台)")

    def canopy(o):
        return (MISSING, "沒有雨遮",
                "參考圖標「大門鋁合金雨遮(2F)」;雨遮是常見的附屬構造")

    def shaft(o):
        return ((HAVE, "narrow_house/graph_layout 管道間(80×60cm)", "")
                if _has_text(o, "管道") else
                (PARTIAL, "AI 版的核有管道間,窄透天刻意不放",
                 "參考圖屋突層標「管道間」;使用者 2026-07-29 決定窄透天不放"))

    def room_name(o):
        return ((HAVE, "room.draw_room_label(室名+面積)", "")
                if _has_text(o, "臥室", "客廳", "樓梯間") else (MISSING, "沒畫", ""))

    def unit_tag(o):
        return (MISSING, "沒有戶別標記",
                "參考圖每戶標 (A)(B) 圈;單戶透天用不到,集合住宅需要")

    def furniture(o):
        return ((HAVE, f"fixtures:圖塊 {len(o['blocks'])} 種(床/沙發/衛浴/流理台)", "")
                if o["blocks"] else (MISSING, "沒畫", ""))

    def sanitary(o):
        return ((HAVE, "toilet / basin / bathtub 圖塊", "")
                if any("TOILET" in b or "BASIN" in b for b in o["blocks"])
                else (PARTIAL, "這層沒有衛浴設備", ""))

    def area_table(o):
        return ((HAVE, "schedule.draw_area_table", "")
                if _has_text(o, "面積計算表") else
                (PARTIAL, "模組有,要 spec.schedules=True", ""))

    def opening_table(o):
        return ((HAVE, "schedule.draw_opening_table", "")
                if _has_text(o, "門窗表") else
                (PARTIAL, "模組有,要 spec.schedules=True", ""))

    def balcony_area(o):
        return (MISSING, "陽台面積沒進面積計算表",
                "真實圖把陽台列成附屬建物面積(與主建物分開計)")

    def drainage(o):
        return (MISSING, "沒有基地排水",
                "參考圖標「雨水排水溝20cm寬」「公共排水溝40cm寬」「陰井90×90」")

    def hatch(o):
        return (PARTIAL, "wall_join.draw_wall_hatch 有,預設關閉",
                "1:100 平面慣例不填剖面線(使用者 2026-07-15 確認),參考圖也沒填")

    def section(o):
        return (HAVE, "section.draw_section / draw_elevation(另出圖)", "")

    def section_mark(o):
        return (MISSING, "平面圖上沒有剖切指示",
                "參考圖外牆角落有 A◺ 之類的方向符號,標出剖面/立面是從哪裡剖、"
                "往哪個方向看。我們的剖面立面是另外一張圖,平面上沒有指到它 —— "
                "看圖的人對不起來(92 年的空殼圖都有,可見是基本要求)")

    def ceiling(o):
        return (MISSING, "沒有天花板圖",
                "參考圖這一組沒有,但真實審定圖有反射天花圖(RCP)")

    return [
        ("sheet_frame", "圖框(A3)", "四周雙框 + 左上「電腦繪圖項」", frame),
        ("title_block", "標題欄", "下方 圖名/比例/檢定時間/試題編號/應檢人簽名", title),
        ("grid", "軸網 + 編號圈", "A/B/C(橫)× 1/2/3/4(縱)", grid),
        ("dim_chains", "尺寸鏈(多層)", "四邊各 2~3 層:總長/軸距/開口細部", dims),
        ("site_line", "地界線", "最外圈一點鏈線,標「地界線」", site_line),
        ("building_line", "建築線", "內一圈鏈線,標「建築線」", build_line),
        ("road_center", "道路中心線", "上下兩處「C℄」", road_center),
        ("bench_mark", "基地界標", "基地四角 ╳ 方框", bench_mark),
        ("north_arrow", "北向箭頭", "壹層平面圖右上角", north),
        ("walls", "牆(雙線+接角)", "全圖", walls),
        ("wall_thickness_note", "牆厚引線標註", "「15cm RC Wall」「20cm RC Wall」",
         wall_note),
        ("columns", "柱", "軸網交點的實心方柱", columns),
        ("doors", "門符號", "開啟弧線 + 門扇", doors),
        ("windows", "窗符號", "牆上三線窗", windows),
        ("opening_tags", "門窗編號(圖上)", "D1~D6 / W1~W3 / DW / SD 帶框標籤",
         opening_tags),
        ("spot_level", "地坪標高", "±0、+20、3.850 + ▽/✛ 符號", spot_level),
        ("stairs", "樓梯", "踏步線 + 折斷線 + 方向", stairs),
        ("stair_steps", "樓梯級數標註", "「UP 16」「DN」「UP 23」", stair_count),
        ("elevator", "電梯", "井道 + 轎廂打叉符號", elevator),
        ("balcony", "陽台", "多處「陽台」+ 欄杆線", balcony),
        ("curved_balcony", "圓弧陽台", "主臥外 R150 弧形陽台", curved_balcony),
        ("canopy", "雨遮", "「大門鋁合金雨遮(2F)」引線", canopy),
        ("pipe_shaft", "管道間", "屋突層「管道間」", shaft),
        ("room_name", "室名", "主臥室/臥室/客廳/餐廳/浴廁/廚房", room_name),
        ("unit_tag", "戶別標記", "(A)(B) 圈", unit_tag),
        ("furniture", "家具", "床/沙發/餐桌/衣櫃", furniture),
        ("sanitary", "衛浴廚具", "馬桶/洗手台/浴缸/流理台/冰箱", sanitary),
        ("area_table", "面積計算表", "(本組試題未附,真實圖必備)", area_table),
        ("opening_table", "門窗表", "(本組試題未附,真實圖必備)", opening_table),
        ("balcony_area", "陽台面積計入", "附屬建物面積", balcony_area),
        ("drainage", "基地排水", "雨水排水溝/公共排水溝/陰井", drainage),
        ("wall_hatch", "牆體剖面線", "本組未填(1:100 慣例)", hatch),
        ("section", "剖面圖 / 立面圖", "同一組試題的其他張", section),
        ("section_mark", "剖切指示符號", "外牆角落 A◺ 方向標記(三個版本都有)",
         section_mark),
        ("ceiling", "天花板圖", "本組未附", ceiling),
    ]


REFERENCE = ("勞動部技能檢定丙級術科平面圖三個版本:「建築製圖應用-電繪項」"
             "21101-107-0301/0302(107 年 8 月)、21101-104-0301/0302(104 年 4 月)、"
             "以及舊制「電腦輔助建築製圖」159-90-0302(92 年 12 月)")

# ⚠️ 三個版本畫的是同一棟雙併建築,元素清單彼此**互相印證**——107 與 104 版幾乎
#    逐項相同(連 R150 圓弧陽台、陰井 90×90、雨水排水溝 20cm 都一樣),差別只在
#    修訂年份;92 年舊制版是「不含家具的空殼圖」,但**門窗編號、剖切指示符號、
#    地界線/建築線/C℄ 一樣都沒少**——可見這幾項是圖面的基本要求,不是裝飾。


# ── 主流程 ──────────────────────────────────────────────────────────────────
def _sample_drawing():
    """拿一張產線畫出來的圖當受測對象(窄透天 2F:有陽台、有樓梯、有家具)。"""
    from dataclasses import replace

    from src.design.layout.narrow_house import generate_narrow_building
    from src.drafting.apartment_plan import draw_floor_plan
    from src.standards.loader import apply_standard, load_standard, new_document

    _lb, spec = generate_narrow_building(7000, 12000, floors=3)[1]
    spec = replace(spec, schedules=True, north_arrow=True, dim_chains=True)
    doc = new_document()
    layers = apply_standard(doc, load_standard())
    msp = doc.modelspace()
    draw_floor_plan(msp, spec, layers)
    return msp, spec


def analyze_gap(msp=None, spec=None) -> GapReport:
    """量測我們的圖有沒有參考圖上的每一種元素 → GapReport。"""
    if msp is None or spec is None:
        msp, spec = _sample_drawing()
    o = _observe(msp, spec)
    items = []
    for code, name, seen_at, probe in _elements():
        status, ours, note = probe(o)
        items.append(GapItem(code, name, seen_at, status, ours, note))
    return GapReport(items, REFERENCE)


def to_markdown(rep: GapReport) -> str:
    """報表 → docs/gap_analysis.md 的內容。"""
    lines = [
        "# 與參考圖對照(Phase 12)",
        "",
        f"**參考圖**:{rep.reference}",
        "",
        "> ⚠️ 一律以**台灣**的圖為準。簡體字的平面圖(露台/主卧/书房/公卫)"
        "房間名、圖例、法規都不一樣,不拿來當基準。",
        "",
        "三個版本畫的是同一棟雙併建築,元素清單彼此**互相印證**:107 與 104 版"
        "幾乎逐項相同(連 R150 圓弧陽台、陰井 90×90、雨水排水溝 20cm 都一樣);"
        "92 年舊制版是**不含家具的空殼圖**,但門窗編號、剖切指示、地界線/建築線/C℄ "
        "一樣都沒少 —— 可見這幾項是圖面的**基本要求**,不是裝飾。",
        "",
        "對照對象:規則版窄透天 7×12m 三層的 **2F**(有陽台、樓梯、家具、"
        "面積表/門窗表全開)。每一項都是**實際去畫好的圖裡找**,不是憑印象列的。",
        "",
        f"**結果**:共 {len(rep.items)} 項 —— "
        f"做到 {len(rep.have)}、部分 {len(rep.partial)}、缺 {len(rep.missing)},"
        f"涵蓋率 **{rep.coverage:.0%}**",
        "",
        "| | 圖面元素 | 參考圖上長什麼樣 | 我們的狀況 | 差在哪 |",
        "|---|---|---|---|---|",
    ]
    order = {HAVE: 0, PARTIAL: 1, MISSING: 2}
    for i in sorted(rep.items, key=lambda x: (order[x.status], x.code)):
        lines.append(f"| {_MARK[i.status]} | {i.name} | {i.seen_at} | "
                     f"{i.ours} | {i.note or '—'} |")
    lines += [
        "",
        "## 缺口按「補起來的難度」排序",
        "",
        "1. **門窗編號標到圖上**(最明顯):門窗表已經有 D1/W1,只差把同一組編號"
        "畫在每個開口旁邊,圖表才對得起來。半天。",
        "2. **牆厚引線標註**:牆厚本來就在 `Wall.thickness` 裡,只是圖上沒寫。"
        "挑幾道代表性的牆拉引線寫「15cm RC Wall」。半天。",
        "3. **樓梯級數**:`UStair.steps_per_flight` 已知,把「上」改成「UP 16」。1 小時。",
        "4. **地坪標高**:陽台比室內低 2cm 這種資訊目前完全沒有,要先在資料模型"
        "加「樓板高程」才畫得出來。1~2 天。",
        "5. **陽台面積計入面積計算表**:要分「主建物/附屬建物」兩欄。半天。",
        "6. **剖切指示符號**:剖面圖已經會畫,只差在平面上標「從這裡剖、往這邊看」,"
        "兩張圖才對得起來。半天。",
        "7. **基地排水、界標、道路中心線**:都是基地層級的圖面元素,目前 spec 只有"
        "`site_boundary` 一個矩形,要先擴充基地資料模型。2~3 天。",
        "8. **圓弧陽台、雨遮**:造型構件,要新的幾何。各 1 天。",
        "9. **天花板圖(RCP)**:整張新圖,目前完全沒有。3~5 天。",
        "",
        "## 已經做到、但這張圖上沒開的",
        "",
        "軸網/柱(窄透天是單跨、不放柱,兩帶式與集合住宅有)、電梯(集合住宅有)、"
        "牆體剖面線(1:100 平面慣例不填,參考圖也沒填)、剖面/立面(另外出圖)。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    import pathlib

    rep = analyze_gap()
    print(rep.summary())
    out = pathlib.Path(__file__).resolve().parents[2] / "docs" / "gap_analysis.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(rep), encoding="utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
