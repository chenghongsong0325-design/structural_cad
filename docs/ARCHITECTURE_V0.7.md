# Architecture Snapshot — v0.7

延續 [ARCHITECTURE_V0.5.md](ARCHITECTURE_V0.5.md)。v0.5 之後新增兩大塊:
**v0.6 Collision Engine**(會改家具)與 **v0.7 Layout Analysis Stack**(預設唯讀)。

---

## 1. 分層總圖

```
                 自然語言(Gemini)/ HouseBrief / BuildingBrief
                                    │
  ┌─────────────────────────────────┴─────────────────────────────────┐
  │ 生成層  design/                                                    │
  │   room_program        面積程式(min/preferred/max + 形狀約束)      │
  │   layout_generator    ★ 唯一「產生」FloorPlanSpec 的地方           │
  │   building_generator  多樓層堆疊、柱位對齊                          │
  └─────────────────────────────────┬─────────────────────────────────┘
                                    │ FloorPlanSpec
  ┌─────────────────────────────────┴─────────────────────────────────┐
  │ 修復層  design/collision/   (v0.6,會改 spec.fixtures)             │
  │   obstacle → geometry(provider)→ detector → engine → resolver     │
  │   接線點:_validate_or_raise / generate_floor_plan 各一次           │
  └─────────────────────────────────┬─────────────────────────────────┘
                                    │
  ┌─────────────────────────────────┴─────────────────────────────────┐
  │ 守門層  layout_generator.validate_spec()   不過就 raise            │
  └─────────────────────────────────┬─────────────────────────────────┘
                                    │ 合格的 FloorPlanSpec
  ┌─────────────────────────────────┴─────────────────────────────────┐
  │ 分析層  design/   (v0.7,★ 不接進生成流程)                          │
  │   connectivity ─┬─► layout_validation                             │
  │                 ├─► corridor ──┐                                   │
  │                 │              ├─► scoring ─┐                      │
  │                 └─► constraints ────────────┴─► optimizer          │
  │                                                    └─► optimization_benchmark │
  │   report.JsonReport  所有 Report 的序列化基底                        │
  └─────────────────────────────────┬─────────────────────────────────┘
                                    │
  ┌─────────────────────────────────┴─────────────────────────────────┐
  │ 製圖層  drafting/     apartment_plan.draw_floor_plan → DXF         │
  │ 呈現層  web/          FastAPI + 前端;scripts/ 預覽                 │
  │ 巡檢台  design/benchmark.py(34 案)/ optimization_benchmark.py     │
  └───────────────────────────────────────────────────────────────────┘
```

---

## 2. 誰可以寫 spec

這是本專案最重要的一條界線:

| 模組 | 對 `FloorPlanSpec` 的權限 |
|---|---|
| `layout_generator` / `building_generator` | **產生**(唯一結構作者) |
| `collision.resolver` | **只寫 `spec.fixtures`**(位置/成員),不碰 rooms/walls |
| `optimizer` | **只寫 rooms / walls / doors**,且**不碰 fixtures** |
| `validate_spec`、① ~ ⑤ 分析層、`drafting`、`web` | **只讀** |

`collision` 與 `optimizer` 的可寫範圍**互斥**——前者只動家具、後者只動格局,
所以兩者不會互相踩踏。Optimizer 的安全閘門另外要求「碰撞數不得增加」,
形成單向的保護關係。

---

## 3. v0.6 Collision Engine

抽象核心是 **Obstacle**,不是 Furniture vs Furniture:

```
geometry.py (provider)          detector.py                engine/resolver
  fixture   → movable  ┐        movable × movable  area>tol      try_move
  door_swing→ static   ├──────► movable × 硬障礙   area>tol      try_drop
  wall      → static   │        movable × 牆      突出面積>容差
  void/stair→ static   │        movable × 柱      穿入深度>容差   (柱只報不修*)
  column    → static   ┘
```

\* 柱在 v0.6 Phase 4 已有 resolver(`try_move` 的避柱守衛),但因本 repo 的柱
100% 藏在牆內、伸進室內最多 250mm < 容差 300mm,**現實案例不會觸發**;它是為
未來獨立柱(開放空間落柱)預留。

判準為何各不相同,是量測出來的:

| 障礙 | 判準 | 依據 |
|---|---|---|
| 家具 / 門迴轉 / 天井 / 樓梯 | 交集面積 > 100mm² | 與 `validate_spec` 一致 |
| 牆 | 突出所屬房間 > 5000mm² | 貼牆 ≤1mm² vs 真穿牆 ≥170000mm²,差五個數量級 |
| 柱 | 穿入深度 > 300mm | 283/941 件家具合法貼柱,最深 175mm(理論上限 190) |

---

## 4. v0.7 Layout Analysis Stack

見 [LAYOUT_ENGINE.md](LAYOUT_ENGINE.md)。三個架構要點:

1. **`connectivity` 是連通判定的單一來源** —— `layout_validation` 的孤立房檢查、
   `corridor` 的步行距離、`scoring` 的 connectivity/privacy、`constraints` 的
   相鄰判定,全部重用同一份圖,不各寫一份(避免「重複 detector」)。
2. **依賴是單向 DAG,無循環** —— `report` 與 `connectivity` 在最底層,
   `optimization_benchmark` 在最上層。
3. **唯讀 vs 可寫的界線寫進測試** —— 每個唯讀層都有
   `test_*_does_not_mutate_spec`;optimizer 有「不碰 fixtures」的測試。

---

## 5. 品質基準(v0.7 定版)

| 指標 | 數值 |
|---|---|
| 測試 | **617 passed** |
| Benchmark | 34/34 生成成功 · **20 pass / 14 warn / 0 fail** |
| DXF / PNG | 100 / 100 |
| 殘留碰撞(牆·天井·樓梯·柱) | 0 · 0 · 0 · 0 |
| 生成後二次 `resolve()` | **100/100 層 no-op** |
| LayoutReport 乾淨樓層 | 100/100 |
| ConnectivityReport 通過 | 100/100 |
| Optimization Benchmark | 8 層:改善 5 · **退步 0** |

「二次 resolve 全 no-op」是碰撞引擎零 regression 的硬證據:四類障礙全開之後,
對每張生成完的圖再跑一次修復,100 層完全不動。

---

## 6. 已知限制

- **Column Resolver 現實案例不會觸發**(柱全藏牆內),為未來獨立柱預留。
- **Optimizer 增益極小**(+0.01 分):三種微調修不掉 constraint error,
  生成器輸出已接近局部最佳。
- **L 形走道寬度被高估**(最小外接矩形近似),只會漏報不會誤報。
- **Elevator / Shaft 未納入碰撞**:Shaft 在本 repo 是「管道牆」= 牆,已被涵蓋;
  Elevator 有 `spec.elevators` 但無 provider,且不在 benchmark 範圍。
- **`validate_spec` 與 `collision_problems` 的家具檢核仍重複**(刻意的過渡設計,
  待收斂為單一來源)。
- **`engine` 的「換對方讓開」後備路徑** 因 `blockers2` 未排除 WALL,自 v0.6
  Phase 2 起實質為死碼(不影響現行行為)。
- **天井版 Living Overflow 尚未套用**(P01~P04)。
