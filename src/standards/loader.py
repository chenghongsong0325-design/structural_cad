"""繪圖標準載入器 —— 讀取 YAML 標準設定檔,套用到一份 ezdxf 文件。

設計目標:
  * 「標準」用資料(YAML)描述,程式只負責「套用」,換客戶不改程式。
  * 支援「同一套標準、套到不同樓層前綴」:圖層代碼固定,完整圖層名由前綴組出來。

典型用法::

    from src.standards.loader import load_standard, new_document, apply_standard

    std = load_standard()                       # 讀 config/standards/default.yaml
    doc = new_document()                        # 建一份 ezdxf 文件(含標準線型)
    names = apply_standard(doc, std, prefix="2F建築底圖")
    # names["COLUMN"] == "2F建築底圖$0$COLUMN"

命名慣例(見 default.yaml 註解):完整圖層名 = 前綴 + 分隔符 + 代碼,
例如 ``2F建築底圖$0$COLUMN``;沒有前綴時就直接用代碼。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ezdxf
import yaml

# 專案根目錄下的預設標準檔位置(本檔:src/standards/loader.py → 往上 2 層是專案根)。
DEFAULT_STANDARD_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "standards" / "default.yaml"
)

# 樓層前綴與代碼之間的預設分隔符(AutoCAD xref 併入時的慣例;可被設定檔覆寫)。
DEFAULT_LAYER_SEPARATOR = "$0$"


# ---------------------------------------------------------------------------
# 資料模型:把 YAML 解析成有型別、可讀的物件(而不是到處傳 dict)
# ---------------------------------------------------------------------------
@dataclass
class LinetypeDef:
    """一種線型。pattern=None 代表「假設文件裡已存在」(例如 CONTINUOUS)。"""

    name: str
    description: str = ""
    pattern: Optional[list[float]] = None


@dataclass
class TextStyleDef:
    """一個文字型(STYLE)。height=0 代表可變高度,建立文字時再指定。"""

    name: str
    font: str
    height: float = 0.0


@dataclass
class DimStyleDef:
    """一個標註型(DIMSTYLE)。欄位對應 AutoCAD 的 DIM 系統變數(見 default.yaml)。"""

    name: str
    text_style: str
    text_height: float   # DIMTXT
    arrow_size: float    # DIMASZ
    ext_beyond: float    # DIMEXE
    ext_offset: float    # DIMEXO
    text_gap: float      # DIMGAP
    scale: float = 1.0   # DIMSCALE
    decimals: int = 0    # DIMDEC


@dataclass
class LayerDef:
    """一個圖層。code 是「去掉樓層前綴」的代碼,完整名稱由 layer_name() 組出。"""

    code: str
    color: int
    linetype: str = "CONTINUOUS"
    description: str = ""
    # 出圖線粗(對應 AutoCAD 的圖層線粗,單位 1/100 mm;例如 0.25mm → 25)。
    # None 代表不設定(用 ezdxf 預設)。
    lineweight: Optional[int] = None


@dataclass
class Standard:
    """一整套繪圖標準。"""

    name: str
    description: str
    units: str
    ltscale: float
    layer_separator: str
    beam_section_format: str
    linetypes: list[LinetypeDef] = field(default_factory=list)
    text_styles: list[TextStyleDef] = field(default_factory=list)
    dim_styles: list[DimStyleDef] = field(default_factory=list)
    layers: list[LayerDef] = field(default_factory=list)
    # 語意代碼 → 實際圖層代碼的別名對應。讓既有模組用的語意代碼(如 COLUMN、A-WALL)
    # 自動對應到規範圖層(如 COL、WALL),模組程式不必改;輸出的 DXF 用規範的圖層名。
    aliases: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 讀取設定檔
# ---------------------------------------------------------------------------
def load_standard(path: Path | str = DEFAULT_STANDARD_PATH) -> Standard:
    """讀取 YAML 標準設定檔,回傳一個 Standard 物件。"""

    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    meta = raw.get("meta", {})

    linetypes = [
        LinetypeDef(
            name=lt["name"],
            description=lt.get("description", ""),
            pattern=lt.get("pattern"),
        )
        for lt in raw.get("linetypes", [])
    ]

    text_styles = [
        TextStyleDef(
            name=ts["name"],
            font=ts["font"],
            height=float(ts.get("height", 0.0)),
        )
        for ts in raw.get("text_styles", [])
    ]

    dim_styles = [
        DimStyleDef(
            name=ds["name"],
            text_style=ds["text_style"],
            text_height=float(ds["text_height"]),
            arrow_size=float(ds["arrow_size"]),
            ext_beyond=float(ds["ext_beyond"]),
            ext_offset=float(ds["ext_offset"]),
            text_gap=float(ds["text_gap"]),
            scale=float(ds.get("scale", 1.0)),
            decimals=int(ds.get("decimals", 0)),
        )
        for ds in raw.get("dim_styles", [])
    ]

    layers = [
        LayerDef(
            code=ly["code"],
            color=int(ly["color"]),
            linetype=ly.get("linetype", "CONTINUOUS"),
            description=ly.get("description", ""),
            lineweight=(int(ly["lineweight"]) if ly.get("lineweight") is not None else None),
        )
        for ly in raw.get("layers", [])
    ]

    aliases = dict(raw.get("layer_aliases", {}) or {})

    return Standard(
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        units=raw.get("units", "MM"),
        ltscale=float(raw.get("ltscale", 1.0)),
        layer_separator=meta.get("layer_separator", DEFAULT_LAYER_SEPARATOR),
        beam_section_format=meta.get("beam_section_format", "{width}×{depth}"),
        linetypes=linetypes,
        text_styles=text_styles,
        dim_styles=dim_styles,
        layers=layers,
        aliases=aliases,
    )


# ---------------------------------------------------------------------------
# 樓層前綴 → 完整圖層名
# ---------------------------------------------------------------------------
def layer_name(
    code: str,
    prefix: str = "",
    separator: str = DEFAULT_LAYER_SEPARATOR,
) -> str:
    """把「代碼」組成完整圖層名。

    >>> layer_name("COLUMN")
    'COLUMN'
    >>> layer_name("COLUMN", "2F建築底圖")
    '2F建築底圖$0$COLUMN'
    """

    if prefix:
        return f"{prefix}{separator}{code}"
    return code


# ---------------------------------------------------------------------------
# 建立一份新文件
# ---------------------------------------------------------------------------
def new_document(dxfversion: str = "R2010") -> ezdxf.document.Drawing:
    """建立一份新的 ezdxf 文件,已載入標準線型/文字型(setup=True)。

    apply_standard() 不強制文件從這裡來,但用這個最省事:CENTER 等標準線型已內建。
    """

    return ezdxf.new(dxfversion, setup=True)


# ---------------------------------------------------------------------------
# 把標準套用到文件
# ---------------------------------------------------------------------------
def apply_standard(
    doc: ezdxf.document.Drawing,
    standard: Standard,
    prefix: str = "",
) -> dict[str, str]:
    """把一套標準套用到 doc:建立線型、文字型、標註型、圖層。

    Args:
        doc: 目標 ezdxf 文件。
        standard: 由 load_standard() 得到的標準。
        prefix: 樓層前綴(例如 "2F建築底圖");空字串代表不加前綴。

    Returns:
        dict:{圖層代碼 -> 實際建立的完整圖層名},方便呼叫端之後掛幾何。
        例如 {"COLUMN": "2F建築底圖$0$COLUMN", ...}
    """

    # (1) 單位與線型全域比例。
    #     units 用字串(如 "MM")對應 ezdxf.units 的常數。
    doc.units = getattr(ezdxf.units, standard.units)
    doc.header["$LTSCALE"] = standard.ltscale

    # (2) 線型:文件裡沒有、且設定檔有給 pattern 的才自行建立。
    #     CONTINUOUS 一定存在;CENTER 若用 new_document(setup=True) 也已存在,會被略過。
    for lt in standard.linetypes:
        if lt.name in doc.linetypes:
            continue
        if lt.pattern is not None:
            doc.linetypes.add(lt.name, pattern=lt.pattern, description=lt.description)
        # else:沒有 pattern 又不存在 → 交給下面圖層建立時的退回機制處理。

    # (3) 文字型。
    for ts in standard.text_styles:
        if ts.name not in doc.styles:
            style = doc.styles.add(ts.name, font=ts.font)
            style.dxf.height = ts.height

    # (4) 標註型(需在文字型之後,因為要引用 text_style)。
    for ds in standard.dim_styles:
        if ds.name in doc.dimstyles:
            continue
        dim = doc.dimstyles.add(ds.name)
        dim.dxf.dimtxt = ds.text_height
        dim.dxf.dimasz = ds.arrow_size
        dim.dxf.dimexe = ds.ext_beyond
        dim.dxf.dimexo = ds.ext_offset
        dim.dxf.dimgap = ds.text_gap
        dim.dxf.dimscale = ds.scale
        dim.dxf.dimdec = ds.decimals
        if ds.text_style in doc.styles:
            dim.dxf.dimtxsty = ds.text_style

    # (5) 圖層:用前綴組出完整名稱後建立。
    created: dict[str, str] = {}
    for ly in standard.layers:
        name = layer_name(ly.code, prefix, standard.layer_separator)

        # 圖層指定的線型若文件裡沒有(例如自訂線型還沒補),退回實線,避免建立失敗。
        linetype = ly.linetype if ly.linetype in doc.linetypes else "CONTINUOUS"

        layer = doc.layers.add(name=name, color=ly.color, linetype=linetype)
        if ly.description:
            layer.description = ly.description
        if ly.lineweight is not None:
            layer.dxf.lineweight = ly.lineweight

        created[ly.code] = name

    # (6) 別名:讓語意代碼對應到實際圖層代碼的完整名稱。
    #     例如 aliases={"COLUMN": "COL"} → created["COLUMN"] = created["COL"],
    #     這樣呼叫端用 layers["COLUMN"] 也能拿到規範圖層 COL 的完整名稱。
    for alias_code, target_code in standard.aliases.items():
        if target_code in created:
            created[alias_code] = created[target_code]

    return created
