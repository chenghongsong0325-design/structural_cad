"""軸網系統產生器 —— 支援多條軸線、雙向(X/Y)、自動編號。

設計目標(P1b):
  * 輸入用「間距列表」(如 [6000, 6000, 6000]),模組自動算出座標與編號,
    呼叫端不必自己維護座標。
  * X 方向(垂直線,沿 Y 延伸)用數字編號 1, 2, 3…
    Y 方向(水平線,沿 X 延伸)用英文編號 A, B, C…(超過 Z 接 AA, AB…)
  * 純資料計算(build_grid_system)與畫圖(draw_grid / draw_grid_dimensions)分開,
    方便單元測試。
  * 軸間尺寸標註(draw_grid_dimensions)標的是「相鄰軸線的間距」,放在跟編號圈相反的
    那一側(X 方向在上方、Y 方向在右側),避免跟編號圈重疊。

典型用法::

    from src.drafting.gridlines import build_grid_system, draw_grid, draw_grid_dimensions

    grid = build_grid_system(x_spacings=[6000, 6000], y_spacings=[5000, 5000])
    draw_grid(msp, grid, layers)              # 軸線 + 編號圈
    draw_grid_dimensions(msp, grid, layers)   # 軸間尺寸標註
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from ezdxf.enums import TextEntityAlignment


# ---------------------------------------------------------------------------
# 編號規則
# ---------------------------------------------------------------------------
def numeric_labels(count: int) -> list[str]:
    """數字編號:1, 2, 3, …"""
    return [str(i) for i in range(1, count + 1)]


def alpha_labels(count: int) -> list[str]:
    """英文編號:A, B, …, Z, AA, AB, …(Excel 欄名式進位,不含 0)。"""
    labels = []
    for i in range(count):
        n = i
        label = ""
        while True:
            label = chr(65 + n % 26) + label
            n = n // 26 - 1
            if n < 0:
                break
        labels.append(label)
    return labels


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
@dataclass
class GridLine:
    """一條軸線:編號(label) + 座標位置(position,沿垂直於該軸線的方向量測)。"""

    label: str
    position: float


@dataclass
class GridSystem:
    """一整套軸網:X 方向(數字編號)與 Y 方向(英文編號)的軸線各自一組。"""

    x_axes: list[GridLine]
    y_axes: list[GridLine]


def _positions_from_spacings(spacings: Sequence[float]) -> list[float]:
    """把間距列表轉成累加座標,第一條軸線固定在 0。

    >>> _positions_from_spacings([6000, 6000])
    [0, 6000.0, 12000.0]
    """
    positions: list[float] = [0]
    for gap in spacings:
        positions.append(positions[-1] + gap)
    return positions


def build_grid_system(
    x_spacings: Optional[Sequence[float]] = None,
    y_spacings: Optional[Sequence[float]] = None,
    x_labels: Optional[Sequence[str]] = None,
    y_labels: Optional[Sequence[str]] = None,
) -> GridSystem:
    """從間距列表建立一套 GridSystem。

    Args:
        x_spacings: X 方向(垂直軸線)相鄰間距,例如 [6000, 6000] → 3 條軸線(1,2,3)。
                    None 代表這個方向完全沒有軸線;空列表 [] 代表只有一條軸線(在 0)。
        y_spacings: Y 方向(水平軸線)相鄰間距,語意同上,例如 [5000, 5000] → 3 條軸線(A,B,C)。
        x_labels / y_labels: 自訂編號;不給就用預設規則(數字 / 英文)。
    """

    x_positions = _positions_from_spacings(x_spacings) if x_spacings is not None else []
    y_positions = _positions_from_spacings(y_spacings) if y_spacings is not None else []

    x_labels = list(x_labels) if x_labels is not None else numeric_labels(len(x_positions))
    y_labels = list(y_labels) if y_labels is not None else alpha_labels(len(y_positions))

    if len(x_labels) != len(x_positions):
        raise ValueError(
            f"x_labels 數量({len(x_labels)})與軸線數量({len(x_positions)})不符"
        )
    if len(y_labels) != len(y_positions):
        raise ValueError(
            f"y_labels 數量({len(y_labels)})與軸線數量({len(y_positions)})不符"
        )

    return GridSystem(
        x_axes=[GridLine(label, pos) for label, pos in zip(x_labels, x_positions)],
        y_axes=[GridLine(label, pos) for label, pos in zip(y_labels, y_positions)],
    )


# ---------------------------------------------------------------------------
# 畫圖
# ---------------------------------------------------------------------------
def draw_grid(
    msp,
    grid: GridSystem,
    layers: dict[str, str],
    extension: float = 1200,
    bubble_offset: float = 2000,
    bubble_radius: float = 350,
    text_height: float = 250,
) -> None:
    """把 GridSystem 畫到 modelspace。

    Args:
        msp: ezdxf modelspace。
        grid: build_grid_system() 產生的軸網資料。
        layers: apply_standard() 回傳的{代碼 -> 完整圖層名}對照表,
                需要有 "AXIS" 與 "S-TEXTB" 兩個代碼。
        extension: 軸線兩端超出建築範圍的外伸長度。
        bubble_offset: 編號圈相對於軸線末端再外推的距離。
        bubble_radius: 編號圈半徑。
        text_height: 編號文字高度。

    X 軸線(數字編號)畫成垂直線,沿 Y 方向延伸,編號圈畫在下方(Y 變小的一端)。
    Y 軸線(英文編號)畫成水平線,沿 X 方向延伸,編號圈畫在左側(X 變小的一端)。
    只有一個方向有軸線時(另一方向為空),仍能正確畫出該方向,只是沒有垂直方向的邊界可外伸,
    退回只用該方向本身的範圍。
    """

    axis_layer = layers["AXIS"]
    text_layer = layers["S-TEXTB"]

    x_positions = [a.position for a in grid.x_axes]
    y_positions = [a.position for a in grid.y_axes]

    # 軸線需要跨越「另一個方向」的範圍;沒有另一方向資料時,退回用 0 當唯一範圍。
    y_span = (min(y_positions), max(y_positions)) if y_positions else (0, 0)
    x_span = (min(x_positions), max(x_positions)) if x_positions else (0, 0)

    y_start, y_end = y_span[0] - extension, y_span[1] + extension
    x_start, x_end = x_span[0] - extension, x_span[1] + extension

    # ── X 方向軸線(垂直線,數字編號,圈在下方)───────────────────────────
    for axis in grid.x_axes:
        x = axis.position
        msp.add_line((x, y_start), (x, y_end), dxfattribs={"layer": axis_layer})

        bubble_y = y_start - bubble_offset
        _add_bubble(
            msp, (x, bubble_y), axis.label,
            axis_layer, text_layer, bubble_radius, text_height,
        )

    # ── Y 方向軸線(水平線,英文編號,圈在左側)───────────────────────────
    for axis in grid.y_axes:
        y = axis.position
        msp.add_line((x_start, y), (x_end, y), dxfattribs={"layer": axis_layer})

        bubble_x = x_start - bubble_offset
        _add_bubble(
            msp, (bubble_x, y), axis.label,
            axis_layer, text_layer, bubble_radius, text_height,
        )


def draw_grid_dimensions(
    msp,
    grid: GridSystem,
    layers: dict[str, str],
    extension: float = 1200,
    dim_offset: float = 800,
    dimstyle: str = "STRUCT",
) -> None:
    """標註軸網中「相鄰軸線的間距」(不是總長度)。

    Args:
        msp: ezdxf modelspace。
        grid: build_grid_system() 產生的軸網資料。
        layers: apply_standard() 回傳的{代碼 -> 完整圖層名}對照表,需要有 "DIM" 代碼。
        extension: 需跟 draw_grid() 用同一個值,才能量到軸線實際外伸後的端點。
        dim_offset: 尺寸線相對於軸線外伸端點,再外推的距離。
        dimstyle: 使用哪個標註型(需已由 apply_standard 建立,見 default.yaml 的 dim_styles)。

    X 方向(垂直軸線)的間距標註畫成水平尺寸鏈,放在網格上方(y 變大的一側)——
    跟 draw_grid() 的編號圈(在下方)相反,避免重疊。
    Y 方向(水平軸線)的間距標註畫成垂直尺寸鏈,放在網格右側(x 變大的一側)——
    跟編號圈(在左側)相反。
    少於 2 條軸線的方向沒有「間距」可標,直接略過。
    """

    dim_layer = layers["DIM"]

    x_positions = [a.position for a in grid.x_axes]
    y_positions = [a.position for a in grid.y_axes]

    y_span = (min(y_positions), max(y_positions)) if y_positions else (0, 0)
    x_span = (min(x_positions), max(x_positions)) if x_positions else (0, 0)

    # ── X 方向間距(水平尺寸鏈,放在網格上方)───────────────────────────
    if len(x_positions) >= 2:
        line_y = y_span[1] + extension
        dim_y = line_y + dim_offset
        for x1, x2 in zip(x_positions, x_positions[1:]):
            dim = msp.add_linear_dim(
                base=(x1, dim_y),
                p1=(x1, line_y),
                p2=(x2, line_y),
                angle=0,
                dimstyle=dimstyle,
                dxfattribs={"layer": dim_layer},
            )
            dim.render()

    # ── Y 方向間距(垂直尺寸鏈,放在網格右側)───────────────────────────
    if len(y_positions) >= 2:
        line_x = x_span[1] + extension
        dim_x = line_x + dim_offset
        for y1, y2 in zip(y_positions, y_positions[1:]):
            dim = msp.add_linear_dim(
                base=(dim_x, y1),
                p1=(line_x, y1),
                p2=(line_x, y2),
                angle=90,
                dimstyle=dimstyle,
                dxfattribs={"layer": dim_layer},
            )
            dim.render()


def _add_bubble(
    msp,
    center: tuple[float, float],
    label: str,
    axis_layer: str,
    text_layer: str,
    radius: float,
    text_height: float,
) -> None:
    """畫一個軸線編號圈:圓(掛 AXIS 圖層,強制實線)+ 置中編號文字(掛 S-TEXTB)。"""

    msp.add_circle(center, radius=radius, dxfattribs={"layer": axis_layer, "linetype": "CONTINUOUS"})
    msp.add_text(
        label,
        height=text_height,
        dxfattribs={"layer": text_layer, "style": "STRUCT"},
    ).set_placement(center, align=TextEntityAlignment.MIDDLE_CENTER)
