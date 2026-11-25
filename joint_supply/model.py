"""领域模型：场、品种、规格、报价、订单、装车与承诺结果。

设计原则
--------
* 一切数字都保留来源（记录 id 与原文），不做“中间值补齐”。袋装只给区间时，
  下界按区间下界计，绝不按中点把不足的重量算够。
* 订单、报价、更正、装车回单是不同作用层级的记录，不能互相覆盖；更正通过
  ``refs`` 明确指向旧记录，而不是按词面相同合并。
* 装车一旦签收，已装部分即承担到场承诺，不能因为某场“愿意让量”而改划给别场。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# 单位与数量
# ---------------------------------------------------------------------------

class Unit(str, Enum):
    KG = "kg"      # 公斤（实秤）
    LB = "lb"      # 磅（袋装）
    JIN = "jin"    # 市斤
    CASE = "case"  # 筐 / 箱 / 件（可数单位；没有每筐净重时不能换算）

    @classmethod
    def parse(cls, text: str) -> "Unit":
        t = text.strip().lower().replace(" ", "")
        if t in {"kg", "公斤", "千克", "实秤公斤"}:
            return cls.KG
        if t in {"lb", "lbs", "磅"}:
            return cls.LB
        if t in {"斤", "市斤"}:
            return cls.JIN
        if t in {"筐", "箱", "件", "case", "袋", "包"}:
            return cls.CASE
        raise ValueError(f"未知单位: {text!r}")


KG_PER_LB = Decimal("0.45359237")
KG_PER_JIN = Decimal("0.5")


def to_kg(value: Decimal, unit: Unit) -> Optional[Decimal]:
    """换算到公斤；筐是可数单位，不知道每筐净重时不能换算。"""
    if unit is Unit.KG:
        return value
    if unit is Unit.LB:
        return (value * KG_PER_LB).quantize(Decimal("0.001"))
    if unit is Unit.JIN:
        return (value * KG_PER_JIN).quantize(Decimal("0.001"))
    return None


@dataclass(frozen=True)
class Quantity:
    """一个数量表达。区间保留上下界，单位保留，可追踪来源。"""

    low: Decimal
    high: Decimal
    unit: Unit
    source_id: str = ""

    def __post_init__(self):
        if self.low < 0 or self.high < self.low:
            raise ValueError(f"非法数量区间: {self.low}..{self.high}")

    @property
    def certain_low_kg(self) -> Decimal:
        """能确定计入的公斤下界。筐装且无每筐净重时为 0，不猜中点。"""
        converted = to_kg(self.low, self.unit)
        return converted if converted is not None else Decimal("0")

    @property
    def certain_high_kg(self) -> Decimal:
        converted = to_kg(self.high, self.unit)
        return converted if converted is not None else Decimal("0")

    @property
    def is_exact(self) -> bool:
        return self.low == self.high

    @property
    def is_range(self) -> bool:
        return self.low != self.high

    def add(self, other: "Quantity") -> "Quantity":
        if self.unit is not other.unit:
            raise ValueError("不同单位的数量不能直接相加")
        return Quantity(self.low + other.low, self.high + other.high, self.unit)

    def __str__(self) -> str:
        if self.low == self.high:
            return f"{_fmt(self.low)}{self.unit.value}"
        return f"{_fmt(self.low)}–{_fmt(self.high)}{self.unit.value}"


def fmt_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


_fmt = fmt_decimal


# ---------------------------------------------------------------------------
# 记录层级
# ---------------------------------------------------------------------------

class Kind(str, Enum):
    ORDER = "order"                  # 场方订单：硬性约束和承诺
    CONFIRMATION = "confirmation"    # 订单确认：只确认已有条件，不新增货
    QUOTE = "quote"                  # 供方报价：可售货与条件
    CORRECTION = "correction"        # 行内补正：改指向 / 改数量，不新增承诺
    LOAD = "load"                    # 装车回单 / 签收：已交付事实
    GLOSSARY = "glossary"            # 名称表 / 规格词汇 / 秤单单位
    INQUIRY = "inquiry"              # 有条件询盘：未确认，不能当承诺
    NOTE = "note"                    # 其它备注


class Role(str, Enum):
    BUYER = "buyer"        # 采购 / 场方
    SUPPLIER = "supplier"  # 供应商 / 档口
    CARRIER = "carrier"    # 车队 / 司机
    UNKNOWN = "unknown"


class Verdict(str, Enum):
    CONFIRMED = "confirmed"      # 已具备兑现条件
    CONDITIONAL = "conditional"  # 有货但缺条件，需找特定方确认
    TENTATIVE = "tentative"      # 仅暂留，不可承诺
    BLOCKED = "blocked"          # 明确不能兑现 / 缺量
    DELIVERED = "delivered"      # 已签收交割


# ---------------------------------------------------------------------------
# 主数据
# ---------------------------------------------------------------------------

@dataclass
class Record:
    """一条原始记录。text 永远保留原文。"""

    id: str
    kind: Kind
    speaker: str
    role: Role
    text: str
    refs: tuple[str, ...] = ()
    sequence: int = 0

    @property
    def header(self) -> str:
        return self.text.split("|", 1)[0].strip() if "|" in self.text else ""

    @property
    def body(self) -> str:
        return self.text.split("|", 1)[1].strip() if "|" in self.text else self.text.strip()


@dataclass
class MarketProfile:
    """场的名称表与秤单单位。同一个词在不同场可以指向不同品种。"""

    name: str
    aliases: dict[str, str] = field(default_factory=dict)
    default_product: Optional[str] = None
    bag_unit: Unit = Unit.LB
    case_unit: Unit = Unit.KG
    raw_text: str = ""

    def resolve(self, term: str) -> Optional[str]:
        cleaned = _clean_term(term)
        if not cleaned:
            return None
        for alias in sorted(self.aliases, key=len, reverse=True):
            if alias in cleaned:
                return self.aliases[alias]
        if cleaned in {"品", "货", "本场品", "原规格", "原品"}:
            return self.default_product
        # 名称表缺省时，仍按本场默认品种解释“青芯”这类同词；跨场使用时
        # planner 会要求供方确认，而不是静默当成同一品种。
        if self.default_product and any(
            word in cleaned for word in ("青芯", "本场品", "原规格")
        ):
            return self.default_product
        return None


@dataclass
class Spec:
    """规格词汇：名称、别名、单位提示。"""

    canonical: str
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unit_hint: Optional[Unit] = None

    def variants_for(self, market: str) -> tuple[str, ...]:
        return tuple(self.aliases.get(market, ()))


@dataclass
class SubstituteRule:
    """替代规则：什么可以替什么、替多少、价差上限。"""

    substitute_terms: tuple[str, ...]
    max_share: Optional[Decimal] = None      # 替代品最多占总需求比例
    max_price_gap: Optional[Decimal] = None  # 每公斤价差上限（元）
    note: str = ""
    accepted_by_buyer: bool = True           # 订单已明确接受“在允许范围内组合”


@dataclass
class MarketOrder:
    """场方订单的硬约束。"""

    market: str
    record_id: str
    text: str
    product_terms: tuple[str, ...] = ()
    product: Optional[str] = None
    min_kg: Optional[Decimal] = None
    min_cases: Optional[int] = None
    max_cases: Optional[int] = None
    case_tolerance: int = 0
    excluded_terms: tuple[str, ...] = ()
    only_accept_terms: tuple[str, ...] = ()
    required_attributes: tuple[str, ...] = ()
    allowed_substitute: Optional[SubstituteRule] = None
    allow_substitute: bool = True
    combine_allowed: bool = False
    deadline_day: Optional[int] = None
    deadline_text: str = ""
    reference_quote: Optional[str] = None
    reference_note: str = ""
    conditions: list[Condition] = field(default_factory=list)

    @property
    def has_product(self) -> bool:
        return bool(self.product_terms or self.product)


@dataclass
class Condition:
    """一条未满足的条件。confirm_with 指明确要找哪一方确认。"""

    topic: str
    text: str
    confirm_with: Role = Role.UNKNOWN
    source_id: str = ""
    blocking: bool = True
    satisfied_by: Optional[str] = None

    @property
    def open(self) -> bool:
        return self.blocking and self.satisfied_by is None


TOPIC_MIXED_STALL = "mixed_stall"      # 不同档口混装
TOPIC_WEIGHING = "weighing"            # 未过秤 / 需实秤
TOPIC_PRICE = "price"                  # 价差 / 原价待核
TOPIC_ARRIVAL = "arrival"              # 到场窗口
TOPIC_REFERENCE = "reference"          # 上次报价指代
TOPIC_NON_GUARANTEE = "non_guarantee"  # 供方“不保证”
TOPIC_EXCLUSION = "exclusion"          # 采购“不要”
TOPIC_QUANTITY = "quantity"            # 数量待定
TOPIC_PRODUCT = "product"              # 同词异义 / 品种指向不明
TOPIC_SUBSTITUTE = "substitute"         # 替代需场方承认
TOPIC_GENERIC = "generic"


@dataclass
class QuoteLot:
    """一批可供货。原始文本按 lot 段落保留。"""

    id: str
    record_id: str
    text: str
    product: Optional[str] = None
    market_hint: Optional[str] = None
    quantity: Optional[Quantity] = None
    packages: Optional[int] = None
    package_kg_low: Optional[Decimal] = None
    package_kg_high: Optional[Decimal] = None
    package_unit: Unit = Unit.CASE
    divisible: bool = True
    unit_price: Optional[Decimal] = None
    price_ref: Optional[str] = None
    origin: str = ""
    conditions: list[Condition] = field(default_factory=list)
    arrival_day: Optional[int] = None
    arrival_text: str = ""
    weighed: Optional[bool] = None  # None=未说明，True=实秤，False=明确未过秤
    tentative: bool = False
    duplicate_of: Optional[str] = None
    released: bool = False
    attributes: dict[str, object] = field(default_factory=dict)
    note: str = ""
    refs: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.duplicate_of is None and not self.released

    @property
    def open_conditions(self) -> list[Condition]:
        return [condition for condition in self.conditions if condition.open]

    @property
    def low_kg(self) -> Decimal:
        if self.quantity is None:
            return Decimal("0")
        return self.quantity.certain_low_kg

    @property
    def high_kg(self) -> Decimal:
        if self.quantity is None:
            return Decimal("0")
        return self.quantity.certain_high_kg

    @property
    def is_package_lot(self) -> bool:
        return bool(self.packages is not None and self.package_kg_low is not None)

    def package_kg(self) -> Optional[Decimal]:
        if not self.is_package_lot:
            return None
        if self.package_kg_high is None or self.package_kg_high == self.package_kg_low:
            return self.package_kg_low
        return None


@dataclass
class LoadLine:
    """装车回单中的一行。"""

    record_id: str
    lot_id: Optional[str]
    market: str
    quantity: Optional[Quantity]
    packages: Optional[int] = None
    sealed: bool = False
    weighed: bool = False
    loaded: bool = False
    text: str = ""
    truck: Optional[str] = None
    note: str = ""

    @property
    def delivered(self) -> bool:
        """已装且能核出净重的部分才计入已交割；过秤与否单独用 weighed 表达。

        回单只写“尚无过秤净重”时 quantity 为空，不会产生交割重量；报价已给
        每筐/每袋净重且回单写明装了几件的，按该净重锁定到场承诺。
        """
        return self.loaded and self.quantity is not None


@dataclass
class Confirmation:
    """订单确认 / 采购确认。只关闭已存在的条件，不创造新的供给。"""

    record_id: str
    market: Optional[str]
    topics: tuple[str, ...]
    text: str


@dataclass
class ReferenceIssue:
    """无法自动判定、必须找人确认的指代问题。"""

    kind: str
    message: str
    confirm_with: Role = Role.UNKNOWN
    source_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "confirm_with": self.confirm_with.value,
            "source_ids": list(self.source_ids),
        }


# ---------------------------------------------------------------------------
# 规划结果
# ---------------------------------------------------------------------------

@dataclass
class Allocation:
    lot_id: str
    market: str
    kg_low: Decimal
    kg_high: Decimal
    packages: Optional[int] = None
    role: str = "primary"          # primary / substitute / locked
    tentative: bool = False        # 是否由暂留批次移出
    conditions: list[Condition] = field(default_factory=list)
    sources: tuple[str, ...] = ()
    note: str = ""

    @property
    def conditional(self) -> bool:
        return any(condition.open and condition.blocking for condition in self.conditions)

    @property
    def blocking_conditions(self) -> list[Condition]:
        return [condition for condition in self.conditions if condition.open and condition.blocking]


@dataclass
class MarketPlan:
    market: str
    order: Optional[MarketOrder]
    verdict: Verdict
    reason: str
    locked_kg: Decimal = Decimal("0")
    delivered_kg: Decimal = Decimal("0")
    allocated_low_kg: Decimal = Decimal("0")
    allocated_high_kg: Decimal = Decimal("0")
    confirmed_kg: Decimal = Decimal("0")
    conditional_kg: Decimal = Decimal("0")
    shortage_kg: Decimal = Decimal("0")
    conditions: list[Condition] = field(default_factory=list)
    advisory_conditions: list[Condition] = field(default_factory=list)
    allocations: list[Allocation] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    sources: tuple[str, ...] = ()


@dataclass
class LoadInstruction:
    action: str                  # load_now / hold / weigh_first / do_not_load
    lot_id: Optional[str]
    market: Optional[str]
    packages: Optional[int] = None
    kg_low: Optional[Decimal] = None
    kg_high: Optional[Decimal] = None
    note: str = ""
    confirm_with: Optional[Role] = None
    sources: tuple[str, ...] = ()
    original_text: str = ""


@dataclass
class Plan:
    market_plans: dict[str, MarketPlan] = field(default_factory=dict)
    load_instructions: list[LoadInstruction] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    issues: list[ReferenceIssue] = field(default_factory=list)
    lots: dict[str, QuoteLot] = field(default_factory=dict)
    profiles: dict[str, MarketProfile] = field(default_factory=dict)
    records: dict[str, Record] = field(default_factory=dict)
    loads: list[LoadLine] = field(default_factory=list)
    record_count: int = 0
    ignored_duplicates: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "metadata": self.metadata,
            "record_count": self.record_count,
            "markets": {
                name: _market_dict(plan) for name, plan in self.market_plans.items()
            },
            "load_instructions": [_instruction_dict(item) for item in self.load_instructions],
            "released": list(self.released),
            "ignored_duplicates": list(self.ignored_duplicates),
            "conflicts": list(self.conflicts),
            "issues": [issue.as_dict() for issue in self.issues],
            "loads": [
                {
                    "record_id": line.record_id,
                    "lot_id": line.lot_id,
                    "market": line.market,
                    "quantity": str(line.quantity) if line.quantity else None,
                    "packages": line.packages,
                    "sealed": line.sealed,
                    "weighed": line.weighed,
                    "loaded": line.loaded,
                    "delivered": line.delivered,
                    "text": line.text,
                    "note": line.note,
                }
                for line in self.loads
            ],
            "records": {
                record_id: {
                    "kind": record.kind.value,
                    "speaker": record.speaker,
                    "text": record.text,
                }
                for record_id, record in self.records.items()
            },
            "lots": {lot_id: _lot_dict(lot) for lot_id, lot in self.lots.items()},
            "profiles": {
                name: {
                    "aliases": profile.aliases,
                    "default_product": profile.default_product,
                    "bag_unit": profile.bag_unit.value,
                    "case_unit": profile.case_unit.value,
                    "raw_text": profile.raw_text,
                }
                for name, profile in self.profiles.items()
            },
        }


def _num(value: Optional[Decimal]) -> Optional[str]:
    return _fmt(value) if value is not None else None


def _condition_dict(condition: Condition) -> dict:
    return {
        "topic": condition.topic,
        "text": condition.text,
        "confirm_with": condition.confirm_with.value,
        "source_id": condition.source_id,
        "open": condition.open,
    }


def _market_dict(plan: MarketPlan) -> dict:
    return {
        "verdict": plan.verdict.value,
        "reason": plan.reason,
        "locked_kg": _num(plan.locked_kg),
        "delivered_kg": _num(plan.delivered_kg),
        "allocated_low_kg": _num(plan.allocated_low_kg),
        "allocated_high_kg": _num(plan.allocated_high_kg),
        "confirmed_kg": _num(plan.confirmed_kg),
        "conditional_kg": _num(plan.conditional_kg),
        "shortage_kg": _num(plan.shortage_kg),
        "conditions": [_condition_dict(condition) for condition in plan.conditions],
        "advisory_conditions": [_condition_dict(condition) for condition in plan.advisory_conditions],
        "allocations": [
            {
                "lot_id": allocation.lot_id,
                "kg_low": _num(allocation.kg_low),
                "kg_high": _num(allocation.kg_high),
                "packages": allocation.packages,
                "role": allocation.role,
                "tentative": allocation.tentative,
                "conditional": allocation.conditional,
                "conditions": [_condition_dict(condition) for condition in allocation.conditions],
                "sources": list(allocation.sources),
                "note": allocation.note,
            }
            for allocation in plan.allocations
        ],
        "alternatives": list(plan.alternatives),
        "sources": list(plan.sources),
    }


def _instruction_dict(item: LoadInstruction) -> dict:
    return {
        "action": item.action,
        "lot_id": item.lot_id,
        "market": item.market,
        "packages": item.packages,
        "kg_low": _num(item.kg_low),
        "kg_high": _num(item.kg_high),
        "note": item.note,
        "confirm_with": item.confirm_with.value if item.confirm_with else None,
        "sources": list(item.sources),
        "original_text": item.original_text,
    }


def _lot_dict(lot: QuoteLot) -> dict:
    return {
        "record_id": lot.record_id,
        "product": lot.product,
        "market_hint": lot.market_hint,
        "quantity": str(lot.quantity) if lot.quantity else None,
        "low_kg": _num(lot.low_kg),
        "high_kg": _num(lot.high_kg),
        "packages": lot.packages,
        "package_kg_low": _num(lot.package_kg_low),
        "package_kg_high": _num(lot.package_kg_high),
        "divisible": lot.divisible,
        "unit_price": _num(lot.unit_price),
        "price_ref": lot.price_ref,
        "origin": lot.origin,
        "arrival_day": lot.arrival_day,
        "arrival_text": lot.arrival_text,
        "weighed": lot.weighed,
        "tentative": lot.tentative,
        "duplicate_of": lot.duplicate_of,
        "released": lot.released,
        "available": lot.available,
        "attributes": lot.attributes,
        "conditions": [_condition_dict(condition) for condition in lot.conditions],
        "note": lot.note,
        "refs": list(lot.refs),
        "original_text": lot.text,
    }


def _clean_term(term: str) -> str:
    cleaned = term.strip()
    for prefix in ("按上次", "上次的", "上次", "要", "给"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    for suffix in ("那批", "这批", "的货", "货", "的", "品"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return cleaned.strip(" ，,。；;：:")
