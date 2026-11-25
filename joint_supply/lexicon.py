"""中文业务文本的词法工具：数字、单位、数量、否定作用域与品种别名。

这些函数只回答“文字里说了什么”，不替业务方做判断。需要业务判断的地方
一律返回显式条件，交给 planner 决定找谁确认。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from .model import (  # noqa: F401  (to_kg 由本模块转出给 parser 使用)
    KG_PER_JIN,
    KG_PER_LB,
    MarketProfile,
    Quantity,
    Unit,
    to_kg,
)

# ---------------------------------------------------------------------------
# 数字
# ---------------------------------------------------------------------------

CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
CN_NUM_CHARS = "".join(CN_DIGITS) + "".join(CN_UNITS) + "半"

NUM = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万半]+(?:点[零〇一二三四五六七八九]+)?)"
UNIT = r"(?:公斤|千克|kg|KG|磅|斤|市斤)"
RANGE_SEP = r"(?:[–—\-~～]|至|到)"
RANGE = rf"{NUM}\s*{RANGE_SEP}\s*{NUM}"


def cn_to_decimal(token: str) -> Decimal:
    """把阿拉伯数字或中文数字转成 Decimal。'半' = 0.5。"""
    token = token.strip()
    if not token:
        raise ValueError("空数字")
    if token == "半":
        return Decimal("0.5")
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return Decimal(token)
    if "点" in token:
        whole, fraction = token.split("点", 1)
        whole_value = cn_to_decimal(whole) if whole else Decimal(0)
        digits = "".join(str(CN_DIGITS[char]) for char in fraction)
        return whole_value + Decimal(f"0.{digits}")

    total = 0
    section = 0
    number = 0
    for char in token:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char in ("十", "百", "千"):
            unit = CN_UNITS[char]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        elif char == "万":
            section += number
            total += section * 10000
            section = 0
            number = 0
        else:
            raise ValueError(f"无法解析数字: {token!r}")
    return Decimal(total + section + number)


def parse_number(token: str) -> Decimal:
    return cn_to_decimal(token)


def parse_range(token: str, default: Optional[Decimal] = None) -> tuple[Decimal, Decimal]:
    """解析 '15–20' / '十五到二十' / '45'，返回 (下界, 上界)。"""
    token = token.strip()
    match = re.fullmatch(rf"({NUM})\s*{RANGE_SEP}\s*({NUM})", token)
    if match:
        low = cn_to_decimal(match.group(1))
        high = cn_to_decimal(match.group(2))
        if high < low:
            low, high = high, low
        return low, high
    if default is not None and not token:
        return default, default
    value = cn_to_decimal(token)
    return value, value


def money_to_yuan(amount: Decimal, unit: Optional[str]) -> Decimal:
    """价差单位换算到元。"""
    if unit in ("角",):
        return amount / Decimal("10")
    if unit in ("分",):
        return amount / Decimal("100")
    return amount


# ---------------------------------------------------------------------------
# 品种与规格
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProductSpec:
    key: str
    display: str
    aliases: tuple[str, ...]
    attributes: tuple[str, ...] = ()


PRODUCTS: tuple[ProductSpec, ...] = (
    ProductSpec(
        "low_root_white_stem",
        "低根白梗",
        ("低根白梗", "低根品", "低根"),
        ("stem:white", "root:low"),
    ),
    ProductSpec(
        "white_stem",
        "白梗菜",
        ("白梗菜", "白梗", "东场品", "东场青芯", "白梗的青芯"),
        ("stem:white", "root:normal"),
    ),
    ProductSpec(
        "short_stem",
        "辣味短梗",
        ("辣味短梗", "短梗", "西场青芯", "西场短梗"),
        ("stem:short", "spicy:true"),
    ),
    ProductSpec(
        "fine_leaf",
        "细叶",
        ("细叶", "细叶菜", "小叶"),
        ("leaf:fine",),
    ),
)

_ALIAS_INDEX: dict[str, ProductSpec] = {}
for _spec in PRODUCTS:
    for _alias in _spec.aliases:
        _ALIAS_INDEX.setdefault(_alias, _spec)
# 长别名优先，避免“低根白梗”被“白梗”抢先。
_ALIASES_BY_LENGTH = sorted(_ALIAS_INDEX, key=len, reverse=True)


def product_spec(key: str) -> Optional[ProductSpec]:
    for spec in PRODUCTS:
        if spec.key == key:
            return spec
    return None


def display_product(key: Optional[str]) -> str:
    if not key:
        return "未识别"
    spec = product_spec(key)
    return spec.display if spec else key


def canonical_product(term: str, market: Optional[str], profiles: dict[str, MarketProfile]) -> Optional[str]:
    """按场优先解析品种；同词异义必须靠场名或场的名称表区分。"""
    cleaned = _clean_product_term(term)
    if not cleaned:
        return None
    if market and market in profiles:
        resolved = profiles[market].resolve(cleaned)
        if resolved:
            return resolved
    for alias in _ALIASES_BY_LENGTH:
        if alias in cleaned:
            return _ALIAS_INDEX[alias].key
    # 独立的“青芯”没有场名时不猜，交给调用方记为歧义。
    return None


def is_ambiguous_product(term: str, profiles: dict[str, MarketProfile]) -> bool:
    cleaned = _clean_product_term(term)
    if not cleaned:
        return False
    if any(alias in cleaned for alias in _ALIAS_INDEX):
        return False
    return cleaned in {"青芯"}


def _clean_product_term(term: str) -> str:
    cleaned = term.strip(" ，,。；;：:、")
    for prefix in ("按上次", "上次的", "上次", "要", "给", "另有", "还有", "另有"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    for suffix in ("那批", "这批", "本批", "的货", "货"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return cleaned.strip(" 的，,。；;：:")


def attributes_in(text: str) -> dict[str, object]:
    """读出不影响品种 key 的附加属性，如无根、细叶。"""
    attributes: dict[str, object] = {}
    if "无根" in text:
        attributes["root"] = "none"
    elif "低根" in text:
        attributes["root"] = "low"
    if "细叶" in text:
        attributes["leaf"] = "fine"
    if "粗叶" in text:
        attributes["leaf"] = "coarse"
    return attributes


# ---------------------------------------------------------------------------
# 否定作用域
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Negation:
    kind: str      # buyer_exclusion / supplier_non_guarantee / fact_negative / order_no_substitute
    marker: str
    obj: str
    start: int
    end: int


BUYER_EXCLUSION = "buyer_exclusion"
SUPPLIER_NON_GUARANTEE = "supplier_non_guarantee"
FACT_NEGATIVE = "fact_negative"
ORDER_NO_SUBSTITUTE = "order_no_substitute"

_CLAUSE_BREAK = "，,。；;！？\n"


def clause_start(text: str, index: int) -> int:
    start = 0
    for pos in range(index - 1, -1, -1):
        if text[pos] in _CLAUSE_BREAK:
            start = pos + 1
            break
    return start


def clause_text(text: str, index: int) -> str:
    start = clause_start(text, index)
    end = len(text)
    for pos in range(index, len(text)):
        if text[pos] in _CLAUSE_BREAK:
            end = pos
            break
    return text[start:end]


def find_negations(text: str) -> list[Negation]:
    """区分采购的“不要”、供方的“不保证”和事实否定。

    “无根”是品种属性，不是“无 + 根”的否定；这里显式排除。
    """
    found: list[Negation] = []

    for match in re.finditer(r"不要(?P<obj>[^，,。；;！？]{1,14}?)(?:之外|以外|外的|外)", text):
        found.append(Negation(BUYER_EXCLUSION, "不要…外", match.group("obj"), match.start(), match.end()))

    for match in re.finditer(r"不要(?P<obj>[^，,。；;！？]{1,12})", text):
        found.append(Negation(BUYER_EXCLUSION, "不要", match.group("obj"), match.start(), match.end()))

    for match in re.finditer(r"(?:不保证|不能保证|无法保证|没法保证)(?P<obj>[^，,。；;！？]{0,12})", text):
        found.append(Negation(SUPPLIER_NON_GUARANTEE, "不保证", match.group("obj"), match.start(), match.end()))

    for match in re.finditer(r"(?P<obj>[^，,。；;！？]{0,8}?)(?:除外|之外)", text):
        found.append(Negation(BUYER_EXCLUSION, "除外", match.group("obj"), match.start(), match.end()))

    for match in re.finditer(r"(?:不换菜|不换品|不换品种|不要替代|不接受替代|不能替代)", text):
        found.append(Negation(ORDER_NO_SUBSTITUTE, "不换", match.group(0), match.start(), match.end()))

    for match in re.finditer(r"(?:尚无|尚未|未曾|没有|未|无)(?P<obj>[^，,。；;！？]{0,10})", text):
        obj = match.group("obj")
        # “无根”是属性，不做否定。
        if match.group(0).startswith("无") and obj.startswith("根"):
            continue
        found.append(Negation(FACT_NEGATIVE, match.group(0)[0], obj, match.start(), match.end()))

    return _dedupe_negations(found)


def _dedupe_negations(items: Iterable[Negation]) -> list[Negation]:
    seen: set[tuple[str, int, int]] = set()
    result = []
    for item in items:
        key = (item.kind, item.start, item.end)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return sorted(result, key=lambda item: item.start)


def negated_near(text: str, index: int, window: int = 14) -> Optional[Negation]:
    """判断某个位置前的同一分句里是否有否定。"""
    start = max(clause_start(text, index), index - window)
    prefix = text[start:index]
    for negation in find_negations(prefix):
        if negation.kind in (FACT_NEGATIVE, SUPPLIER_NON_GUARANTEE):
            return negation
    return None


# ---------------------------------------------------------------------------
# 数量
# ---------------------------------------------------------------------------

CONTAINERS = ("筐", "箱", "件", "袋", "包")
QUANTITY_UNITS = ("公斤", "千克", "kg", "KG", "磅", "斤", "市斤")

_PACKAGE_RE = re.compile(
    rf"(?P<count>{NUM})\s*(?P<container>{'|'.join(CONTAINERS)})"
    rf"[，,]?\s*(?:(?:净重|每筐|每袋|每件|每包)\s*)?"
    rf"(?:(?P<per>{RANGE}|{NUM})\s*(?P<unit>{UNIT}))?"
)
_PER_PACKAGE_RE = re.compile(
    rf"(?:每(?P<container>筐|袋|箱|件|包)|净重)\s*"
    rf"(?P<per>{RANGE}|{NUM})\s*(?P<unit>{UNIT})"
)
_WEIGHT_RE = re.compile(
    rf"(?P<value>{RANGE}|{NUM})\s*(?P<unit>{UNIT})"
)


@dataclass
class QuantityParse:
    quantity: Optional[Quantity] = None
    packages: Optional[int] = None
    package_kg_low: Optional[Decimal] = None
    package_kg_high: Optional[Decimal] = None
    package_unit: Unit = Unit.CASE
    container: Optional[str] = None
    per_package: bool = False
    unit_hint: Optional[Unit] = None
    spans: list[tuple[int, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_range(self) -> bool:
        return bool(self.quantity and self.quantity.is_range)


def parse_unit(token: str) -> Unit:
    return Unit.parse(token)


def unit_default_for_container(container: str, profile: Optional[MarketProfile], explicit: Optional[str]) -> Unit:
    if explicit:
        return parse_unit(explicit)
    if profile is not None:
        if container in ("袋", "包") and profile.bag_unit is Unit.LB:
            return Unit.LB
        if container in ("筐", "箱", "件") and profile.case_unit is Unit.KG:
            return Unit.KG
    return Unit.KG


def parse_quantity(
    text: str,
    *,
    profile: Optional[MarketProfile] = None,
    source_id: str = "",
) -> QuantityParse:
    """从一段报价/回单文字里读出数量。

    规则：
    * 有“N 筐/袋”且给出每件净重时，总重 = N × 每件（区间保留区间）。
    * 只有件数、没有每件净重时，只记录件数，绝不换算成公斤。
    * 独立的“X 公斤/磅/斤”按原单位保留。
    """
    result = QuantityParse()
    consumed: list[tuple[int, int]] = []

    package_match = _PACKAGE_RE.search(text)
    if package_match:
        count_token = package_match.group("count")
        container = package_match.group("container")
        count = int(cn_to_decimal(count_token))
        per_token = package_match.group("per")
        unit_token = package_match.group("unit")
        result.packages = count
        result.container = container
        consumed.append(package_match.span())
        if per_token:
            low, high = parse_range(per_token)
            unit = unit_default_for_container(container, profile, unit_token)
            result.package_kg_low = low
            result.package_kg_high = high
            result.package_unit = unit
            result.per_package = True
            total_low = low * count
            total_high = high * count
            result.quantity = Quantity(total_low, total_high, unit, source_id)
            result.unit_hint = unit
            if low != high:
                result.notes.append(
                    f"{count}{container} × {low}–{high}，只按下界 {total_low} 计入，不取中点"
                )
            return result
        per_elsewhere = _PER_PACKAGE_RE.search(text)
        if per_elsewhere:
            per_low, per_high = parse_range(per_elsewhere.group("per"))
            unit = unit_default_for_container(
                per_elsewhere.group("container"), profile, per_elsewhere.group("unit")
            )
            result.package_kg_low = per_low
            result.package_kg_high = per_high
            result.package_unit = unit
            result.per_package = True
            result.quantity = Quantity(per_low * count, per_high * count, unit, source_id)
            result.unit_hint = unit
            if per_low != per_high:
                result.notes.append(
                    f"{count}{container} × {per_low}–{per_high}，只按下界"
                    f" {per_low * count} 计入，不取中点"
                )
            return result
        result.quantity = Quantity(
            Decimal(count), Decimal(count), Unit.CASE, source_id
        )
        result.unit_hint = Unit.CASE
        result.notes.append(f"仅知 {count}{container}，无每件净重，不能换算公斤")
        return result

    # “每袋十五磅，三袋”这种倒装：先找每件净重，再找件数。
    per_match = _PER_PACKAGE_RE.search(text)
    if per_match:
        per_low, per_high = parse_range(per_match.group("per"))
        unit = unit_default_for_container(
            per_match.group("container"), profile, per_match.group("unit")
        )
        count_match = re.search(
            rf"(?P<count>{NUM})\s*(?P<container>{'|'.join(CONTAINERS)})", text
        )
        count = int(cn_to_decimal(count_match.group("count"))) if count_match else None
        result.package_kg_low = per_low
        result.package_kg_high = per_high
        result.package_unit = unit
        result.per_package = True
        if count is not None:
            result.packages = count
            result.container = count_match.group("container")
            result.quantity = Quantity(per_low * count, per_high * count, unit, source_id)
            result.unit_hint = unit
            if per_low != per_high:
                result.notes.append(
                    f"{count}{result.container} × {per_low}–{per_high}，只按下界"
                    f" {per_low * count} 计入，不取中点"
                )
            return result
        result.quantity = Quantity(per_low, per_high, unit, source_id)
        result.unit_hint = unit
        return result

    # 没有件数，找独立重量。
    weight_match = _WEIGHT_RE.search(text)
    if weight_match:
        low, high = parse_range(weight_match.group("value"))
        unit = parse_unit(weight_match.group("unit"))
        result.quantity = Quantity(low, high, unit, source_id)
        result.unit_hint = unit
        consumed.append(weight_match.span())
        if low != high:
            result.notes.append(
                f"{low}–{high}{unit.value}，只按下界 {low} 计入，不取中点"
            )
        return result

    # 只有“N 筐/袋”但正则没匹配到的兜底。
    simple = re.search(rf"(?P<count>{NUM})\s*(?P<container>{'|'.join(CONTAINERS)})", text)
    if simple:
        count = int(cn_to_decimal(simple.group("count")))
        result.packages = count
        result.container = simple.group("container")
        result.quantity = Quantity(Decimal(count), Decimal(count), Unit.CASE, source_id)
        result.unit_hint = Unit.CASE
        consumed.append(simple.span())
    return result


def quantity_from_packages(
    count: int,
    per_low: Decimal,
    per_high: Decimal,
    unit: Unit,
    source_id: str,
) -> Quantity:
    return Quantity(per_low * count, per_high * count, unit, source_id)


# ---------------------------------------------------------------------------
# 时间、价格、份额
# ---------------------------------------------------------------------------

def parse_day(text: str) -> Optional[int]:
    """把相对到货时间折算成“今天=0”的天数。"""
    if not text:
        return None
    if "后天" in text:
        return 2
    if "明早" in text or "明天" in text or "次日" in text:
        return 1
    if "今晚" in text or "今天" in text or "当日" in text or "马上" in text:
        return 0
    return None


def parse_share(token: str) -> Optional[Decimal]:
    token = token.strip()
    if token in ("一半", "半"):
        return Decimal("0.5")
    if token.endswith("成"):
        return cn_to_decimal(token[:-1]) / Decimal("10")
    if token.endswith("%"):
        return cn_to_decimal(token[:-1]) / Decimal("100")
    if token.endswith("％"):
        return cn_to_decimal(token[:-1]) / Decimal("100")
    return None


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。；;\n]", text) if part.strip()]


def split_clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[，,。；;\n]", text) if part.strip()]


def clean_lot_marker(marker: str) -> str:
    return marker.strip().upper()


LOT_MARKER_RE = re.compile(r"(?<![A-Za-z0-9])(?P<id>[A-Z]\d{1,3})(?![A-Za-z0-9])")
RECORD_REF_RE = re.compile(r"(?<![A-Za-z0-9])(?P<id>[a-zA-Z]\d{1,3})(?![A-Za-z0-9])")


def find_lot_markers(text: str) -> list[tuple[str, int, int]]:
    return [(m.group("id"), m.start(), m.end()) for m in LOT_MARKER_RE.finditer(text)]


# ---------------------------------------------------------------------------
# 换算辅助
# ---------------------------------------------------------------------------

def kg_from_quantity(quantity: Optional[Quantity], unit: Optional[Unit] = None) -> Optional[Decimal]:
    if quantity is None:
        return None
    if quantity.unit is Unit.KG:
        return quantity.low
    if quantity.unit is Unit.LB:
        return (quantity.low * KG_PER_LB).quantize(Decimal("0.001"))
    if quantity.unit is Unit.JIN:
        return (quantity.low * KG_PER_JIN).quantize(Decimal("0.001"))
    return None


def describe_unit(unit: Unit) -> str:
    return {
        Unit.KG: "实秤公斤",
        Unit.LB: "磅（袋装）",
        Unit.JIN: "市斤",
        Unit.CASE: "筐/袋（可数）",
    }[unit]
