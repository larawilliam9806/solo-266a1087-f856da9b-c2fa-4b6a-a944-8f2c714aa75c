"""提交清单的请求模型与合法性校验。

非法提交（不满足以下任一约束）在入库存储前即被拒绝（HTTP 422），
因此不会影响已发布的旧版本。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

MAX_BATCH = 40  # 整批提交最多 40 条


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


class SubmissionIn(BaseModel):
    k: int = Field(ge=2, le=5, description="k-匿名参数，2~5")
    cases: list[CaseIn] = Field(min_length=1, max_length=MAX_BATCH)

    @field_validator("cases")
    @classmethod
    def _unique_case_ids(cls, v: list[CaseIn]) -> list[CaseIn]:
        ids = [c.case_id for c in v]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"病例号重复: {', '.join(dupes)}")
        return v
