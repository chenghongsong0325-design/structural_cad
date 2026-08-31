"""AI 選配版:讓 LLM 在**我們的透天骨架**上做選擇,不要自己重畫格局。

使用者 2026-08-28 指著 AI 產線畫出來的圖說:

    「我要讓 AI 做我這種方案 A、B,不要按照他的做法,他做的圖不是正確的,
      不會分成這麼多的間格,AI 只要從我做的格局稍加修改變更就好。」

原本的 AI 產線(`room_graph` → `graph_layout`)是**讓 LLM 自由設計房間關係圖**,
再用 BSP 搜尋落實。問題不在搜尋,在前提:LLM 沒看過台灣連棟街屋長什麼樣,它會
把 4~8m 面寬的房子切成一堆小格子(實測 7m 面寬切出兩間「客廳」加一間 0.5㎡ 的
管道間),那不是真實街屋的樣子。

這支把 LLM 的職責換掉:**骨架是固定的**(`narrow_house` 的前後串聯 + 中段核),
LLM 只挑「一個設計師會挑的那幾個選項」——幾層、幾房、哪一款核、要不要車庫/
天井、樓梯核擺哪一側…… 然後照樣跑「落實→挑毛病→重選→留最高分」的收斂迴圈。

**做得到的變化仍然很多**(3 款核 × 鏡射 × 開放餐廚 × 大門位置 × 車庫 × 天井 ×
樓層 × 房數),但每一種變化都仍然是一張真實街屋的平面 —— 這正是使用者要的
「從我做的格局稍加修改」。

⚠️ LLM 回來的值一律要過 `normalize_options`:它會給出 10 層樓、3.5m 面寬配車庫
   這種東西。夾不住的組合由 `build_from_options` 的退讓階梯收掉(車庫 → 天井 →
   核的款式),**寧可少一個加分選項,也不要生不出圖**(本檔在 AGENTS.md 的鐵則)。
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.layout.narrow_house import (  # noqa: E402
    MAX_WIDTH, MIN_WIDTH, NarrowVariant, garage_min_depth,
    generate_narrow_building, min_depth_for,
)
from src.design.layout.room_graph import MODEL  # noqa: E402

#: 收斂迴圈的 fitness:平均分 − 這個係數 × 問題數(與 design_loop 同一把尺)。
FITNESS_PROBLEM_COST = 2.0
#: 大門在南牆上的位置比例,只給這三個(與 `narrow_house._ENTRY_FRACS` 同一組)。
ENTRY_FRACS = (0.22, 0.5, 0.78)
CORE_STYLES = ("default", "mid", "ref")

TOWNHOUSE_OPTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "core_style": {
            "type": "string",
            "enum": list(CORE_STYLES),
            "description": "中段核要哪一款(見說明的方案 A / A' / B)",
        },
        "floors": {"type": "integer", "description": "樓層數 1~4"},
        "bedrooms": {"type": "integer", "description": "臥室數 1~4"},
        "garage": {"type": "boolean", "description": "1F 前段做車庫(客廳上 2F)"},
        "patio": {"type": "boolean", "description": "中段開天井(只有方案 B 有)"},
        "mirror": {"type": "boolean", "description": "整層左右鏡射(樓梯核換邊)"},
        "open_kitchen": {"type": "boolean",
                         "description": "1F 後段做開放餐廚(否則隔成餐廳+廚房)"},
        "entry_frac": {"type": "number",
                       "description": "大門在臨路牆上的位置:0.22 偏西 / 0.5 置中 "
                                      "/ 0.78 偏東"},
        "rationale": {"type": "string", "description": "一句話說明為什麼這樣選"},
    },
    "required": ["core_style", "floors", "bedrooms", "garage", "patio",
                 "mirror", "open_kitchen", "entry_frac", "rationale"],
}

DESIGNER_PROMPT = """\
你是台灣透天厝(連棟街屋)的設計師。**平面的骨架已經定好了,你不要重畫格局**,
你的工作是在這個骨架上做「一個設計師會做的那幾個選擇」。

骨架(每一層都一樣,前後串聯、共同壁不開窗、只有前後兩端對外):

    臨路 → 前段(客廳 / 主臥 / 臥室,有車庫時 1F 前段整段停車)
         → 中段核(樓梯 + 浴廁 + 走道,每層同位置,樓梯上下對齊)
         → 後段(1F 是餐廚,樓上是臥室)
         → 後院

中段核有三款,差別只在核裡怎麼排:

  方案 A  ("default")  浴廁 | 樓梯 | 走道
      走道貼著界牆,廁所在最外側。廁所的門開向餐廚。前後段最容易切成兩間房。
  方案 A' ("mid")      樓梯 | 浴廁 | 走道
      廁所夾在樓梯與走道中間,**門直接開在走道上**(比較好用)。
      樓梯旁不必再留走道,梯段可以做寬一點 → 樓梯比較不陡。
  方案 B  ("ref")      樓梯(橫置) + (天井 | 廁所) + 走道
      樓梯橫著擺,只吃掉一小段進深;中段有**天井**採光,廁所的門也開在走道上。
      代價:走道只有一扇門的寬度,前後段**不能**再左右切成兩間。

選擇的原則:
- **方案 B 是使用者自己畫的參考平面** —— 沒有明確理由選別款就選它。
- 房間數少、要中段有採光 → 方案 B(有天井)。
- 一般情形、想要廁所好用又不想樓梯太陡 → 方案 A'。
- 面寬寬(7~8m)**而且**使用者明講要多一間房 → 方案 A(只有它切得成兩間)。
- 使用者明講要車庫才給 garage;車庫會讓 1F 沒有客廳(客廳上 2F),而且需要
  建築進深夠深、面寬 4m 以上。
- 天井只有方案 B 放得下,其他兩款給 false。
- 樓層數與臥室數照使用者的需求;沒講就給 3 層 3 房(台灣透天最常見)。

**絕對不要**做這些事:自己發明房間、把一層切成很多小格子、加管道間、加儲藏室、
指定尺寸或座標。骨架會自己處理這些;你只回上面那幾個選項。
"""

REFINE_PROMPT = DESIGNER_PROMPT + """

修正模式:輸入含「上一版選項(JSON)」與「畫出來之後發現的問題」兩段。請針對問題
換一組選項,輸出**完整**的選項(所有欄位齊全)。常見對策:

- 某房太大 → 房間數加一(多一層或多一間臥室),或改用方案 A(它切得成兩間)。
- 中段沒採光 / 想要天井 → 改用方案 B。
- 廁所的門開向餐廚很怪 → 改用方案 A' 或 B。
- 樓梯太陡 → 改用方案 A'(梯段做得比較寬)。
- 生不出來 / 放不下 → 拿掉車庫或天井,或減少樓層/房間。

保留原本合理的選擇,只動跟問題有關的那幾項。
"""


# ---------------------------------------------------------------------------
# LLM 兩支呼叫(與 room_graph 同一套介面:client 可注入假物件,測試不打 API)
# ---------------------------------------------------------------------------
def _dims_note(width: float, depth: float) -> str:
    return (f"\n\n(這棟的**建築物**尺寸約 {width / 1000:.1f}×{depth / 1000:.1f} 米。"
            f"面寬 4~5 米最常見,6~8 米算寬;車庫需要進深 "
            f"{(garage_min_depth() + 7600) / 1000:.1f} 米以上。)")


def propose_options(brief_text: str, *, width: float, depth: float,
                    client: Optional[object] = None,
                    temperature: float = 0.8) -> dict:
    """需求描述 → 骨架上的選項 dict(不畫圖)。

    client 可注入假物件(測試用);None 時建真的 Gemini 客戶端,需 GEMINI_API_KEY。
    """
    if not brief_text or not brief_text.strip():
        raise ValueError("需求描述是空的")
    if client is None:
        from src.design.api_keys import make_client
        client = make_client()          # 多把金鑰輪替(見 api_keys 模組說明)
    response = client.models.generate_content(
        model=MODEL,
        contents=brief_text + _dims_note(width, depth),
        config={
            "system_instruction": DESIGNER_PROMPT,
            "response_mime_type": "application/json",
            "response_schema": TOWNHOUSE_OPTIONS_SCHEMA,
            "temperature": temperature,
        },
    )
    return json.loads(response.text)


def refine_options(prev: dict, problems: list, *, width: float, depth: float,
                   client: Optional[object] = None,
                   temperature: float = 0.6) -> dict:
    """上一版選項 + 問題清單 → 改良後的選項(收斂迴圈的「重選」那步)。"""
    if client is None:
        from src.design.api_keys import make_client
        client = make_client()
    contents = ("上一版選項(JSON):\n" + json.dumps(prev, ensure_ascii=False)
                + "\n\n畫出來之後發現的問題:\n"
                + "\n".join(f"- {p}" for p in problems)
                + _dims_note(width, depth))
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config={
            "system_instruction": REFINE_PROMPT,
            "response_mime_type": "application/json",
            "response_schema": TOWNHOUSE_OPTIONS_SCHEMA,
            "temperature": temperature,
        },
    )
    return json.loads(response.text)


# ---------------------------------------------------------------------------
# 收下 LLM 的答案:夾住 → 蓋 → 蓋不出來就一級一級退
# ---------------------------------------------------------------------------
def _nearest(value: float, choices) -> float:
    return min(choices, key=lambda c: abs(c - value))


def normalize_options(opts: dict, *, width: float, depth: float) -> dict:
    """把 LLM 給的選項夾成**一定合法**的一組。

    ⚠️ LLM 會給 10 層樓、3.5m 面寬配車庫、方案 A 配天井這種東西。schema 擋得住
    型別,擋不住「物理上不可能」——那要拿骨架自己的常數去夾(單一出處在
    `narrow_house` 檔頭,不要在這裡另抄數字)。
    """
    out = dict(opts or {})
    # ⚠️ 預設是**方案 B**:那是使用者 2026-08-28 給的參考平面
    #    (前院|客廳|樓梯橫置+天井/廁所+走道|餐廚|後院),他兩次指著它說
    #    「照我圖這樣排」。排不下時 `build_from_options` 會自己退回別款。
    style = str(out.get("core_style") or "ref")
    out["core_style"] = style if style in CORE_STYLES else "ref"
    out["floors"] = max(1, min(4, int(out.get("floors") or 3)))
    out["bedrooms"] = max(1, min(4, int(out.get("bedrooms") or 3)))
    out["mirror"] = bool(out.get("mirror"))
    out["open_kitchen"] = bool(out.get("open_kitchen", True))
    out["entry_frac"] = _nearest(float(out.get("entry_frac") or 0.22),
                                 ENTRY_FRACS)
    # 天井只有方案 B 的核放得下(另外兩款把面寬用完了,見 narrow_house._core_mid)。
    out["patio"] = bool(out.get("patio")) and out["core_style"] == "ref"
    # 車庫:一層樓不能配(會連客廳都沒有)、進深不夠也不行 —— 這兩條 narrow_house
    # 會 raise,先在這裡夾掉,免得每次都要靠退讓階梯補救。
    garage = bool(out.get("garage"))
    if garage and (out["floors"] < 2 or depth < min_depth_for(width, True)):
        garage = False
    out["garage"] = garage
    return out


def build_from_options(width: float, depth: float, opts: dict, *,
                       seed: int = 7, furnish: bool = True):
    """照選項蓋 → [(樓層標示, FloorPlanSpec)];蓋不出來就**一級一級退**。

    退讓順序 = 由「加分項」往「必要項」退:車庫 → 天井 → 核的款式(回預設核)。
    ⚠️ 這條鐵則在 AGENTS.md 已經第七次登場:**加分項不得讓原本生得出來的案子
       生不出來**。LLM 選了一個放不下的組合時,要靜靜地退,不是把錯誤丟給使用者。

    ⚠️ 「蓋不出來」不只是 raise:有些組合**蓋得出來但圖不合格**(實測 3.6m 面寬
       配方案 B,某些變體會生出「餐廚沒有門」)。判準因此是 `plan_check` 過不過,
       不是有沒有丟例外 —— 只看例外的話,退讓階梯對這種案子完全不會啟動。
       全部都不合格時回**第一個蓋得出來的**,讓呼叫端的關卡去回報(不要 raise:
       那會讓使用者連一張可以看的圖都拿不到)。
    """
    from src.design.layout.plan_check import check_building

    o = normalize_options(opts, width=width, depth=depth)
    tries = [o]
    if o["garage"]:
        tries.append({**tries[-1], "garage": False})
    if o["patio"]:
        tries.append({**tries[-1], "patio": False})
    if o["core_style"] != "default":
        tries.append({**tries[-1], "core_style": "default"})
    last: Exception | None = None
    fallback = None
    for t in tries:
        variant = NarrowVariant(mirror=t["mirror"], bath_north=False,
                                open_kitchen=t["open_kitchen"],
                                entry_frac=t["entry_frac"])
        try:
            floors = generate_narrow_building(
                width, depth, floors=t["floors"], bedrooms=t["bedrooms"],
                furnish=furnish, variant=variant, patio=t["patio"],
                garage=t["garage"], core_style=t["core_style"])
        except ValueError as exc:
            last = exc
            continue
        if check_building(floors).ok:
            return floors, t
        if fallback is None:
            fallback = (floors, t)
    if fallback is not None:
        return fallback
    raise last if last is not None else ValueError("選項組不出圖")


def applicable(width: float, depth: float) -> bool:
    """這個尺寸走不走得了這條產線(骨架的定義域,單一出處在 narrow_house)。"""
    return MIN_WIDTH <= width <= MAX_WIDTH and depth >= min_depth_for(width)


# ---------------------------------------------------------------------------
# 收斂迴圈:選 → 蓋 → 挑毛病 → 重選 → 留最高分(與 design_loop 同一個骨架)
# ---------------------------------------------------------------------------
def design_townhouse(brief: str, width: float, depth: float, *,
                     iterations: int = 2, client: Optional[object] = None,
                     seed: int = 7, verbose: bool = True):
    """選項版的雙向收斂 → (best, history)。

    best = {iter, fitness, mean_score, options, floors, problems}。
    每次都用同一個 seed 落實 → 分數變化來自「選得比較好」,不是切法運氣
    (與 `design_loop.design_building` 同一個設計決定)。
    """
    from src.design.layout.design_loop import critique_building
    from src.design.layout.global_score import score_report
    from src.design.layout.plan_check import building_env

    if not applicable(width, depth):
        raise ValueError(
            f"AI 選配版做連棟透天(建築寬 {MIN_WIDTH / 1000:.1f}~"
            f"{MAX_WIDTH / 1000:.1f} 米、深 ≥{min_depth_for(width) / 1000:.1f} 米);"
            f"你的建築約 {width / 1000:.1f}×{depth / 1000:.1f} 米。")

    opts = propose_options(brief, width=width, depth=depth, client=client)
    best, history = None, []
    for it in range(iterations):
        try:
            floors, used = build_from_options(width, depth, opts, seed=seed)
        except ValueError:
            if best is not None:
                break                          # 已有較早的最佳 → 用它
            raise
        scores = [score_report(sp)["overall_score"] for _, sp in floors]
        mean_score = sum(scores) / len(scores)
        env = building_env(floors[0][1])
        problems = critique_building([(lb, sp, 0, 0) for lb, sp in floors], env)
        fitness = mean_score - FITNESS_PROBLEM_COST * len(problems)
        history.append({"iter": it, "mean_score": mean_score,
                        "n_problems": len(problems), "fitness": fitness,
                        "options": used})
        if best is None or fitness > best["fitness"]:
            best = {"iter": it, "fitness": fitness, "mean_score": mean_score,
                    "options": used, "floors": floors, "problems": problems}
        if verbose:
            print(f"  迭代 {it}: 方案 {used['core_style']}  平均分 {mean_score:.0f}"
                  f"  問題 {len(problems)} 個  fitness {fitness:.0f}")
            for p in problems:
                print(f"       · {p}")
        if not problems or it == iterations - 1:
            break
        try:
            opts = refine_options(used, problems, width=width, depth=depth,
                                  client=client)
        except Exception:                      # 重選失敗(額度/網路)→ 用目前最佳
            break
    return best, history


def main(argv: Optional[list[str]] = None) -> None:
    """`python -m src.design.layout.townhouse_options "透天三層三房" 4.5 15`"""
    import argparse

    ap = argparse.ArgumentParser(description="AI 選配版:在透天骨架上讓 LLM 選選項")
    ap.add_argument("brief")
    ap.add_argument("width", type=float, help="建築面寬(公尺)")
    ap.add_argument("depth", type=float, help="建築進深(公尺)")
    ap.add_argument("--iterations", type=int, default=2)
    args = ap.parse_args(argv)
    best, _hist = design_townhouse(args.brief, args.width * 1000,
                                   args.depth * 1000,
                                   iterations=args.iterations)
    print(json.dumps(best["options"], ensure_ascii=False, indent=2))


if __name__ == "__main__":               # pragma: no cover
    main()
