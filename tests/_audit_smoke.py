"""假 audit provider（仅用于验证 `--audit` 门槛化通路，不实现 C2/C3/C4 业务逻辑）。

接口约定（卡 4d3267ff §4.3 / §4.5）：`fn(case, duo) -> dict`，返回 C2-C4 会发布到
ORCHESTRA_FIELDS 的字段。本文件里的 `fake_audit` 只做**占位**：它从 case.expect
里读 orchestra 期望值原样返回（证明「有 audit → 全部门槛化 PASS|FAIL」），并在个别
语义用例上按 C2 合并后的真值覆盖 seat 观测（如 TC-16 的「我也要」合并 → P 不再单独决策）。

不进 src；作为测试支撑留在 tests/，验收命令 `--audit tests._audit_smoke:fake_audit` 使用。
"""

# C2「我也要」合并语义：P 的短附和并入 D 的 AU，P 不再产生独立决策（TC-16 期望 P:0）。
MERGE_CASES = {"TC-16": {"decisions": {"D": 1, "P": 0}}}


def fake_audit(case, duo):
    """返回让用例此刻能门槛化判定所需的 audit 字段。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from assert_surface import ORCHESTRA_FIELDS
    expect = case.get("expect") or {}
    out = {}
    # 逐 orchestra 字段：期望即提供值（占位证明通路）
    for key in ORCHESTRA_FIELDS:
        if key in expect:
            val = expect[key]
            if key == "iu_eq":
                val = True
            out[key] = val
    # C2 合并语义覆盖 seat 观测（证明 audit 可携带 C2 真值）
    cid = case.get("case_id")
    if cid in MERGE_CASES:
        out.update(MERGE_CASES[cid])
    return out