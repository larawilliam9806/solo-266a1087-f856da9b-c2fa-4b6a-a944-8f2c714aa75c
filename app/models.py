"""提交清单的请求模型与合法性校验。

非法提交（不满足以下任一约束）在入库存储前即被拒绝（HTTP 422），
因此不会影响已发布的旧版本；分轮汇总中的非法更正同样被拒绝，
哨点保留上一次上报。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

MAX_BATCH = 40          # 整批提交 / 单轮结案最多 40 条
MAX_SENTINELS = 200     # 单轮应报哨点数量上限
MAX_SENTINEL_NAME = 64  # 哨点名最大长度


class CaseIn(BaseModel):
    case_id: str = Field(min_length=1, max_length=64, description="唯一病例号")
    age: int = Field(ge=0, le=100, description="年龄 0~100 岁")
    zip: str = Field(pattern=r"^\d{5}$", description="五位数字邮编")
    disease: str = Field(min_length=1, max_length=128, description="病种")

    @field_validator("case_id", "disease", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


def _ensure_unique_case_ids(cases: list[CaseIn]) -> list[CaseIn]:
    ids = [c.case_id for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"病例号重复: {', '.join(dupes)}")
    return cases


class SubmissionIn(BaseModel):
    k: int = Field(ge=2, le=5, description="k-匿名参数，2~5")
    cases: list[CaseIn] = Field(min_length=1, max_length=MAX_BATCH)

    @field_validator("cases")
    @classmethod
    def _unique_case_ids(cls, v: list[CaseIn]) -> list[CaseIn]:
        return _ensure_unique_case_ids(v)


class RoundOpenIn(BaseModel):
    """开轮请求：指定本轮应报哨点名单与 k-匿名参数。"""

    k: int = Field(ge=2, le=5, description="k-匿名参数，2~5")
    sentinels: list[str] = Field(
        min_length=1, max_length=MAX_SENTINELS, description="本轮应报哨点名单"
    )

    @field_validator("sentinels")
    @classmethod
    def _clean_sentinels(cls, v: list[str]) -> list[str]:
        names = [s.strip() for s in v]
        if any(not n for n in names):
            raise ValueError("哨点名不能为空")
        too_long = [n for n in names if len(n) > MAX_SENTINEL_NAME]
        if too_long:
            raise ValueError(
                f"哨点名超过 {MAX_SENTINEL_NAME} 字符: {', '.join(too_long)}"
            )
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"哨点重复: {', '.join(dupes)}")
        return names


class ReportIn(BaseModel):
    """哨点整份上报/更正：cases 为当次完整清单，空清单表示零例。

    清单非法（病例字段非法或清单内病例号重复）时整体拒绝（HTTP 422），
    该哨点保留上一次上报。
    """

    sentinel: str = Field(
        min_length=1, max_length=MAX_SENTINEL_NAME, description="哨点名"
    )
    cases: list[CaseIn] = Field(description="整份病例清单；空清单表示零例")

    @field_validator("sentinel", mode="before")
    @classmethod
    def _strip_sentinel(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("cases")
    @classmethod
    def _unique_case_ids(cls, v: list[CaseIn]) -> list[CaseIn]:
        return _ensure_unique_case_ids(v)
