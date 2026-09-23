"""Explicit applicability metadata, independent of semantic embedding dimensions."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import os
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml


class Range(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min: float = Field(ge=0)
    max: float = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.min > self.max:
            raise ValueError("範圍下限不可大於上限")
        return self


class CaseMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(default="reference", pattern="^(reference|case|regulation)$")
    review_status: str = Field(default="unreviewed", pattern="^(unreviewed|reviewed)$")
    reviewer: str = Field(default="", max_length=80)
    review_note: str = Field(default="", max_length=600)
    reviewed_at: str = ""
    dimension_basis: str = Field(default="building", pattern="^(building|site)$")
    width_m: Range | None = None
    depth_m: Range | None = None
    floors: Range | None = None
    bedrooms: Range | None = None
    car_spaces: Range | None = None
    party_walls: bool | None = None
    window_sides: list[str] | None = None
    limitations: str = Field(default="", max_length=800)

    @model_validator(mode="after")
    def valid(self):
        if self.review_status == "reviewed" and (not self.reviewer.strip() or not self.review_note.strip()):
            raise ValueError("標示已核對須填核對者及依據")
        if self.window_sides is not None and (len(set(self.window_sides)) != len(self.window_sides)
                                             or any(s not in {"N", "S", "E", "W"} for s in self.window_sides)):
            raise ValueError("開窗側只能用 N/S/E/W，且不可重複")
        return self


class CaseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    dimension_basis: str = Field(default="building", pattern="^(building|site)$")
    width_m: float | None = Field(default=None, gt=0)
    depth_m: float | None = Field(default=None, gt=0)
    floors: int | None = Field(default=None, ge=1)
    bedrooms: int | None = Field(default=None, ge=0)
    car_spaces: int | None = Field(default=None, ge=0)
    party_walls: bool | None = None
    window_sides: list[str] | None = None
    reviewed_only: bool = False


def compatibility(meta: dict, query: dict) -> tuple[bool, str]:
    """Reject known mismatches before vector ranking; unknown never means verified."""
    if query.get("reviewed_only") and meta["review_status"] != "reviewed":
        return False, "尚未人工核對"
    checked, unknown = [], []
    for name in ("width_m", "depth_m", "floors", "bedrooms", "car_spaces", "party_walls", "window_sides"):
        value = query.get(name)
        if value is None:
            continue
        bound = meta.get(name)
        if bound is None or (name in {"width_m", "depth_m"} and meta["dimension_basis"] != query.get("dimension_basis", "building")):
            unknown.append(name)
            continue
        if name == "party_walls":
            good = bound == value
        elif name == "window_sides":
            good = set(bound) <= set(value)  # case may only use available window sides
        else:
            good = bound["min"] <= value <= bound["max"]
        if not good:
            return False, f"{name} 與本案不相容"
        checked.append(name)
    return True, ("已比對：" + ", ".join(checked) if checked else "未提供可比對的條件") + (
        "；條件未記載：" + ", ".join(unknown) if unknown else "")


_QUERY: ContextVar[dict | None] = ContextVar("rag_case_query", default=None)


@contextmanager
def case_context(query: dict):
    token = _QUERY.set(CaseQuery.model_validate(query).model_dump(exclude_none=True))
    try:
        yield
    finally:
        _QUERY.reset(token)


def current_query():
    return dict(_QUERY.get() or {})


def document_files(corpus: Path, data_dir: Path):
    """Enumerate known documents; callers never supply filesystem paths."""
    roots = [corpus]
    imports = data_dir / "imports"
    if imports.is_dir():
        roots.extend(p for p in imports.iterdir() if p.is_dir() and (p / "manifest.json").is_file()
                     and p.resolve().is_relative_to(imports.resolve()))
    for root in roots:
        for path in sorted(root.rglob("*.md")):
            if path.name.lower() == "readme.md" or not path.resolve().is_relative_to(root.resolve()):
                continue
            raw = path.read_text(encoding="utf-8-sig")
            match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", raw, re.S)
            if not match:
                continue
            meta = yaml.safe_load(match[1])
            yield path, meta, raw[match.end():]


def list_documents(corpus: Path, data_dir: Path):
    return [{"id": m["id"], "title": m["title"], "source": m["source"],
             "case": CaseMetadata.model_validate(m.get("case") or {}).model_dump()}
            for _, m, _ in document_files(corpus, data_dir)]


def update_document(corpus: Path, data_dir: Path, document_id: str, case: CaseMetadata):
    from .storage import staging_directory
    matches = [(p, m, body) for p, m, body in document_files(corpus, data_dir) if m.get("id") == document_id]
    if len(matches) != 1:
        raise ValueError("找不到唯一的參考資料")
    path, meta, body = matches[0]
    value = case.model_dump()
    value["reviewed_at"] = datetime.now(timezone.utc).isoformat() if case.review_status == "reviewed" else ""
    meta["case"] = value
    with staging_directory(path.parent, ".metadata-") as stage:
        temp = stage / "document.tmp"
        temp.write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + body,
                        encoding="utf-8")
        os.replace(temp, path)
    return value
