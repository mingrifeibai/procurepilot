"""领域输入契约。金额统一转为整数分，拒绝浮点误差和隐式兼容推断。"""

from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class StrictModel(BaseModel):
    """拒绝拼错字段，防止用户以为约束生效而实际被忽略。"""

    model_config = ConfigDict(extra="forbid")


class Need(StrictModel):
    category: Literal["computer", "monitor", "dock"]
    quantity: int = Field(ge=1, le=10000)
    min_memory_gb: int = Field(default=0, ge=0)
    interface: str = "usb-c"
    purpose: str = Field(default="", max_length=100)


class Requirement(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    budget: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    delivery_days: int = Field(ge=1, le=365)
    allow_split: bool = True
    needs: list[Need] = Field(min_length=1, max_length=30)


class WorkspaceInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    organization_id: str | None = Field(default=None, min_length=1, max_length=36)


class QuoteInput(StrictModel):
    product_id: str
    supplier: str = Field(min_length=1, max_length=100)
    unit_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    stock: int = Field(ge=0, le=1000000)
    delivery_days: int = Field(ge=1, le=365)
    valid_hours: int = Field(default=72, ge=1, le=8760)
    source: str = Field(min_length=1, max_length=500)


class ReceiptInput(StrictModel):
    quote_id: str
    quantity: int = Field(ge=1)
    accepted: int = Field(ge=0)
    note: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ResolutionInput(StrictModel):
    accepted_after_reinspection: int = Field(ge=0)
    note: str = Field(min_length=1, max_length=1000)


def cents(value) -> int:
    """只在输入边界转换；求解和账务计算始终使用整数。"""
    return int(Decimal(str(value)) * 100)
