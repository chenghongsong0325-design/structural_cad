"""Final validation fails closed; a missing/malformed check is not a pass."""
from __future__ import annotations


def validate_building(building, *, env=None) -> dict:
    from src.design.layout.plan_check import check_building
    from src.design.layout.code_check import check_code_building
    floors = [(f.label, f.spec) for f in building.floors]
    if not floors:
        return {"validation": {"status": "unverified", "failures": [{"check": "building", "error_type": "EmptyBuilding"}]}}
    result = {}
    failures = []
    for name, check in (("plan_check", lambda: check_building(floors, env)),
                        ("code_check", lambda: check_code_building(floors, env, building.floor_height))):
        try:
            result[name] = check().to_dict()
        except Exception as exc:
            # Exception text may contain paths/secrets. Only expose a stable type.
            failures.append({"check": name, "error_type": type(exc).__name__})
    result["validation"] = validation_status(result)
    if failures:
        result["validation"]["failures"] = failures
    return result


def validation_status(result: dict) -> dict:
    missing, invalid, failed = [], [], []
    for name, severity, count_key in (("plan_check", "error", "n_errors"),
                                      ("code_check", "violation", "n_violations")):
        report = result.get(name)
        if not isinstance(report, dict):
            missing.append(name)
            continue
        issues = report.get("issues")
        if (type(report.get("ok")) is not bool or not isinstance(issues, list)
                or any(not isinstance(i, dict) or i.get("severity") not in {severity, "warning"}
                       for i in issues)):
            invalid.append(name)
            continue
        errors = sum(i["severity"] == severity for i in issues)
        if report.get(count_key) != errors or report["ok"] != (errors == 0):
            invalid.append(name)
        elif errors:
            failed.append(name)
    return {"status": "unverified" if missing or invalid else "failed" if failed else "passed",
            "missing": missing, "invalid": invalid, "failed": failed,
            "scope": "僅限已實作的圖面與尺寸規則，不等同建照或施工審查"}


def assess_candidate(floors, requirements: list, *, env=None, floor_height=None):
    from src.design.building_generator import _narrow_to_building, FLOOR_HEIGHT
    from src.design.requirements import check_requirements
    building = _narrow_to_building(floors, FLOOR_HEIGHT if floor_height is None else floor_height)
    report = validate_building(building, env=env)
    request_report = check_requirements(building, requirements).to_dict()
    feasible = report["validation"]["status"] == "passed" and request_report["ok"]
    problems = [i["label"] + "：" + i["detail"] for i in request_report["items"]
                if i["status"] != "met"]
    for name in ("plan_check", "code_check"):
        problems.extend(i["detail"] for i in report.get(name, {}).get("issues", [])
                        if i["severity"] in {"error", "violation"})
    if report["validation"]["status"] == "unverified":
        problems.append("驗證未完成，不能將此候選視為合格")
    return {"feasible": feasible, "requirement_check": request_report,
            "validation": report["validation"], "problems": problems}
