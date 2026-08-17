"""LLM 當「設計師」的試水溫(Spike):需求 → 房間關係圖(不畫圖)。

驗證「混合式 AI 建築師」的第 1 步——發散提案:讓 Gemini 針對一段需求,
自由提出「要哪些房間、放哪層、誰挨著誰、大門開進哪一間」的**拓撲**,
完全不碰尺寸/座標。目的只是回答一個問題:

    這條路通不通?——LLM 提的格局拓撲會不會「每次都不一樣、而且合理」?

若通,下一步才把這張拓撲圖餵給既有引擎(rooms_to_spec 那套)落實成真圖。

跟 nl_parser 同一套 Gemini 呼叫(genai.Client + 結構化輸出 response_schema),
差別只在:這裡要的是「格局點子」,不是「解析既有句子的欄位」。

用法::

    python src/design/layout/room_graph.py "三代同堂,要中庭,透天三層"
    python src/design/layout/room_graph.py            # 用預設需求跑 4 次比多樣性

需要 GEMINI_API_KEY(同 nl_parser)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# 房間種類:對齊產生器已認得的 kind(living/dining/kitchen/bedroom/bathroom/
# stair/storage/study…),多幾個生活化的(elder_room/garage/balcony)。
# ⚠️ 沒有 patio(天井)、也沒有 storage/utility(儲藏室):住宅一律不設天井
#    (2026-07-29)、不設獨立儲藏室(2026-07-30),兩者都由使用者定調,LLM 連提
#    都不該提;真的提了也會在落實時丟掉(見 graph_layout._realize_floor_core)。
ROOM_KINDS = [
    "living", "dining", "kitchen", "bedroom", "master_bedroom", "bathroom",
    "toilet", "stair", "study", "elder_room",
    "garage", "balcony", "corridor",
]

# 結構化輸出的「填空表格」:LLM 只准回這個形狀(房間清單 + 相鄰邊 + 大門)。
ROOM_GRAPH_SCHEMA = {
    "type": "object",
    "properties": {
        "rooms": {
            "type": "array",
            "description": "這棟房子要哪些房間(不含尺寸座標)",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "唯一代號,如 living、bed_master、bath_2f",
                    },
                    "kind": {"type": "string", "enum": ROOM_KINDS},
                    "floor": {
                        "type": "integer",
                        "description": "樓層:1=一樓、2=二樓…、-1=地下一層",
                    },
                    "wants_daylight": {
                        "type": "boolean",
                        "description": "是否需要對外採光(客廳/臥室=true;"
                                       "浴廁/走道/樓梯間通常=false)",
                    },
                },
                "required": ["id", "kind", "floor", "wants_daylight"],
            },
        },
        "adjacencies": {
            "type": "array",
            "description": "哪兩間要挨在一起 / 相通(用 id 指涉上面的房間)",
            "items": {
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                    "connection": {
                        "type": "string",
                        "enum": ["door", "open", "near"],
                        "description": "door=有門相通;open=開放連通(客餐廳);"
                                       "near=只是相鄰不一定開門",
                    },
                },
                "required": ["a", "b", "connection"],
            },
        },
        "entry": {
            "type": "string",
            "description": "臨路大門開進哪一間的 id(通常是 living 或玄關所在)",
        },
        "rationale": {
            "type": "string",
            "description": "一句話說明這個格局的邏輯(給人看的)",
        },
    },
    "required": ["rooms", "adjacencies", "entry", "rationale"],
}

DESIGNER_PROMPT = """\
你是台灣住宅建築師。使用者給你一段需求,你要提出「房間關係圖」——
也就是這棟房子要哪些房間、各放第幾層、哪些房間彼此相鄰或相通、大門開進哪一間。

只給「拓撲(誰挨著誰)」,絕對不要給任何尺寸、座標、面積、公尺數。

要像真的設計師那樣依生活邏輯配置(不是套固定模板):
- 大門先進到玄關/客廳這類公共空間,不會一進門就是臥室或浴室。
- 廚房要挨著餐廳(煮和吃在一起);客廳餐廳常開放連通。
- 臥室要私密:從走廊/樓梯平台進,不要「穿過一間臥室才到另一間」。
- 每一層都要能被樓梯到達;多層透天樓梯上下同位。
- 浴廁要有人到得了,且盡量靠近臥室或公共動線。
- 客廳、臥室需要對外採光,要排在貼外牆的位置;**不要用天井**(住宅不設天井),
  中段放樓梯/衛浴這類不需要採光的服務空間。
- **不要獨立儲藏室**(住宅不設儲藏室):收納靠櫥櫃解決,那些坪數留給居室。
- 房間數/廳數依需求增減,該有的機能(睡、煮、吃、盥洗、上下樓)要齊。
- 若下方補充有給「每層樓地板面積」,房間數要配到剛好塞滿那個面積,別讓單一
  房間過大(不要出現 50㎡ 的客廳)。面積越大,房間與機能越多(玄關、書房、
  獨立餐廳、更多臥室…);一般單間 3~25㎡。

不同需求給不同格局;同一需求也可以有不只一種合理解法。
"""


# 修正模式(雙向收斂迴圈用):給上一版關係圖 + 落實後發現的問題,請 LLM 改良。
# 跟首次設計同一張 schema → 改完仍是合法關係圖。
REFINE_PROMPT = DESIGNER_PROMPT + """

修正模式:輸入含「上一版關係圖(JSON)」與「落實後發現的問題」兩段。請針對每個
問題修改關係圖,輸出**完整**的改良後關係圖(同格式,所有欄位齊全)。常見對策:
- 某房太大 → 拆成兩間,或該層增加房間/機能,把多的面積吸收掉。
- 內間沒對外採光 → 減少該層房間數,或把它換成不需採光的服務空間(衛浴/走道)。
- 要求的相鄰沒排進去 → 簡化該層、確保關鍵相鄰(廚房挨餐廳、臥室挨走廊)。
- 動線不通 → 該房別擠太多機能。
保留原本合理的部分,只動有問題的地方。
"""


def refine_room_graph(prev_graph: dict, problems: list, *,
                      client: Optional[object] = None, temperature: float = 0.7,
                      floor_area_m2: Optional[float] = None) -> dict:
    """上一版關係圖 + 問題清單 → 改良後的關係圖(雙向收斂迴圈的「重設計」那步)。"""
    if client is None:
        from src.design.api_keys import make_client
        client = make_client()   # 多把金鑰輪替(見 api_keys 模組說明)
    contents = ("上一版關係圖(JSON):\n"
                + json.dumps(prev_graph, ensure_ascii=False)
                + "\n\n落實後發現的問題:\n"
                + "\n".join(f"- {p}" for p in problems))
    if floor_area_m2:
        contents += f"\n\n(每一層樓地板面積約 {floor_area_m2:.0f} ㎡。)"
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config={
            "system_instruction": REFINE_PROMPT,
            "response_mime_type": "application/json",
            "response_schema": ROOM_GRAPH_SCHEMA,
            "temperature": temperature,
        },
    )
    return json.loads(response.text)


def propose_room_graph(brief_text: str, client: Optional[object] = None,
                       temperature: float = 1.0,
                       floor_area_m2: Optional[float] = None) -> dict:
    """需求描述 → 房間關係圖 dict(不畫圖)。

    client 可注入假物件(測試用);None 時建真的 Gemini 客戶端,需 GEMINI_API_KEY。
    temperature 調高一點(預設 1.0)是刻意的——要看「同需求會不會給不同格局」。
    floor_area_m2:每層樓地板面積(㎡)。給了就回饋給設計端,讓它依面積提剛好
      夠的房間數(閉「設計↔落實」的迴圈——沒它 LLM 不知道要塞多滿,易生巨大客廳)。
    """
    if not brief_text or not brief_text.strip():
        raise ValueError("需求描述是空的")
    if client is None:
        from src.design.api_keys import make_client
        client = make_client()   # 多把金鑰輪替(見 api_keys 模組說明)
    contents = brief_text
    if floor_area_m2:
        contents += (f"\n\n(補充:每一層樓地板面積約 {floor_area_m2:.0f} ㎡。"
                     f"請據此提出剛好塞滿這個面積的房間數與機能,別讓單一房間過大。)")
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config={
            "system_instruction": DESIGNER_PROMPT,
            "response_mime_type": "application/json",
            "response_schema": ROOM_GRAPH_SCHEMA,
            "temperature": temperature,
        },
    )
    return json.loads(response.text)


# ---------------------------------------------------------------------------
# 合理性煙霧測試:純資料檢查(不碰網路),回傳「問題清單」,空 = 看起來合理
# ---------------------------------------------------------------------------
def sanity_check(graph: dict) -> list[str]:
    """對一張拓撲圖做基本常識檢查,回傳違規描述(空清單=沒抓到問題)。

    只抓「明顯不合理」——不是要證明它完美,是要濾掉 LLM 亂講的。
    """
    problems: list[str] = []
    rooms = graph.get("rooms", [])
    ids = {r["id"] for r in rooms}
    kinds = {r["id"]: r["kind"] for r in rooms}
    edges = graph.get("adjacencies", [])

    def _neighbors_kinds(rid: str) -> set[str]:
        out = set()
        for e in edges:
            if e["a"] == rid and e["b"] in kinds:
                out.add(kinds[e["b"]])
            if e["b"] == rid and e["a"] in kinds:
                out.add(kinds[e["a"]])
        return out

    if not rooms:
        return ["沒有任何房間"]

    # 1) 邊指涉的房間都要存在
    for e in edges:
        for k in ("a", "b"):
            if e[k] not in ids:
                problems.append(f"相鄰邊指到不存在的房間:{e[k]}")

    # 2) 大門要開進一間存在的房間
    if graph.get("entry") not in ids:
        problems.append(f"大門開進不存在的房間:{graph.get('entry')!r}")

    # 3) 一進門不該直接是臥室/浴廁
    entry_kind = kinds.get(graph.get("entry"))
    if entry_kind in ("bedroom", "master_bedroom", "bathroom", "toilet"):
        problems.append(f"大門直接開進 {entry_kind}(該先進公共空間)")

    # 4) 每間房都要有人到得了(陽台可以是純採光不進人)
    connected = {e["a"] for e in edges} | {e["b"] for e in edges}
    for r in rooms:
        if r["kind"] in ("patio", "balcony"):
            continue
        if r["id"] not in connected and r["id"] != graph.get("entry"):
            problems.append(f"孤立房間(沒有任何相鄰/相通):{r['id']}")

    # 5) 廚房要挨著餐廳或客廳(煮和吃在一起)
    for rid, k in kinds.items():
        if k == "kitchen" and not (_neighbors_kinds(rid)
                                   & {"dining", "living"}):
            problems.append(f"廚房 {rid} 沒挨著餐廳或客廳")

    # 6) 至少要有睡、盥洗的地方
    all_kinds = set(kinds.values())
    if not (all_kinds & {"bedroom", "master_bedroom", "elder_room"}):
        problems.append("沒有任何臥室")
    if not (all_kinds & {"bathroom", "toilet"}):
        problems.append("沒有任何浴廁")

    # 7) 有多層就要有樓梯
    floors = {r["floor"] for r in rooms}
    if len(floors) > 1 and "stair" not in all_kinds:
        problems.append(f"跨 {len(floors)} 層卻沒有樓梯")

    return problems


# ---------------------------------------------------------------------------
# 摘要:把一張拓撲圖印成人看得懂的樣子
# ---------------------------------------------------------------------------
def _fmt_graph(graph: dict) -> str:
    lines = []
    rooms = sorted(graph.get("rooms", []), key=lambda r: (r["floor"], r["id"]))
    by_floor: dict[int, list] = {}
    for r in rooms:
        by_floor.setdefault(r["floor"], []).append(r)
    for fl in sorted(by_floor):
        tag = f"B{-fl}" if fl < 0 else f"{fl}F"
        names = ", ".join(f"{r['id']}({r['kind']})"
                          + ("*" if r["wants_daylight"] else "")
                          for r in by_floor[fl])
        lines.append(f"  {tag}: {names}")
    edges = graph.get("adjacencies", [])
    sym = {"door": "─門─", "open": "═通═", "near": "··鄰··"}
    elines = [f"    {e['a']} {sym.get(e['connection'], '—')} {e['b']}"
              for e in edges]
    return ("  大門→ {}\n".format(graph.get("entry"))
            + "\n".join(lines)
            + "\n  相鄰/相通:\n" + "\n".join(elines)
            + f"\n  設計邏輯:{graph.get('rationale', '')}")


def _topology_signature(graph: dict) -> tuple:
    """把一張圖壓成可比較的指紋:房間種類多重集 + 無向邊集合。

    兩次跑出來指紋不同 = 拓撲真的不一樣(不是換皮的同一張)。
    """
    kind_ms = tuple(sorted(r["kind"] for r in graph.get("rooms", [])))
    edge_set = frozenset(
        frozenset((e["a"], e["b"])) for e in graph.get("adjacencies", []))
    return (kind_ms, edge_set)


def main(argv: Optional[list[str]] = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    from src.design.api_keys import have_key
    if not have_key():
        print("需要設定 GEMINI_API_KEY 環境變數")
        raise SystemExit(1)

    brief = " ".join(args) if args else "三代同堂,要中庭採光,透天三層,四房"
    runs = 1 if args else 4          # 給了需求跑一次;沒給就跑 4 次看多樣性

    print(f"需求:「{brief}」")
    print(f"跑 {runs} 次(temperature=1.0,看同需求會不會給不同格局)\n")

    sigs = set()
    for i in range(1, runs + 1):
        graph = propose_room_graph(brief)
        problems = sanity_check(graph)
        sig = _topology_signature(graph)
        sigs.add(sig)
        verdict = "✅ 合理" if not problems else f"⚠️ {len(problems)} 個問題"
        print(f"── 方案 {i}  [{verdict}]  "
              f"{len(graph['rooms'])} 室 / {len(graph['adjacencies'])} 條相鄰 ──")
        print(_fmt_graph(graph))
        for p in problems:
            print(f"    ⚠️ {p}")
        print()

    if runs > 1:
        print(f"多樣性:{runs} 次跑出 {len(sigs)} 種不同拓撲"
              f"({'有變化,不是固定模板 ✅' if len(sigs) > 1 else '每次都一樣 ❌'})")


if __name__ == "__main__":
    main()
