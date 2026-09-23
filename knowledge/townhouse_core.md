---
id: townhouse-core
title: 連棟透天的中段核與方案選項
source: src/design/layout/townhouse_options.py; src/design/layout/narrow_house.py
updated: "2026-09-17"
scopes: [townhouse]
---
## 前後串聯的透天骨架
現有窄透天骨架是前段居室、中段樓梯浴廁與側走道、後段居室或餐廚。多層沿用同一垂直核。LLM 可選 core_style、mirror、open_kitchen、entry_frac、patio、garage、floors、bedrooms，尺寸座標仍由引擎計算。樓梯目前採折返梯。

## ref 參考圖方案：天井與橫置樓梯
core_style=ref 把樓梯橫置，中段保留天井與浴廁的配置可能，浴廁門可通向側走道。這是現有選配提示詞的參考方案。走道口的寬度會限制前後段分房；多房需求與天井需求可能需要取捨，實際是否放得下由生成器確認。

## mid 方案：廁所直接連接走道
core_style=mid 的中段由樓梯、浴廁、側走道並排。浴廁貼走道，門能直接開向走道。此選配方案不提供天井；想改善廁所進出、不要求天井時是可用選項。鏡射可改變樓梯核左右位置，不能解決所有尺寸限制。

## default 方案與房數
core_style=default 採浴廁、樓梯、側走道並排；較寬面寬的前後段有分房機會。浴廁與走道之間隔著樓梯，其開門關係與 mid 不同。房間過大或要求更多房間時，可以考慮其他核款式，仍須通過門與動線檢查。
