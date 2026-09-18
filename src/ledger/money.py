"""金额与日期工具。

所有金额在账本内部以字符串形式保存（定点 6 位小数，与
``reference/domain.json`` 的 ``quantity_precision`` 对齐），计算时转换为
:class:`decimal.Decimal`，避免浮点误差进入长期留存的流水。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP


class Money:
    """定点小数金额运算器。"""

    def __init__(self, precision: int = 6) -> None:
        self.precision = precision
        self.quant = Decimal(1).scaleb(-precision)

    def dec(self, value: object) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def q(self, value: object) -> Decimal:
        """按账本精度取整（四舍五入）。"""
        return self.dec(value).quantize(self.quant, rounding=ROUND_HALF_UP)

    def s(self, value: object) -> str:
        """序列化为定点小数字符串，供事件与快照保存。"""
        return format(self.q(value), "f")


def parse_date(value: object, field: str = "date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 格式的字符串")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 格式的字符串: {value!r}") from exc


def month_of(day: date) -> str:
    return day.strftime("%Y-%m")


def next_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{mon + 1:02d}"


def valid_month(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
        return False
    try:
        year, mon = int(value[:4]), int(value[5:7])
    except ValueError:
        return False
    return 1 <= mon <= 12 and 1900 <= year <= 9999
