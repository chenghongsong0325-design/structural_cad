"""自然語言介面(C2)—— 中文需求描述 → HouseBrief/CorridorBrief → 出圖。

「基地 16×14 米,三房兩廳」這樣的一句話,解析成 C1 產生器吃的設計需求,
跑完整流程出圖。路線圖的最後一步。

實作方式(使用者 2026-07-13 定調:以 LLM 為主,用 Gemini API):
  * 呼叫 Google Gemini API(google-genai SDK),用「結構化輸出」
    (response_schema 強制 JSON 格式)保證回傳一定是合法 JSON、欄位齊全
    ——像給模型一張固定格式的點餐單,不用寫正則、不怕模型自由發揮。
  * 兩層分工,方便測試:
      - parse_brief(text, client=None):組 prompt、呼叫 API(client 可注入
        假物件,單元測試不需網路/API key)。
      - _brief_from_data(data):純資料轉換(米→mm、補預設值、範圍檢查),
        不碰網路,測試直接餵 dict。
  * 解析結果餵給 generate_floor_plan(),沿用 C1 的 validate_spec 檢核,
    LLM 給出離譜數值(基地 3×3m、8 間臥室)會被同一套規則擋下。

需要環境變數 GEMINI_API_KEY(https://aistudio.google.com 申請)。
單元測試不需要——測試注入假 client。

典型用法::

    from src.design.nl_parser import parse_brief

    brief = parse_brief("基地 16×14 米,三房兩廳,一層樓")
    spec = generate_floor_plan(brief)     # → 直接餵 draw_floor_plan 出圖

命令列(解析+出圖一條龍)::

    python src/design/nl_parser.py "基地16米寬14米深,三房"
    python src/design/nl_parser.py "集合住宅,每排6戶,走廊2米寬"

⚠️ 待確認假設見模組結尾 PENDING 區塊。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.layout_generator import Brief, CorridorBrief, HouseBrief

# 模型:2.5 flash(快、便宜,解析一句話綽綽有餘);可用 GEMINI_MODEL 覆寫。
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# ---------------------------------------------------------------------------
# JSON schema:LLM 的「填空表格」——結構化輸出保證回傳長這樣
# ---------------------------------------------------------------------------
# 長度一律「米」(中文描述的自然單位),_brief_from_data 再轉 mm。
# 沒提到的欄位填 null(nullable),由 Python 補預設值(與 dataclass 預設一致)。
BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "brief_type": {
            "type": "string",
            "enum": ["house", "corridor"],
            "description": "house=單戶住宅(透天/獨棟,有基地寬深);"
                           "corridor=集合住宅(公寓/大樓,多戶沿走廊重複)",
        },
        "site_width_m": {
            "type": "number", "nullable": True,
            "description": "基地寬(米,東西向)。單戶必填;沒講就 null",
        },
        "site_depth_m": {
            "type": "number", "nullable": True,
            "description": "基地深(米,南北向)。單戶必填;沒講就 null",
        },
        "bedrooms": {
            "type": "integer", "nullable": True,
            "description": "臥室數(「三房兩廳」的房=3)。沒講就 null",
        },
        "units_per_row": {
            "type": "integer", "nullable": True,
            "description": "集合住宅「每排」戶數(雙邊走廊南北各一排,"
                           "總戶數=2×每排)。描述若給總戶數要除以 2。沒講就 null",
        },
        "corridor_width_m": {
            "type": "number", "nullable": True,
            "description": "集合住宅走廊寬(米)。沒講就 null",
        },
        "floor_label": {
            "type": "string", "nullable": True,
            "description": "樓層標示(如 1F、3F)。沒講就 null",
        },
        "master_corner": {
            "type": "string", "nullable": True,
            "enum": ["NW", "NE", "SW", "SE"],
            "description": "主臥指定角落(單戶限定):西南角=SW、東北角=NE、"
                           "西北角=NW、東南角=SE。沒講就 null",
        },
        "kitchen_side": {
            "type": "string", "nullable": True,
            "enum": ["N", "S", "E", "W"],
            "description": "廚房靠哪一側(單戶限定):靠北=N、靠南=S、"
                           "靠東=E、靠西=W。沒講就 null",
        },
        "floors_above": {
            "type": "integer", "nullable": True,
            "description": "地上樓層數(「透天三層」=3、「五層公寓」=5)。"
                           "沒講就 null",
        },
        "basements": {
            "type": "integer", "nullable": True,
            "description": "地下層數(「地下一層」「B1 車庫」=1)。沒講就 null",
        },
        "want_study": {
            "type": "boolean", "nullable": True,
            "description": "是否要書房/工作室/閱讀室(單戶限定)。有提到=true;"
                           "沒提就 null",
        },
        "want_elder_room": {
            "type": "boolean", "nullable": True,
            "description": "是否要孝親房/長輩房/父母房(單戶限定,一樓臥室,"
                           "多代同堂用)。有提到=true;沒提就 null",
        },
        "car_spaces": {
            "type": "integer", "nullable": True,
            "description": "汽車停車位數(單戶限定):「雙車位/停兩台車」=2、"
                           "「一個車位」=1。機車位不算。沒提就 null",
        },
    },
    "required": ["brief_type", "site_width_m", "site_depth_m", "bedrooms",
                 "units_per_row", "corridor_width_m", "floor_label",
                 "master_corner", "kitchen_side", "floors_above", "basements",
                 "want_study", "want_elder_room", "car_spaces"],
}

SYSTEM_PROMPT = """\
你是建築設計需求解析器。把使用者的中文描述解析成設計需求 JSON。

規則:
- 判斷建築類型:提到「集合住宅/公寓/大樓/N戶/走廊」→ corridor;
  否則(單戶/透天/基地寬深+房數)→ house。
- 長度單位一律換算成「米」輸出(「16米」=16;「1600公分」=16;
  描述用坪數當基地時,假設接近方形換算成寬×深)。
- 「三房兩廳」「3房2廳1衛」的房數=臥室數(廳/衛浴由產生器自動配置,忽略)。
- corridor 的 units_per_row 是「每排」戶數;若描述給總戶數(雙排對排),
  除以 2。
- 方位(單戶限定):「主臥要在西南角」→ master_corner="SW";
  「廚房靠北」→ kitchen_side="N"。方位用羅盤縮寫(N北/S南/E東/W西)。
- 樓層:「透天三層」「三層樓」→ floors_above=3;「地下一層」「B1」→
  basements=1。只講「透天」沒講層數 → 都 null。
- 指定房間(單戶限定):「書房/工作室/閱讀室」→ want_study=true;
  「孝親房/長輩房/父母房/多代同堂」→ want_elder_room=true;
  「雙車位/停兩台車/兩個汽車位」→ car_spaces=2、「一個車位/停一台車」→
  car_spaces=1(機車位不算車位)。這些是額外指定,臥室數不受影響
  (「三房加書房」= bedrooms=3、want_study=true)。
- 沒提到的欄位一律 null,不要瞎猜數值。
"""


# ---------------------------------------------------------------------------
# 資料轉換(純函式,不碰網路)
# ---------------------------------------------------------------------------
def _brief_from_data(data: dict) -> Brief:
    """解析結果 dict → Brief(米→mm、補預設值)。

    只做最基本的「缺必填欄位」檢查;數值合理性(房數 1~4、每排 2~10 戶、
    基地夠不夠大)交給 generate_floor_plan 既有的檢核,規則單一來源。
    """
    btype = data.get("brief_type")
    if btype == "house":
        w, d = data.get("site_width_m"), data.get("site_depth_m")
        if w is None or d is None:
            raise ValueError(
                "單戶住宅需要基地寬與深(例:「基地 16×14 米」),描述裡找不到")
        kwargs = dict(site_width=float(w) * 1000, site_depth=float(d) * 1000)
        if data.get("bedrooms") is not None:
            kwargs["bedrooms"] = int(data["bedrooms"])
        if data.get("floor_label"):
            kwargs["floor_label"] = data["floor_label"]
        if data.get("master_corner"):           # 方位約束(C2)
            kwargs["master_corner"] = data["master_corner"]
        if data.get("kitchen_side"):
            kwargs["kitchen_side"] = data["kitchen_side"]
        if data.get("want_study"):              # 指定房間(E3)
            kwargs["want_study"] = True
        if data.get("want_elder_room"):
            kwargs["want_elder_room"] = True
        if data.get("car_spaces") is not None:
            kwargs["car_spaces"] = int(data["car_spaces"])
        return HouseBrief(**kwargs)

    if btype == "corridor":
        kwargs = {}
        if data.get("units_per_row") is not None:
            kwargs["units_per_row"] = int(data["units_per_row"])
        if data.get("corridor_width_m") is not None:
            kwargs["corridor_width"] = float(data["corridor_width_m"]) * 1000
        if data.get("floor_label"):
            kwargs["floor_label"] = data["floor_label"]
        return CorridorBrief(**kwargs)

    raise ValueError(f"未知建築類型:{btype!r}(需為 house 或 corridor)")


def _building_from_data(data: dict, seed: int = 0) -> "BuildingBrief":
    """解析結果 dict → BuildingBrief(整棟樓需求)。

    標準層沿用 _brief_from_data;樓層數/地下室在這裡接上 D 階段的
    building_generator:
      * floors_above 沒講 → 1(單層,行為同 C2 時期)。
      * 透天(house)只要「多層或有地下室」就開層別分化 differentiated
        (1F 公共層、2F+ 臥室層、B1 車庫)——這是 D2 的預設玩法,也是
        generate_building 對「透天+地下室」的硬性要求。
    seed:設計變體種子(E2)——同 seed 同方案、換 seed 換方案;只對透天
      HouseBrief 有效(集合住宅變體未做)。
    """
    from src.design.building_generator import BuildingBrief

    typical = _brief_from_data(data)
    if isinstance(typical, HouseBrief):
        typical.seed = seed
    floors = int(data.get("floors_above") or 1)
    basements = int(data.get("basements") or 0)
    # 汽車位需要地下車庫(E3):透天地下室必走 differentiated 骨架(柱位才對得上
    # 標準層),而該骨架下 1F 是公共層、臥室在樓上——故「要車位」隱含「多樓層
    # 透天+地下室」。使用者沒指定樓層/地下室時自動補齊(≥2 樓地上 + ≥1 地下)。
    if isinstance(typical, HouseBrief) and typical.car_spaces > 0:
        basements = max(basements, 1)
        floors = max(floors, 2)
    differentiated = isinstance(typical, HouseBrief) and (
        floors > 1 or basements > 0)
    return BuildingBrief(typical=typical, floors=floors, basements=basements,
                         differentiated=differentiated)


# ---------------------------------------------------------------------------
# LLM 呼叫
# ---------------------------------------------------------------------------
def parse_brief(text: str, client: Optional[object] = None) -> Brief:
    """中文需求描述 → Brief。

    client 可注入(單元測試給假物件);None 時建立真的 Gemini 客戶端,
    需要 GEMINI_API_KEY 環境變數。
    """
    if not text or not text.strip():
        raise ValueError("需求描述是空的")

    if client is None:
        from google import genai
        client = genai.Client()   # 自動讀 GEMINI_API_KEY / GOOGLE_API_KEY

    return _brief_from_data(_call_llm(text, client))


def parse_building_brief(text: str, client: Optional[object] = None,
                         seed: int = 0) -> "BuildingBrief":
    """中文需求描述 → BuildingBrief(整棟樓,E1 網頁化的入口)。

    與 parse_brief 同一次 LLM 呼叫、同一張 schema,多接住樓層數/地下室;
    沒講樓層就是單層,行為與 parse_brief + generate_floor_plan 一致。
    seed:設計變體種子(E2),同 seed 同方案、換 seed 換方案(透天限定)。
    """
    if not text or not text.strip():
        raise ValueError("需求描述是空的")
    if client is None:
        from google import genai
        client = genai.Client()   # 自動讀 GEMINI_API_KEY / GOOGLE_API_KEY
    return _building_from_data(_call_llm(text, client), seed=seed)


def _call_llm(text: str, client: object) -> dict:
    response = client.models.generate_content(
        model=MODEL,
        contents=text,
        config={
            "system_instruction": SYSTEM_PROMPT,
            "response_mime_type": "application/json",
            "response_schema": BRIEF_SCHEMA,
        },
    )
    return json.loads(response.text)


# ---------------------------------------------------------------------------
# 命令列:一句話 → DXF
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print('用法:python src/design/nl_parser.py "基地16×14米,三房"')
        raise SystemExit(1)
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("需要設定 GEMINI_API_KEY 環境變數(https://aistudio.google.com 申請)")
        raise SystemExit(1)

    from src.design.layout_generator import generate_floor_plan
    from src.drafting.apartment_plan import draw_floor_plan
    from src.standards.loader import apply_standard, load_standard, new_document

    text = " ".join(args)
    print(f"[1/3] 解析需求:{text}")
    brief = parse_brief(text)
    print(f"      → {brief}")

    print("[2/3] 生成格局…")
    spec = generate_floor_plan(brief)

    name = "nl_house" if isinstance(brief, HouseBrief) else "nl_corridor"
    out = _PROJECT_ROOT / "output" / f"{name}.dxf"
    out.parent.mkdir(exist_ok=True)
    doc = new_document()
    layers = apply_standard(doc, load_standard())
    draw_floor_plan(doc.modelspace(), spec, layers)
    doc.saveas(out)
    print(f"[3/3] 出圖完成:{out}"
          f"({len(spec.rooms)} 室 {len(spec.doors)} 門 {len(spec.windows)} 窗)")


if __name__ == "__main__":
    main()


# =============================================================================
# PENDING(待確認假設彙整)
# =============================================================================
# 1. 模型:gemini-2.5-flash(使用者提供 Gemini API key,2026-07-13 實測可用;
#    GEMINI_MODEL 環境變數可覆寫)。解析輸入極短,單次費用可忽略。
# 2. 需求欄位覆蓋現有 Brief 的旋鈕:基地寬深/房數/每排戶數/走廊寬/樓層,
#    以及方位約束(主臥角落 master_corner、廚房方位 kitchen_side——單戶
#    限定,靠整張圖鏡射達成,衝突會報錯)。更細的約束(「客廳要大一點」
#    「浴室兩間」)等產生器支援再加。
# 3. 坪數→寬深的換算假設「接近方形」;實際基地形狀應該問使用者。待確認。
# 4. 解析錯誤的重試:目前一次定輸贏(schema 保證格式,但語意誤判不重試)。
#    待確認是否需要「解析結果先給使用者確認再出圖」的互動步驟。
# =============================================================================
