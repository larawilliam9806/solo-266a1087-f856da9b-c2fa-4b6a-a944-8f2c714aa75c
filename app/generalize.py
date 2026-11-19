"""k-匿名泛化核心逻辑。

对整批病例在 9 种（年龄段 x 邮区前缀）组合下评估可发布性：
- 每个可见（泛化后）组合须 >= k 条且含 >= 2 种病种；
- 不满足的组合只能整组抑制，抑制总数须 <= 2；
- 至少保留一个可见组合；
- 目标：保留病例最多；并列时依次选更细的年龄段、更长的邮区前缀。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

AGE_BANDS: tuple[int, ...] = (5, 10, 20)   # 允许的年龄段（岁），由细到粗
ZIP_PREFIXES: tuple[int, ...] = (3, 2, 0)  # 允许的邮区前缀位数，由长到短
MAX_SUPPRESSIONS = 2                        # 最多抑制条数
MIN_DISEASES = 2                            # 每个可见组合至少包含的病种数


@dataclass(frozen=True)
class CaseRecord:
    case_id: str
    age: int
    zip: str
    disease: str


@dataclass(frozen=True)
class Option:
    """一种可行的发布方案。"""
    age_band: int
    zip_prefix: int
    suppressed_ids: tuple[str, ...]
    publishable_groups: int

    @property
    def suppressed_count(self) -> int:
        return len(self.suppressed_ids)


def age_band_label(age: int, band: int) -> str:
    """年龄 -> 年龄段标签，如 band=5 时 34 -> '30-34'。"""
    lo = (age // band) * band
    return f"{lo}-{lo + band - 1}"


def zip_label(zip5: str, prefix: int) -> str:
    """五位邮编 -> 保留前 prefix 位、其余打码，如 '12345',3 -> '123**'。"""
    return zip5[:prefix] + "*" * (5 - prefix)


def _rank(option: Option) -> tuple[int, int, int]:
    # 抑制更少（保留更多）优先 -> 年龄段更细 -> 邮区前缀更长
    return (option.suppressed_count, option.age_band, -option.zip_prefix)


def evaluate(records: Sequence[CaseRecord], k: int):
    """评估全部参数组合。

    返回 (best, diagnostics)：
    - best: 最优可行 Option，无可行方案时为 None；
    - diagnostics: 每种参数组合的需要抑制条数/可保留组数，用于说明原因。
    """
    diagnostics: list[dict] = []
    best: Option | None = None
    for band in AGE_BANDS:
        for prefix in ZIP_PREFIXES:
            groups: dict[tuple[int, str], list[CaseRecord]] = {}
            for r in records:
                groups.setdefault((r.age // band, r.zip[:prefix]), []).append(r)
            suppressed: list[str] = []
            good = 0
            for members in groups.values():
                diseases = {m.disease for m in members}
                if len(members) >= k and len(diseases) >= MIN_DISEASES:
                    good += 1
                else:
                    # 不达标的组无法通过部分抑制修复，只能整组抑制
                    suppressed.extend(m.case_id for m in members)
            feasible = good >= 1 and len(suppressed) <= MAX_SUPPRESSIONS
            diagnostics.append({
                "age_band": band,
                "zip_prefix": prefix,
                "groups": len(groups),
                "publishable_groups": good,
                "suppressions_required": len(suppressed),
                "feasible": feasible,
            })
            if feasible:
                option = Option(band, prefix, tuple(suppressed), good)
                if best is None or _rank(option) < _rank(best):
                    best = option
    return best, diagnostics


def infeasible_reason(k: int, diagnostics: Sequence[dict]) -> str:
    """生成无法达标的原因说明。"""
    head = (
        f"无法达标：k={k} 时，9 种（年龄段 × 邮区前缀）泛化组合均不能满足"
        f"“每个可见组合 ≥{k} 条且 ≥{MIN_DISEASES} 种病种、最多抑制 "
        f"{MAX_SUPPRESSIONS} 条、至少保留一组”。"
    )
    with_groups = [d for d in diagnostics if d["publishable_groups"] >= 1]
    if not with_groups:
        return head + "所有组合下均不存在同时满足数量与病种多样性要求的可见组合。"
    nearest = min(with_groups, key=lambda d: (d["suppressions_required"], d["age_band"], -d["zip_prefix"]))
    return head + (
        f"最接近的方案（年龄段 {nearest['age_band']} 岁、邮区保留前 "
        f"{nearest['zip_prefix']} 位）仍需抑制 {nearest['suppressions_required']} 条，"
        f"超过上限 {MAX_SUPPRESSIONS} 条。"
    )
