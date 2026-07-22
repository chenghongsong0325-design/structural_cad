"""Obstacle —— 碰撞引擎的抽象核心。

不是「家具」專用:牆/柱/門迴轉/窗/樓梯/天井/停車/保留區/家具都包成 Obstacle。
detector 只認「movable(可移動,目前只有家具)vs 其他」,加新障礙種類 =
在 geometry.py 加一個 provider,detector/resolver 一行不改。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from shapely.geometry import Polygon

# ── ObstacleKind(障礙種類;字串常數,避免 import Enum 的樣板)──────────────
FURNITURE = "furniture"      # 家具/設備(唯一 movable)
DOOR_SWING = "door_swing"    # 門扇迴轉範圍(家具不可壓)
COLUMN = "column"            # 柱斷面
WALL = "wall"                # 牆體(Phase 1 不納入偵測:家具本來就貼牆)
WINDOW = "window"            # 窗前淨空(軟約束,備用)
STAIR = "stair"              # 樓梯佔地
VOID = "void"                # 天井/挑空(patio)
PARKING = "parking"          # 車位/車道
RESERVED = "reserved"        # 保留區(未來新增,spec 尚無此欄位)


@dataclass
class Obstacle:
    """一個碰撞對象。

    poly      世界座標多邊形(shapely;mm)。
    kind      ObstacleKind 之一。
    movable   可否被修復器移動/丟棄——目前只有家具 True。
    priority  重要度(高=先佔位、不可丟);由 priority.py 給,detector 不用。
    ref       來源物件(FixturePlacement / Counter / DoorPlacement …),
              resolver 靠它把修好的位置寫回。
    room      所屬房間多邊形(移動時的邊界;非家具可為 None)。
    tag       人看的標籤(家具名 / "牆 N 的門" …),用於問題訊息。
    meta      額外欄位(如門迴轉的 wall_index),不同 kind 各取所需。
    """

    poly: Polygon
    kind: str
    movable: bool = False
    priority: int = 0
    ref: object = None
    room: Optional[Polygon] = None
    tag: str = ""
    meta: dict = field(default_factory=dict)
    # 碰撞判定用多邊形(牆/穿牆判定用):多數 = poly;桌椅組(table4)用收緊的
    # 碰撞 footprint(椅子區不算穿牆依據)。None → 退回 poly。家具×家具與門迴轉
    # 仍用 poly(完整 footprint),不受此影響。
    collision_poly: Optional[Polygon] = None
