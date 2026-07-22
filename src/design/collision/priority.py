"""priority —— 家具的重要度:誰先佔位、誰不可丟、誰可丟。

修復器的決策依據:兩件家具相撞時,讓「低優先」的那件退讓(先移,移不動且
可丟就丟);高優先的(床/馬桶/沙發/流理台…)永遠保留。這樣修復不會把必要
家具弄丟,只犧牲裝飾。
"""
from __future__ import annotations

# 優先度分三級(數字大=重要)。未列的家具給預設中級 2。
#   3 必要:房間的機能主角,絕不丟(丟了房間就不成立)。
#   2 主要:重要但非唯一,盡量保留。
#   1 裝飾:錦上添花,修不好可丟(房間仍成立)。
PRIORITY = {
    # 3 必要
    "bed_single": 3, "bed_double": 3,
    "toilet": 3, "basin": 3,
    "sofa3": 3, "counter": 3, "fridge": 3,
    # 2 主要
    "table4": 2, "desk": 2, "wardrobe": 2,
    # 1 裝飾(可丟)
    "coffee_table": 1, "armchair": 1, "tv_cabinet": 1, "nightstand": 1,
    "bar_stool": 1, "bookshelf": 1, "shoe_cabinet": 1, "bathtub": 1,
}
_DEFAULT = 2
DROP_MAX = 1        # 優先度 ≤ 此值才可丟(只有裝飾)


def priority_of(tag: str) -> int:
    """家具標籤(fixture name / "counter")→ 優先度。"""
    return PRIORITY.get(tag, _DEFAULT)


def is_droppable(tag: str) -> bool:
    """這件家具修不好時可否直接丟掉(只有裝飾類可以)。"""
    return priority_of(tag) <= DROP_MAX


def yielder(a_tag: str, b_tag: str) -> int:
    """兩件相撞的家具,誰該退讓——回 0(a 讓)或 1(b 讓)。低優先的退讓;
    同優先時後放的(b)退讓(後放的通常是裝飾/次要,且順序穩定可測)。"""
    return 0 if priority_of(a_tag) < priority_of(b_tag) else 1
