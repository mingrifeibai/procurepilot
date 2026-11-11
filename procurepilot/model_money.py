"""将领域整数分转换为清晰的模型金额文本，拦截无法对应的金额。"""

import re
from collections import Counter
from decimal import Decimal

from .planner import matches_need


def to_yuan(cents):
    return f"{Decimal(cents) / 100:.2f} 元"


def _quote_subtotals(context, quote):
    """从同一报价及需求数量计算整行小计，可按报价 ID 和需求序号复核。"""
    product = next(
        (
            evidence["product"]
            for evidence in context.get("product_evidence", [])
            if evidence["product"]["id"] == quote["product_id"]
        ),
        None,
    )
    requirement = context["requirement"]
    if product is None or quote["delivery_days"] > requirement["delivery_days"]:
        return []
    return [
        (index, need["quantity"], quote["price_cents"] * need["quantity"])
        for index, need in enumerate(requirement["needs"])
        if 1 < need["quantity"] <= quote["stock"] and matches_need(product, need)
    ]


def for_model(context):
    """只转换模型副本。数据库、求解和审批继续使用整数分。"""
    requirement = dict(context["requirement"])
    requirement["budget_cny"] = to_yuan(requirement.pop("budget_cents"))
    plan = dict(context["plan"]) if context.get("plan") else None
    feasible = plan is not None and plan["status"] == "feasible"
    quotes = []
    for item in context["quotes"]:
        quote = dict(item)
        quote["unit_price_cny"] = to_yuan(quote.pop("price_cents"))
        # 已有方案时，候选报价的整需求假设会与实际拆单行混在一起。
        subtotals = [] if feasible else _quote_subtotals(context, item)
        if subtotals:
            quote["line_subtotals"] = [
                {
                    "need_index": index,
                    "quantity": quantity,
                    "subtotal_cny": to_yuan(total),
                }
                for index, quantity, total in subtotals
            ]
        quotes.append(quote)
    if plan is not None and "total_cents" in plan:
        plan["total_cny"] = to_yuan(plan.pop("total_cents"))
    if feasible:
        allocated = Counter()
        lines = []
        for source in plan.get("lines", []):
            line = dict(source)
            quantity = line["quantity"]
            allocated[line["need_index"]] += quantity
            if "price_cents" in line:
                line["subtotal_cny"] = to_yuan(quantity * line.pop("price_cents"))
            lines.append(line)
        plan["lines"] = lines
        plan["need_coverage"] = [
            {
                "need_index": index,
                "required_quantity": need["quantity"],
                "allocated_quantity": allocated[index],
            }
            for index, need in enumerate(requirement["needs"])
        ]
    return {**context, "requirement": requirement, "quotes": quotes, "plan": plan}


_NUMBER = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"
_SUFFIX = re.compile(
    r"(?<![0-9A-Za-z.,-])"
    + _NUMBER
    + r"\s*(万元|千元|万块|千块|万(?=[，。；、）\s]|$)|元|块钱|块|分(?=[，。；、）\s]|$))"
)
_PREFIX = re.compile(r"[￥¥]\s*" + _NUMBER)


def amounts_supported(assessment, context):
    """识别金额并限定为预算、报价单价、确定性小计及已有方案总价。"""
    allowed = {context["requirement"]["budget_cents"]}
    allowed.update(quote["price_cents"] for quote in context["quotes"])
    plan = context.get("plan")
    if plan and plan["status"] == "feasible":
        allowed.update(
            line["quantity"] * line["price_cents"]
            for line in plan.get("lines", [])
            if "price_cents" in line
        )
    else:
        allowed.update(
            total
            for quote in context["quotes"]
            for _, _, total in _quote_subtotals(context, quote)
        )
    if plan and "total_cents" in plan:
        allowed.add(plan["total_cents"])
    fields = [assessment.summary, *assessment.risks, *assessment.missing_information]
    for field in fields:
        for value, unit in _SUFFIX.findall(field):
            scale = 1 if unit == "分" else 100
            if unit.startswith("万"):
                scale *= 10000
            if unit.startswith("千"):
                scale *= 1000
            if Decimal(value.replace(",", "")) * scale not in allowed:
                return False
        for value in _PREFIX.findall(field):
            if Decimal(value.replace(",", "")) * 100 not in allowed:
                return False
    return True
