"""engine —— CollisionEngine:collect → check → resolve 的編排。

批次修復(Batch Resolver):蒐集 Obstacle → 偵測碰撞 → 依優先序讓低優先的
家具退讓(先 try_move,移不動且裝飾類才 try_drop)→ 寫回 spec.fixtures。

⚠️ 關鍵不變量:**沒有碰撞時完全不動 spec**(collect→find→空→直接返回)。
現有合格案例(benchmark)本來就零碰撞,故接進流程後輸出逐字不變 → 零 regression。
修復器只在「本來會被 validate 判失敗」的碰撞案例上作用。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.design.collision.detector import find_collisions
from src.design.collision.geometry import collect_active
from src.design.collision.obstacle import COLUMN, WALL
from src.design.collision.priority import priority_of
from src.design.collision.resolver import try_drop, try_move
from src.design.report import JsonReport


@dataclass
class ResolveReport(JsonReport):
    """一次修復的結果(給測試/日後 benchmark 統計;生成流程本身不看)。"""

    moved: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    # 家具壓柱(v0.6 Phase 3-3):偵測到的柱碰撞,供報表檢視。
    column_hits: list = field(default_factory=list)
    # 壓柱但找不到合法落點(v0.6 Phase 4):家具**保留原位**,標記交人判斷。
    unresolved_column: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.dropped)

    def to_dict(self) -> dict:
        return {
            "changed": self.changed,
            "moved": list(self.moved),
            "dropped": list(self.dropped),
            "unresolved": list(self.unresolved),
            "column_hits": list(self.column_hits),
            "unresolved_column": list(self.unresolved_column),
        }


class CollisionEngine:
    """對一份 FloorPlanSpec 做家具碰撞的偵測與批次修復。"""

    def __init__(self, spec):
        self.spec = spec

    def collect(self):
        """作用中 Obstacle(Phase 1:家具 + 門迴轉)。每次重建,反映最新 fixtures。"""
        return collect_active(self.spec)

    def check(self):
        """只偵測、不改——回 Collision 清單。"""
        return find_collisions(self.collect())

    def resolve(self, max_passes: int = 4) -> ResolveReport:
        """偵測 + 批次修復,就地改 spec.fixtures。回 ResolveReport。

        每一輪處理一組碰撞(讓低優先者退讓),再重新偵測(移動可能連鎖);
        必要家具修不動就留給 validate 安全網。max_passes 防無限迴圈。"""
        report = ResolveReport()
        gave_up: set = set()                           # 修不動的柱碰撞,不再重試
        for _pass in range(max_passes):
            obs = self.collect()
            found = find_collisions(obs)
            # 柱:只有穿入深度 > COLUMN_TOLERANCE_MM 的才會被 detector 回報
            # (柱藏牆內、家具貼牆壓到柱的室內半邊 ≤190mm 屬合法,不會進來)。
            if not _pass:
                report.column_hits = [f"{c.a.tag}×柱" for c in found
                                      if c.b.kind == COLUMN]
            # 放棄過的柱碰撞要濾掉,否則它會一直佔住 cols[0],擋住其他碰撞。
            cols = [c for c in found
                    if c.b.kind != COLUMN or id(c.a.ref) not in gave_up]
            if not cols:
                break                                  # 無可修碰撞 → 完全不動(常態)
            c = cols[0]
            a, b = c.a, c.b
            col_polys = [o.poly for o in obs if o.kind == COLUMN]
            if b.kind == COLUMN:
                # Phase 4:柱碰撞只 try_move,**不 try_drop**——修不動就保留家具
                # 並標記 unresolved_column(結構柱不該讓家具憑空消失,交人判斷)。
                if try_move(a, [o.poly for o in obs
                                if o is not a and o.kind not in (WALL, COLUMN)],
                            columns=col_polys):
                    report.moved.append(a.tag)
                else:
                    gave_up.add(id(a.ref))
                    report.unresolved_column.append(a.tag)
                continue
            # 決定誰退讓:對方是靜態障礙(門迴轉 / 牆)→ 只能動家具 a;
            # 兩件都是家具 → 低優先(或同優先時後者)退讓。
            if not b.movable:                          # 門迴轉 / 牆(穿牆)
                yielder, other = a, b
            elif priority_of(a.tag) <= priority_of(b.tag):
                yielder, other = a, b
            else:
                yielder, other = b, a
            # blockers 排除牆(kind=WALL 的 poly 是房間多邊形,家具本來就在裡面,
            # 拿來當重疊障礙會永遠「撞」)——牆的約束由 try_move 的穿牆判定處理。
            # 也排除柱:柱藏牆內,30% 的合法貼牆家具都壓到柱,拿來當 blocker 會
            # 讓 try_move 幾乎找不到落點、改變既有修復結果 → 柱完全不影響移動。
            blockers = [o.poly for o in obs
                        if o is not yielder and o.kind not in (WALL, COLUMN)]
            if try_move(yielder, blockers, columns=col_polys):
                report.moved.append(yielder.tag)
                continue
            if try_drop(yielder):
                self.spec.fixtures.remove(yielder.ref)
                report.dropped.append(yielder.tag)
                continue
            # 退讓者動不了 → 試著讓對方(若也是家具)讓開。
            if other.movable and other is not yielder:
                blockers2 = [o.poly for o in obs if o is not other]
                if try_move(other, blockers2, columns=col_polys):
                    report.moved.append(other.tag)
                    continue
            report.unresolved.append(f"{yielder.tag}×{other.tag}")
            break                                      # 修不動 → 交給 validate 報錯
        return report


def resolve_collisions(spec) -> ResolveReport:
    """對外唯一入口:接進生成流程的那一個呼叫(validate 之前)。"""
    return CollisionEngine(spec).resolve()
