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

import os
import random
import re
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.design.building_generator import BuildingSpec, generate_building
from src.design.layout_generator import HouseBrief, house_design_note
from src.design.nl_parser import parse_building_brief
from src.web.render import build_sheets, sheet_svg

JOBS_DIR = _PROJECT_ROOT / "output" / "web"          # 每次生成一個子資料夾
STATIC_DIR = Path(__file__).resolve().parent / "static"

_JOB_ID_RE = re.compile(r"[0-9a-f]{12}")
_FILENAME_RE = re.compile(r"[A-Za-z0-9_]+\.(dxf|zip)")   # 白名單:擋路徑跳脫


class GenerateRequest(BaseModel):
    text: str
    code: str = ""
    seed: Optional[int] = None      # 設計變體(E2):None → 隨機抽一個(每次不同)


def _has_api_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY"))


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
    return " · ".join(parts)


def create_app(client_factory: Optional[Callable[[], object]] = None) -> FastAPI:
    """建立應用。client_factory 注入假 Gemini client(測試用);None = 真的。"""
    app = FastAPI(title="自動建築平面圖生成器")

    @app.get("/api/config")
    def config() -> dict:
        return {
            "needs_code": bool(os.environ.get("ACCESS_CODE")),
            "has_api_key": _has_api_key() or client_factory is not None,
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
        seed = req.seed if req.seed is not None else random.randrange(1_000_000)

        # 1) 解析需求(LLM)——語意錯誤 422;網路/額度問題 502
        try:
            brief = parse_building_brief(req.text, client=client, seed=seed)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                502, f"需求解析服務暫時無法使用:{exc}") from exc

        # 2) 生成格局 + 出圖——設計檢核不過(基地太小等)一樣回 422 給使用者看
        try:
            building = generate_building(brief)
            sheets = build_sheets(building)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        # 3) 存檔(DXF + 打包 zip)+ 組回應(SVG 直接內嵌 JSON)
        job_id = uuid.uuid4().hex[:12]
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        out_sheets = []
        for s in sheets:
            s.doc.saveas(job_dir / s.filename)
            out_sheets.append({
                "label": s.label,
                "kind": s.kind,
                "svg": sheet_svg(s),
                "dxf": f"/api/jobs/{job_id}/{s.filename}",
            })
        with zipfile.ZipFile(job_dir / "all_dxf.zip", "w",
                             zipfile.ZIP_DEFLATED) as zf:
            for s in sheets:
                zf.write(job_dir / s.filename, s.filename)

        return {
            "job_id": job_id,
            "seed": seed,
            "summary": _summary(brief, building),
            "design_note": house_design_note(brief.typical),
            "sheets": out_sheets,
            "zip": f"/api/jobs/{job_id}/all_dxf.zip",
        }

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
