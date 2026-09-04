"""網頁後端(E1)—— 把「中文需求 → 整棟樓出圖」包成 HTTP 服務。

瀏覽器打開網頁,輸入一句「透天三層,基地19×13米,三房,地下一層」,
按下生成,後端跑完整生產線,回傳每層樓的 SVG(直接顯示)與 DXF(下載)。

端點:
    GET  /                      前端頁面(src/web/static/)
    GET  /api/config            前端開機自檢:要不要通行碼、API key 有沒有設
    POST /api/generate          {"text": 需求描述, "code": 通行碼}
                                → {"summary", "sheets": [{label, kind, svg,
                                   dxf}], "zip"}
    GET  /api/jobs/{id}/{file}  下載該次生成的 DXF / 全部打包 zip

離線示範(發表/口試用):

    DEMO_MODE=1 → LLM 呼叫改讀 samples/ai_demo.json 的錄音回放(產線照跑),
    Gemini 免費額度用完或斷網也能完整演示 AI 設計師模式;回應帶 demo=true,
    前端會標示「示範模式・離線回放」。

安全(放上公網的最低配備):
    * ACCESS_CODE 環境變數:設了之後,generate 要帶對通行碼才會動——
      防止路人亂打 API 燒你的 Gemini 額度。沒設就完全開放(本機開發用)。
    * 下載檔名走白名單(英數 + .dxf/.zip),擋路徑跳脫。

本機啟動::

    uvicorn src.web.app:app --reload
    # 瀏覽器開 http://localhost:8000

單元測試用 create_app(client_factory=...) 注入假 Gemini client,不需網路。
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.building_generator import (
    BuildingSpec,
    generate_building,
    generate_building_auto,
)
from src.design.layout_generator import (
    HouseBrief,
    house_design_note,
    max_house_bedrooms,
)
from src.design.layout.global_score import score_report
from src.design.metrics import building_metrics
from src.design.nl_parser import (
    building_brief_from_data,
    parse_brief_data,
    parse_modification_data,
)
from src.web.render import build_sheets, docs_to_pdf, sheet_svg

JOBS_DIR = _PROJECT_ROOT / "output" / "web"          # 每次生成一個子資料夾
STATIC_DIR = Path(__file__).resolve().parent / "static"

_JOB_ID_RE = re.compile(r"[0-9a-f]{12}")
_FILENAME_RE = re.compile(r"[A-Za-z0-9_]+\.(dxf|zip|pdf)")   # 白名單:擋路徑跳脫

HISTORY_LIMIT = 20            # /api/history 最多回幾筆(新→舊)
# 回應裡最多內嵌這麼多位元組的 DXF(base64 前的原始大小)。
# 為什麼要內嵌:Render 免費方案的硬碟是**暫時的**,每次部署/閒置休眠就清空,
# output/web/<job_id> 會消失 → 舊頁面的下載連結 404(使用者實際遇到過)。
# 把 DXF 直接放進回應,下載就不再依賴伺服器硬碟;job_id 連結仍保留當備援。
INLINE_DXF_BUDGET = 8 * 1024 * 1024


class GenerateRequest(BaseModel):
    text: str
    code: str = ""
    seed: Optional[int] = None      # 設計變體(E2):None → 隨機抽一個(每次不同)
    # 多輪修改(E4):帶上一輪的需求 dict(回應裡的 brief_data)→ text 視為
    # 「修改指令」,以 base 為底合併;不帶 = 全新需求。
    base: Optional[dict] = None
    # AI 設計師模式:走「LLM 設計拓撲 → 搜尋落實 → 挑毛病回饋重設計」的混合式
    # 收斂管線(design_loop),而非既有規則產生器。目前限窄面寬透天(建築 5~7m 寬)。
    ai_design: bool = False


class ScoreRequest(BaseModel):
    """家具配置評分(Phase 6-7)—— 對已生成的方案就地評分(不搬家具)。"""

    job_id: str
    code: str = ""


def _has_api_key() -> bool:
    # 金鑰不只來自那兩個環境變數(還有 GEMINI_API_KEYS 與 api_keys.json),
    # 一律問 api_keys 模組,別在這裡自己讀 os.environ。
    from src.design.api_keys import have_key
    return have_key()


# Gemini 額度/限流錯誤 → 友善中文(而非把整包 429 RESOURCE_EXHAUSTED JSON 砸給使用者)。
_QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "quota", "exceeded your current",
                  "rate limit", "RATE_LIMIT")


def _quota_error(exc) -> Optional[HTTPException]:
    """LLM 額度/限流類的例外 → 503 + 白話訊息;其他例外回 None(交給預設處理)。"""
    msg = str(exc)
    if "429" in msg or any(m in msg for m in _QUOTA_MARKERS):
        return HTTPException(
            503, "Gemini 免費額度暫時用完(免費版每日約 20 次;AI 設計師模式一次要 "
                 "2~3 次)。請稍後再試,或在 Render 儀表板改用付費金鑰;不勾 AI 的"
                 "一般模式較省用量,可先用。")
    return None


def _summary(brief, building: BuildingSpec) -> str:
    """給前端顯示的一行摘要:解析出了什麼、蓋了幾層、建築配置的取捨。

    後半段是「設計師的說明」——基地很大時建築不會照抄基地尺寸(房間有
    合理上限,再大就失去尺度),多的地留院子;使用者要看得到這個決策
    (建築多大、院子留多深、有沒有中庭),不然會以為尺寸被無視。
    """
    t = brief.typical
    if isinstance(t, HouseBrief):
        kind = (f"單戶住宅 {t.bedrooms} 房,基地 "
                f"{t.site_width / 1000:.0f}×{t.site_depth / 1000:.0f} 米")
    else:
        kind = (f"集合住宅 每排 {t.units_per_row} 戶,"
                f"走廊 {t.corridor_width / 1000:.1f} 米")
    above = sum(1 for f in building.floors if f.level > 0)
    below = sum(1 for f in building.floors if f.level < 0)
    floors = f"地上 {above} 層" + (f" + 地下 {below} 層" if below else "")
    parts = [kind, floors]

    if isinstance(t, HouseBrief):
        spec = building.floors[-1].spec          # 任一層(外殼各層相同)
        bw, bd = sum(spec.x_spacings), sum(spec.y_spacings)
        parts.append(f"建築 {bw / 1000:.1f}×{bd / 1000:.1f} 米")
        courtyard = next((r.name for fl in building.floors
                          for r in fl.spec.rooms if r.kind == "patio"), None)
        if courtyard:
            parts.append(f"{courtyard}採光")
        ox, oy = spec.grid_origin                 # 建築置中 → 前後/兩側院等深
        front = oy / 1000                         # 基地邊到建築的距離
        side = (t.site_width - ox - bw) / 1000
        yard_bits = []
        if front > 3.5:                           # 比退縮線明顯多才值得說
            yard_bits.append(f"前後院各約 {front:.0f} 米")
        if side > 3.5:
            yard_bits.append(f"兩側院各約 {side:.0f} 米")
        if yard_bits:
            parts.append("、".join(yard_bits) + "(庭園/停車)")
        bals = [b for f in building.floors for b in (f.spec.balconies or [])]
        if bals:                                  # 二樓以上的前後陽台(挑出式)
            parts.append(f"二樓以上前後陽台 {len(bals)} 座"
                         f"(挑出 {bals[0].depth / 1000:.1f} 米)")
    return " · ".join(parts)


_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}


def _suggestions(brief, building: BuildingSpec) -> list[dict]:
    """設計建議:這塊基地還放得下什麼(升級房數/加地下車庫/加蓋樓層)。

    真正的設計師不只交圖,還會告訴業主「其實你的地可以做更多」。每個建議
    附一句完整需求(text),前端做成可點的按鈕——點了直接以該需求重新生成。
    只對透天(HouseBrief)提;數量上限:房 1~4、樓層 4(合理透天規模)。
    """
    t = brief.typical
    if not isinstance(t, HouseBrief):
        return []
    above = sum(1 for f in building.floors if f.level > 0)
    below = sum(1 for f in building.floors if f.level < 0)
    site = (f"基地{t.site_width / 1000:g}×{t.site_depth / 1000:g}米")
    car = ",地下一層車庫" if below else ""

    def req(floors: int, bedrooms: int, with_car: str) -> str:
        head = f"透天{_NUM[floors]}層," if floors >= 2 else ""
        return f"{head}{site},{_NUM[bedrooms]}房{with_car}"

    out: list[dict] = []
    mb = max_house_bedrooms(t)
    if mb > t.bedrooms:
        out.append({
            "label": f"升級 {mb} 房",
            "text": req(above, mb, car),
            "note": f"基地寬度還放得下 {mb} 房,建築會加寬",
        })
    if not below:
        out.append({
            "label": "加地下車庫",
            "text": req(max(above, 2), t.bedrooms, ",地下一層車庫"),
            "note": "地下室作車庫+儲藏,樓梯直通",
        })
    if 2 <= above < 4:
        out.append({
            "label": f"加蓋到 {above + 1} 層",
            "text": req(above + 1, t.bedrooms, car),
            "note": "多一層臥室層,格局與柱位不變",
        })
    return out


def _ai_generate(brief_text: str, brief, client):
    """AI 設計師模式:跑混合式收斂管線 → (BuildingSpec, 額外回應欄位)。

    **連棟透天走「選配版」**(`townhouse_options`):骨架是我們的透天骨架,LLM 只
    在上面挑選項(哪一款核 / 幾層幾房 / 車庫 / 天井 / 鏡射 / 大門位置),照樣跑
    「選→蓋→挑毛病→重選」的收斂迴圈。

    ⚠️ 使用者 2026-08-28:「AI 做的圖不是正確的,不會分成這麼多的間格,AI 只要從
       我做的格局稍加修改變更就好。」自由生關係圖那條路(`design_loop`)會把 7m
       面寬切出兩間「客廳」加一間 0.5㎡ 管道間,不是真實街屋的樣子。骨架放不下的
       尺寸(面寬 >8m)才退回原本的關係圖版。
    """
    from src.design.building_generator import _narrow_to_building
    from src.design.layout.design_loop import design_building
    from src.design.layout.graph_layout import (
        AI_MAX_WIDTH, AI_MIN_DEPTH, AI_MIN_WIDTH)
    from src.design.layout.narrow_house import MIN_WIDTH

    t = brief.typical
    if not isinstance(t, HouseBrief):
        raise ValueError("AI 設計師模式目前只做單戶透天(不吃集合住宅),請關掉此模式")
    # 換算建築尺寸。使用者講「建築物」→ 直接用(基地已由 parser 加了四周退縮,退回
    # 來即得建築)。使用者講「基地」→ 透天共壁不退側院,建築填滿面寬;進深退前院、
    # 貼後界(退一次)。這樣「基地5×10」= 建築 5×8,不再被退縮吃成 1×6。
    if t.dimension_basis == "building":
        bw = t.site_width - 2 * t.setback
        bd = t.site_depth - 2 * t.setback
    else:
        bw = t.site_width                       # 共壁,無側院
        bd = t.site_depth - t.setback           # 前院退縮,後貼界
    # ⚠️ 定義域是**兩條的聯集**:選配版走透天骨架(4~8m),關係圖版走 BSP
    #    (5~30m)。只拿關係圖版的下限去擋,4~5m 的真實街屋(最常見的面寬)會
    #    在這裡就被踢掉,永遠走不到選配版。
    from src.design.layout import townhouse_options as topts
    if not (topts.applicable(bw, bd)
            or (AI_MIN_WIDTH <= bw <= AI_MAX_WIDTH and bd >= AI_MIN_DEPTH)):
        raise ValueError(
            f"AI 設計師模式目前做透天(建築寬 {MIN_WIDTH/1000:.1f}~"
            f"{AI_MAX_WIDTH/1000:.0f} 米、深 ≥{AI_MIN_DEPTH/1000:.0f} 米);你的建築約 "
            f"{bw/1000:.1f}×{bd/1000:.1f} 米,請關掉 AI 模式改用一般生成。")

    if topts.applicable(bw, bd):                # 連棟透天 → 在我們的骨架上選選項
        best, history = topts.design_townhouse(brief_text, bw, bd,
                                               iterations=2, client=client,
                                               verbose=False)
        fl = list(best["floors"])
        env = None                              # 外框由 spec 自己推(建築會封頂)
        style = best["options"]["core_style"]
    else:                                       # 骨架放不下 → 原本的關係圖版
        best, history = design_building(brief_text, bw, bd, iterations=2,
                                        client=client)
        fl = [(lb, sp) for lb, sp, _s, _t in best["floors"]]
        from src.design.layout.design_loop import SETBACK as _SB
        env = (_SB, _SB, _SB + bw, _SB + bd)
        style = "graph"
    building = _narrow_to_building(fl, brief.floor_height)

    # 圖面正確性檢查(硬規則):產線已在每層落實時把關,這裡再驗一次整棟並回報,
    # 讓使用者/我們看得到「這張圖過了哪些檢查」。
    from src.design.layout.code_check import check_code_building
    from src.design.layout.plan_check import check_building
    check = check_building(fl, env)
    code = check_code_building(fl, env)          # 法規尺寸(樓梯/採光…)
    from src.design.layout.balcony import balcony_report
    from src.design.layout.door_rules import door_table
    doors = door_table(fl)                       # 門連通表(房間→門→通往哪裡)

    extra = {
        "engine": "ai",                         # 單一入口:回報實際用了哪個引擎
        "ai_design": True,
        "ai_trajectory": history,               # 每次迭代的分數/問題數/fitness
        "ai_problems": best["problems"],        # 收斂後剩下的問題(可能是物理硬限)
        "door_table": doors.to_dict(),
        "ai_iter": best["iter"],
        "ai_fitness": round(best["fitness"], 1),
        "ai_core_style": style,                 # 選配版挑了哪一款核("graph"=關係圖版)
        "ai_options": best.get("options"),      # 選配版:LLM 實際採用的那組選項
        "plan_check": check.to_dict(),          # 圖面檢查:ok / 錯誤數 / 警告數 / 明細
        "code_check": code.to_dict(),           # 法規檢查:建築技術規則尺寸
        "balconies": balcony_report(fl).to_dict(),   # 陽台清單(AI 版目前不配)
    }
    return building, extra


def _ai_applicable(brief) -> bool:
    """這份需求適不適合走 AI 設計師(單戶透天、尺寸在搜尋引擎的定義域內)。"""
    from src.design.layout.graph_layout import (
        AI_MAX_WIDTH, AI_MIN_DEPTH, AI_MIN_WIDTH)

    t = brief.typical
    if not isinstance(t, HouseBrief):
        return False                                # 集合住宅走規則版
    if t.dimension_basis == "building":
        bw, bd = t.site_width - 2 * t.setback, t.site_depth - 2 * t.setback
    else:
        bw, bd = t.site_width, t.site_depth - t.setback
    from src.design.layout import townhouse_options as topts
    return (topts.applicable(bw, bd)                    # 選配版(透天骨架)
            or (AI_MIN_WIDTH <= bw <= AI_MAX_WIDTH      # 關係圖版(BSP)
                and bd >= AI_MIN_DEPTH))


# 硬錯誤代碼 → 給使用者看的白話說明(圖面/法規/門與動線三道關卡共用)。
_ERROR_HELP = {
    "room_no_door": "有房間沒有門,進不去",
    "floor_split": "同一層的室內被切成好幾塊,彼此走不通",
    "no_entry": "一樓沒有對外大門",
    "entry_upstairs": "樓上的外牆開了門(門會通往空中)",
    "furniture_in_wall": "家具嵌進牆裡(畫出來是穿牆)",
    "opening_on_column": "門窗開口壓在柱子上(柱會穿過窗框/門洞)",
    "door_in_corner": "門卡在房間角落,人走不進那個角",
    "stair_blocks_door": "門直接開在階梯上,門前沒有站人的平地",
    "stair_side_open": "樓梯有一側沒有牆,走上去會從旁邊掉下去",
    "circulation_blocked": "房間走不進去,或家具擋死動線",
    "entry_door_narrow": "對外大門淨寬不足 90 公分",
    "room_door_narrow": "房門淨寬不足(居室 80 公分、衛浴 75 公分)",
    "door_swing_blocked": "門打開會撞到牆、柱、家具或另一扇門",
    "bath_door_to_kitchen": "衛浴的門直接開向廚房",
    "through_bedroom": "有空間要穿過別人的臥室才進得去",
    "stair_wrapped": "樓梯被房間包住,上下樓要穿過私人房間",
    "stair_riser": "樓梯級高超過法規上限(20 公分)",
    "stair_tread": "樓梯級深不足法規下限(21 公分)",
    "stair_width": "梯段淨寬不足法規下限(75 公分)",
    "stair_landing": "樓梯平臺深度小於梯段寬(轉身空間不足)",
    "daylight_area": "居室採光開口不足樓地板面積的 1/8",
    "vent_area": "居室通風開口不足樓地板面積的 1/20",
}


def _supported_sizes(setback: float = 2000.0) -> str:
    """目前**保證生得出合格圖**的建築尺寸(從各產線常數算,不要寫死)。"""
    from src.design.layout.narrow_house import (
        MAX_WIDTH as NW_MAX, MIN_WIDTH as NW_MIN, min_depth_for)
    from src.design.layout.shallow_house import (
        MAX_WIDTH as SH_MAX, MIN_DEPTH as SH_MIN_D, MIN_WIDTH as SH_MIN)
    sb = setback / 1000.0
    return (
        "目前保證生得出合格圖的是**單戶透天**,建築尺寸:\n"
        f"  ・淺進深型:{SH_MIN/1000:g}~{SH_MAX/1000:g} 米寬 × "
        f"{SH_MIN_D/1000:g} 米以上深\n"
        f"  ・一般透天:{NW_MIN/1000:g}~{NW_MAX/1000:g} 米寬 × "
        f"{min_depth_for(NW_MIN)/1000:g}~{min_depth_for(NW_MAX)/1000:g} 米以上深"
        "(面寬越寬,要求的進深越大)\n"
        f"講「基地」尺寸的話,再加上四周退縮各 {sb:g} 米。"
    )


def _reject_if_broken(extra: dict, brief) -> None:
    """圖面檢查沒過就**不出圖**——寧可講清楚,也不要給一張走不通的平面圖。

    這裡只擋 error(換個切法就能解的硬錯誤);warning 是設計取捨,照樣出圖。"""
    rep = extra.get("plan_check") or {}
    code = extra.get("code_check") or {}
    errors = [i for i in rep.get("issues", []) if i.get("severity") == "error"]
    # 法規違規(§33/§40/§43)同樣不出圖:那是「不合法」,比畫錯更嚴重。
    errors += [i for i in code.get("issues", [])
               if i.get("severity") == "violation"]
    if not errors:
        return
    seen, lines = set(), []
    for i in errors:
        key = (i.get("floor", ""), i.get("code", ""))
        if key in seen:
            continue
        seen.add(key)
        who = i.get("floor", "")
        if i.get("room"):
            who += "/" + i["room"]
        lines.append(f"  ・{who}:{_ERROR_HELP.get(i.get('code'), i.get('code'))}")
        if len(lines) >= 6:
            break
    more = ("\n  ・(還有其他問題,先修上面這幾項)"
            if len(errors) > len(lines) else "")
    setback = getattr(getattr(brief, "typical", None), "setback", 2000.0)
    raise HTTPException(
        422,
        "這個尺寸目前生不出合格圖,所以不出圖(出了也是不能用的平面圖)。\n"
        "檢查沒過的地方:\n" + "\n".join(lines) + more + "\n\n"
        + _supported_sizes(setback))


def _generate_auto(brief_text: str, brief, client, force_ai: bool = False):
    """**單一入口**:自動挑引擎生圖 → (BuildingSpec, 額外回應欄位)。

    規則:合用就走 AI 設計師(LLM 設計房間關係 → 搜尋落實 → 收斂),它畫得比較好;
    不合用(寬基地/集合住宅)或 AI 這條出任何狀況(額度用完、斷網、落實失敗)就
    **自動退回規則產生器**——使用者不必知道有幾種引擎,也不會因為 AI 掛掉就沒圖。

    force_ai=True:明確要求 AI(不合用時照樣報錯,方便測試/除錯)。

    ⚠️ 兩條路徑最後都會過 _reject_if_broken:圖面檢查有硬錯誤就**不出圖**,
       改回 422 + 白話說明 + 目前生得出來的尺寸範圍。
    """
    if force_ai:
        building, extra = _ai_generate(brief_text, brief, client)
        _reject_if_broken(extra, brief)
        return building, extra

    if _ai_applicable(brief):
        try:
            building, extra = _ai_generate(brief_text, brief, client)
            _reject_if_broken(extra, brief)          # AI 的圖也要過關卡
            return building, extra
        except Exception:
            # 不合格 / 額度用完 / 斷網 / 落實失敗 → 都退回規則版再試一次。
            # (規則版的圖最後同樣會過 _reject_if_broken,不會因此漏出壞圖。)
            pass

    building = generate_building_auto(brief)
    extra = {"engine": "rule"}
    try:                                            # 規則版也送圖面檢查(同一套標準)
        from src.design.layout.code_check import check_code_building
        from src.design.layout.plan_check import check_building
        floors = [(f.label, f.spec) for f in building.floors]
        # 外框由各層 spec 自己推(env=None):深基地會**封頂建築、留前後院**,
        # 用「基地−退縮」當外框會把大門/窗誤判成不在外牆上。
        extra["plan_check"] = check_building(floors).to_dict()
        extra["code_check"] = check_code_building(
            floors, None, brief.floor_height).to_dict()
        from src.design.layout.balcony import balcony_report
        from src.design.layout.door_rules import door_table
        extra["door_table"] = door_table(floors).to_dict()   # 房間→門→通往哪裡
        extra["balconies"] = balcony_report(floors).to_dict()   # 二樓以上的前後陽台
    except Exception:                               # 檢查本身壞掉不該擋出圖
        pass
    _reject_if_broken(extra, brief)                 # 有硬錯誤 → 422,不出壞圖
    return building, extra


def create_app(client_factory: Optional[Callable[[], object]] = None) -> FastAPI:
    """建立應用。client_factory 注入假 Gemini client(測試用);None = 真的。

    DEMO_MODE=1 且沒有注入 client 時,改用**離線回放**的錄音客戶端
    (src/web/demo_client):產線照跑,只有 LLM 的回覆是錄好的 —— 免費額度用完
    或斷網也能完整示範 AI 設計師模式。回應會帶 demo=True,前端要標示出來。"""
    from src.web.demo_client import DemoClient, demo_enabled

    if client_factory is None and demo_enabled():
        client_factory = DemoClient
    app = FastAPI(title="自動建築平面圖生成器")

    @app.get("/api/config")
    def config() -> dict:
        return {
            "needs_code": bool(os.environ.get("ACCESS_CODE")),
            "has_api_key": _has_api_key() or client_factory is not None,
            "demo": demo_enabled(),             # 前端顯示「示範模式(離線回放)」
        }

    @app.post("/api/generate")
    def generate(req: GenerateRequest) -> dict:
        access_code = os.environ.get("ACCESS_CODE")
        if access_code and req.code != access_code:
            raise HTTPException(403, "通行碼錯誤")

        client = client_factory() if client_factory else None
        if client is None and not _has_api_key():
            raise HTTPException(
                503, "伺服器沒設定 GEMINI_API_KEY,無法解析需求描述")

        # 設計變體種子(E2):沒帶就隨機抽一個 → 每次「重新設計」換方案。
        # 多輪修改預設沿用上一輪 seed(前端會帶),格局才不會整個重骰。
        seed = req.seed if req.seed is not None else random.randrange(1_000_000)

        # 1) 解析需求(LLM)——語意錯誤 422;網路/額度問題 502。
        #    多輪修改(帶 base):text 是修改指令,以 base 為底合併(E4)。
        try:
            if req.base is not None:
                data = parse_modification_data(req.text, req.base, client=client)
            else:
                data = parse_brief_data(req.text, client=client)
            brief = building_brief_from_data(data, seed=seed)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise (_quota_error(exc)
                   or HTTPException(502, f"需求解析服務暫時無法使用:{exc}")) from exc

        # 2) 生成格局 + 出圖——設計檢核不過(基地太小等)一樣回 422 給使用者看。
        #    **單一入口**:自動選引擎(合用就走 AI 設計師,否則規則產生器),
        #    AI 這條失敗(額度/尺寸不合/意外)就自動退回規則版,不讓使用者看到錯誤。
        try:
            building, ai_extra = _generate_auto(req.text, brief, client,
                                                force_ai=req.ai_design)
            sheets = build_sheets(building)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:                 # AI 模式的二次 LLM 呼叫也可能失敗
            raise (_quota_error(exc)
                   or HTTPException(502, f"生成服務暫時無法使用:{exc}")) from exc

        # 3) 存檔(DXF + 打包 zip + PDF 圖冊)+ 組回應(SVG 直接內嵌 JSON)
        job_id = uuid.uuid4().hex[:12]
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        out_sheets, budget, inline_n = [], INLINE_DXF_BUDGET, 0
        for s in sheets:
            path = job_dir / s.filename
            s.doc.saveas(path)
            item = {
                "label": s.label,
                "kind": s.kind,
                "svg": sheet_svg(s),
                "dxf": f"/api/jobs/{job_id}/{s.filename}",   # 備援(硬碟還在時)
            }
            raw = path.read_bytes()
            if len(raw) <= budget:                  # 內嵌到預算用完為止
                item["dxf_b64"] = base64.b64encode(raw).decode("ascii")
                budget -= len(raw)
                inline_n += 1
            out_sheets.append(item)
        with zipfile.ZipFile(job_dir / "all_dxf.zip", "w",
                             zipfile.ZIP_DEFLATED) as zf:
            for s in sheets:
                zf.write(job_dir / s.filename, s.filename)

        try:
            metrics = building_metrics(building)          # 關鍵數字(E4)
        except Exception:                                 # AI 版 spec 邊角 → 不擋出圖
            metrics = {}
        if ai_extra.get("ai_design"):                # 只有 AI 引擎才有收斂軌跡
            note = (f"AI 設計師:{len(ai_extra['ai_trajectory'])} 次迭代擇優"
                    f"(fitness {ai_extra['ai_fitness']},剩 "
                    f"{len(ai_extra['ai_problems'])} 個待改)")
        else:
            note = house_design_note(brief.typical)
        result = {
            "job_id": job_id,
            "seed": seed,
            "summary": _summary(brief, building),
            "design_note": note,
            "metrics": metrics,
            "brief_data": data,                          # 多輪修改的底(E4)
            "suggestions": ([] if ai_extra.get("ai_design")
                            else _suggestions(brief, building)),
            "demo": demo_enabled(),                      # 這次是不是離線回放
            "sheets": out_sheets,
            "zip": f"/api/jobs/{job_id}/all_dxf.zip",
            # 有幾張圖的 DXF 直接內嵌在這份回應裡(其餘要走 job_id 連結,
            # 而那條在伺服器重新部署後可能失效)。
            "dxf_inline": inline_n,
            "dxf_inline_all": inline_n == len(out_sheets),
            "pdf": f"/api/jobs/{job_id}/pdf",            # 點了才產生(懶生成)
            **ai_extra,                                  # AI 模式:收斂軌跡/剩餘問題
        }

        # 4) 歷史方案(E4):整包回應存 result.json(重新載入用)、
        #    摘要存 meta.json(列表用;files = PDF 懶生成的頁序)。
        (job_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")
        (job_dir / "meta.json").write_text(json.dumps({
            "job_id": job_id,
            "text": req.text,
            "seed": seed,
            "summary": result["summary"],
            "created": datetime.now(timezone.utc).isoformat(),
            "files": [s.filename for s in sheets],
        }, ensure_ascii=False), encoding="utf-8")
        return result

    @app.post("/api/score")
    def score(req: ScoreRequest) -> dict:
        """家具配置評分(Phase 6-7)。

        拿已生成方案的需求(brief_data + seed)重建同一棟樓,對「產生器擺好的
        佈局」**就地評分**,回傳整棟等第 + 12 項子分數 + 各層/各房檢查。

        ⚠️ **不搬家具、不另存圖**:產生器的家具擺位已是精心設計(實測 A+),
        重排反而會打散變差,故本端點只讀不動——畫面上的圖維持原樣,只多一張
        評分卡。
        """
        access_code = os.environ.get("ACCESS_CODE")
        if access_code and req.code != access_code:
            raise HTTPException(403, "通行碼錯誤")

        if not _JOB_ID_RE.fullmatch(req.job_id):
            raise HTTPException(404, "找不到方案")
        result_path = JOBS_DIR / req.job_id / "result.json"
        if not result_path.is_file():
            raise HTTPException(404, "方案不存在(可能已清除,請重新生成)")

        saved = json.loads(result_path.read_text(encoding="utf-8"))
        brief_data = saved.get("brief_data")
        if not brief_data:
            raise HTTPException(422, "此方案無法評分(缺需求資料),請重新生成")

        # 用同一份需求 + seed 重建同一棟樓(決定性),就地評分。
        try:
            brief = building_brief_from_data(
                brief_data, seed=saved.get("seed", 0))
            building = generate_building_auto(brief)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        report = score_report(building, name=req.job_id)
        report["job_id"] = req.job_id
        return report

    @app.get("/api/jobs/{job_id}/pdf")
    def job_pdf(job_id: str) -> FileResponse:
        """A3 PDF 圖冊——第一次點才從已存的 DXF 渲染(之後直接用快取檔)。"""
        if not _JOB_ID_RE.fullmatch(job_id):
            raise HTTPException(404, "找不到方案")
        job_dir = JOBS_DIR / job_id
        pdf_path = job_dir / "plans.pdf"
        if not pdf_path.is_file():
            meta_path = job_dir / "meta.json"
            if not meta_path.is_file():
                raise HTTPException(404, "方案不存在(可能已清除,請重新生成)")
            import ezdxf
            files = json.loads(meta_path.read_text(encoding="utf-8"))["files"]
            docs = [ezdxf.readfile(job_dir / f) for f in files
                    if (job_dir / f).is_file()]
            if not docs:
                raise HTTPException(404, "圖檔不存在(可能已清除,請重新生成)")
            docs_to_pdf(docs, pdf_path)
        return FileResponse(pdf_path, filename="plans.pdf")

    @app.get("/api/history")
    def history() -> list[dict]:
        """最近的生成紀錄(新→舊,最多 HISTORY_LIMIT 筆)。"""
        metas = []
        if JOBS_DIR.is_dir():
            for meta_file in JOBS_DIR.glob("*/meta.json"):
                try:
                    metas.append(json.loads(meta_file.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue                     # 壞檔跳過,列表不因一筆爛掉
        metas.sort(key=lambda m: m.get("created", ""), reverse=True)
        return metas[:HISTORY_LIMIT]

    @app.get("/api/jobs/{job_id}/result")
    def job_result(job_id: str) -> dict:
        """重新載入一筆歷史方案(整包回應,含 SVG/連結/數字)。"""
        if not _JOB_ID_RE.fullmatch(job_id):
            raise HTTPException(404, "找不到方案")
        path = JOBS_DIR / job_id / "result.json"
        if not path.is_file():
            raise HTTPException(404, "方案不存在(可能已清除,請重新生成)")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/jobs/{job_id}/{filename}")
    def download(job_id: str, filename: str) -> FileResponse:
        if not (_JOB_ID_RE.fullmatch(job_id)
                and _FILENAME_RE.fullmatch(filename)):
            raise HTTPException(404, "找不到檔案")
        path = JOBS_DIR / job_id / filename
        if not path.is_file():
            raise HTTPException(404, "檔案不存在(可能已清除,請重新生成)")
        return FileResponse(path, filename=filename)

    # 前端(放最後,才不會蓋掉 /api/*)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()          # uvicorn src.web.app:app 的進入點
