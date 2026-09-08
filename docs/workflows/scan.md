# 隨機掃描:產線的驗收關卡

> ⚠️ 這是**工作流程**,不是 API 文件。數字(尺寸區間、常數)的唯一出處一律是
> 程式碼本身;這裡寫的是「怎麼做、會踩到什麼」。

單元測試守的是**已知案例**;這支守的是**沒人想到的尺寸組合**。目標永遠是「硬錯誤 0」。

## 怎麼跑

```bash
# 窄透天 narrow_house(4~8m 寬,進深 ≥9.5m;>6m 寬要 ≥10.5m)
python scripts/scan_plans.py --n 60 --width 4,8 --depth 10.5,18
# 淺基地 shallow_house(5~9m 寬,進深 5m ~ 放不下折返梯為止)
python scripts/scan_plans.py --n 60 --width 5,9 --depth 5,9.4
# 兩帶式 layout_generator(多樓層需 ≥10×7m)
python scripts/scan_plans.py --n 40 --width 10,30 --depth 8,20
python scripts/scan_plans.py --n 200 --json out.json      # 存明細供前後比對
python scripts/scan_plans.py --n 80 --seed 0              # 全區間(含缺口,E 會很多)
```

⚠️ **區間一定要對著產線的 MIN/MAX 抓**(數字的唯一出處是
[narrow_house.py 檔頭常數](../../src/design/layout/narrow_house.py) 與
[shallow_house.py:49-52](../../src/design/layout/shallow_house.py#L49-L52),別背)。
區間抓錯 → 案子掉進別條產線或直接 raise,滿螢幕 `E`,看起來像大回歸其實只是掃錯地方。
已知缺口:**8~10m 寬 × 深基地沒有任何產線收**(窄透天上限 8m、兩帶式下限 10m),
掃到必 raise,不是 bug。

退出碼:全合格 = 0,有任何不合格/生不出來 = 1(可掛 CI)。
80 案約 1~3 分鐘;200 案跑背景。

## 流程

1. **先確定改了哪條產線**,只掃那段寬度區間(快、訊號乾淨):
   - 寬 4~8m、深 ≥9.5/10.5m → `narrow_house`(前後串聯 + 單樓梯)
   - 寬 5~9m 且進深放不下折返梯 → `shallow_house`(樓梯轉 90 度)
   - 寬 ≥10m → 兩帶式 `layout_generator`
   - AI 版(`graph_layout`,寬 5~30m)不走這支腳本,它有自己的 `design_loop`
2. **跑掃描,看「硬錯誤排行」那張表** —— 這是整支腳本的重點。
   一條規則出現 10 次以上 = 系統性錯誤(不是個案),值得回去改骨架或修復器;
   出現 1~2 次 = 個案,先記下來別急著補丁。
3. **修完再用同一個 `--seed` 重跑**,確認該規則歸零、其他規則沒變多。
4. **收尾前跑 `pytest -q`** 確認沒回歸,再跑一次 `--n 80 --seed 0` 當總驗收。

## 判讀

- `.` 合格 / `X` 出得了圖但不合格 / `E` 直接生不出來(raise)
- **硬錯誤(擋圖)**:`room_no_door` `floor_split` `no_entry` `entry_upstairs`
  `furniture_in_wall` `door_in_corner` `stair_blocks_door` `stair_side_open`
  `circulation_blocked` —— 定義見 [plan_check.py](../../src/design/layout/plan_check.py) 檔頭
- **設計警告(不擋圖)**:`room_no_daylight` `room_oversize` `room_skinny`
  —— 這些是「房間怎麼配」的問題,靠換切法救不動,別拿它當失敗
- `E` 生不出來未必是 bug:超出該骨架物理下限(<5m 寬 / <8m 深)本來就該 raise。
  看訊息裡的下限是否合理,不合理才追。

## 掃描設定的兩個坑

- 尺寸一律當**建築物**尺寸掃(`setback=0`),掃描區間才直接對應各骨架的
  `MIN/MAX_WIDTH`,不會被退縮換算糊掉。
- 多樓層透天一定要 `differentiated=True`(腳本已依 `floors>1` 自動開),跟
  `nl_parser` 一致。忘了開,2F 會整層複製 1F 連大門一起 → 掃出滿滿假的
  `entry_upstairs`。

## 加新產線時

到 [scripts/scan_plans.py](../../scripts/scan_plans.py) 的 `run_case` 確認新骨架走得到
`generate_building_auto`;新產線的專屬尺寸區間補進上面「流程」第 1 點的對照清單。
