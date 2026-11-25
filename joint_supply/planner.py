"""把结构化记录规划成“当班可以一起兑现的承诺”。

规划器遵循这些硬规则：

1. 装车回单里已签收、且能核出净重的货，按回单锁定给对应场；已装部分不能
   因为某场“愿意让量”而改划别场。
2. 区间数量只按下界计入；未过秤的装车行只记录“已装”，不产生交割重量。
3. 供方更正明确指回同一批实物时，被指回的那条只作别名释放，不重复计供给。
4. 暂留批次不参与第一轮组合；某场仍然缺量时，单独做第二轮搜索，把它们
   移出暂留寻找能共存的组合，但仍保留“指代未确认”这一条件。

多个场同时需要同一批货时，规划器做一次小的全局搜索，优先让更多场达到
订单下限，再比较覆盖量、已确认量、替代用量与条件数量；无法两全的会在
conflicts 中写成“调给谁、另一场会缺多少”的明确选项。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from . import lexicon
from .lexicon import display_product
from .model import (
    Allocation,
    Condition,
    LoadInstruction,
    MarketOrder,
    MarketPlan,
    Plan,
    QuoteLot,
    Role,
    TOPIC_ARRIVAL,
    TOPIC_NON_GUARANTEE,
    TOPIC_PRICE,
    TOPIC_PRODUCT,
    TOPIC_REFERENCE,
    TOPIC_SUBSTITUTE,
    TOPIC_WEIGHING,
    Verdict,
    fmt_decimal,
)
from .parser import Parsed, describe_statement

ZERO = Decimal("0")
EPS = Decimal("0.001")
MAX_SEARCH_NODES = 250_000


@dataclass
class Delivery:
    lot_id: str
    market: str
    kg_low: Decimal
    kg_high: Decimal
    packages: Optional[int]
    weighed: bool
    sealed: bool
    sources: tuple[str, ...]
    text: str


@dataclass
class LotView:
    """规划时对一批货的可用量视图。"""

    lot: QuoteLot
    low_kg: Decimal
    high_kg: Decimal
    packages: Optional[int] = None
    package_kg_low: Optional[Decimal] = None
    package_kg_high: Optional[Decimal] = None
    delivered_kg: Decimal = ZERO
    delivered_packages: int = 0

    @property
    def left_packages(self) -> Optional[int]:
        leftover = self.lot.attributes.get("post_load_remaining_packages")
        if isinstance(leftover, int):
            return max(leftover, 0)
        if self.packages is None:
            return None
        return max(self.packages - self.delivered_packages, 0)

    @property
    def remaining_low(self) -> Decimal:
        leftover = self.lot.attributes.get("post_load_remaining_packages")
        if (
            isinstance(leftover, int)
            and self.package_kg_low is not None
            and self.packages is not None
        ):
            converted = lexicon.to_kg(leftover * self.package_kg_low, self.lot.package_unit)
            if converted is not None:
                return converted
        return max(self.low_kg - self.delivered_kg, ZERO)

    @property
    def remaining_high(self) -> Decimal:
        return max(max(self.high_kg - self.delivered_kg, ZERO), self.remaining_low)

    @property
    def remaining_packages(self) -> Optional[int]:
        return self.left_packages

    @property
    def unit_weight(self) -> Optional[Decimal]:
        """整件批次的每件净重下界（公斤）。不能换成公斤时返回 None。"""
        if self.package_kg_low is None:
            return None
        return lexicon.to_kg(self.package_kg_low, self.lot.package_unit)

    @property
    def discrete(self) -> bool:
        return self.remaining_packages is not None and self.unit_weight is not None


@dataclass
class Option:
    """某批货可以给某场的一种用法。"""

    lot_id: str
    market: str
    role: str          # primary / substitute / tentative / related
    kg: Decimal
    packages: Optional[int]
    conditions: list[Condition] = field(default_factory=list)

    @property
    def blocking(self) -> list[Condition]:
        return [condition for condition in self.conditions if condition.open and condition.blocking]


@dataclass
class Chunk:
    lot_id: str
    market: str
    role: str
    kg: Decimal
    packages: Optional[int]
    conditions: list[Condition]


def plan(parsed: Parsed) -> Plan:
    return Planner(parsed).build()


class Planner:
    def __init__(self, parsed: Parsed):
        self.parsed = parsed
        self.plan = Plan(
            lots=parsed.lots,
            profiles=parsed.profiles,
            records=parsed.records_by_id(),
            loads=parsed.loads,
            record_count=len(parsed.records),
            ignored_duplicates=parsed.ignored_duplicates,
        )
        self.views: dict[str, LotView] = {
            lot_id: self._view_for(lot_id, lot) for lot_id, lot in parsed.lots.items()
        }
        # committed = 已装（不能改划）的公斤数；weighed_locked = 其中已过秤的部分。
        self.locked: dict[str, Decimal] = {}
        self.weighed_locked: dict[str, Decimal] = {}
        self.locked_allocations: dict[str, list[Allocation]] = {}

    # ------------------------------------------------------------------
    # 交割与视图
    # ------------------------------------------------------------------

    def _view_for(self, lot_id: str, lot: QuoteLot) -> LotView:
        view = LotView(
            lot=lot,
            low_kg=lot.low_kg,
            high_kg=lot.high_kg,
            packages=lot.packages,
            package_kg_low=lot.package_kg_low,
            package_kg_high=lot.package_kg_high,
        )
        for line in self.parsed.loads:
            if line.lot_id != lot_id:
                continue
            if line.loaded:
                view.delivered_packages += line.packages or 0
            if line.delivered and line.quantity is not None:
                view.delivered_kg += line.quantity.certain_low_kg
        return view

    def _deliveries(self) -> list[Delivery]:
        deliveries: list[Delivery] = []
        for line in self.parsed.loads:
            if not line.lot_id or not line.delivered or line.quantity is None:
                continue
            deliveries.append(
                Delivery(
                    lot_id=line.lot_id,
                    market=line.market or "",
                    kg_low=line.quantity.certain_low_kg,
                    kg_high=line.quantity.certain_high_kg,
                    packages=line.packages,
                    weighed=line.weighed,
                    sealed=line.sealed,
                    sources=(line.record_id,),
                    text=line.text,
                )
            )
        return deliveries

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def build(self) -> Plan:
        self.plan.metadata = {"resolved_references": self.parsed.resolved_references}
        orders = self._ordered_orders()
        self.locked = {order.market: ZERO for order in orders}
        self.weighed_locked = {order.market: ZERO for order in orders}
        self.locked_allocations = {order.market: [] for order in orders}
        self._lock_deliveries(orders)

        candidates = {
            lot_id: view
            for lot_id, view in self.views.items()
            if view.lot.available and not view.lot.tentative
        }
        assignments = self._solve(orders, candidates, include_tentative=False)
        assignments = self._second_pass(orders, candidates, assignments)

        self._materialize(orders, assignments)
        self._plan_instructions(orders, assignments)
        self._report_conflicts(orders, assignments)

        self.plan.issues.extend(self.parsed.issues)
        self.plan.released = [
            lot.id for lot in self.parsed.lots.values() if lot.released or lot.duplicate_of
        ]
        self.plan.metadata.update(
            {
                "rules": {
                    "range_quantities": "区间数量只按下界计入，不取中点",
                    "unweighed_loads": "未过秤的装车行不计入已交割，只记录已装事实",
                    "tentative_lots": "指代未确认的批次只作暂留；有更正后释放，缺量时再单独搜索",
                    "locked_loads": "已装货按回单锁定到场承诺，不能改划别场；未过秤部分不计交割",
                    "substitution": "替代只按订单允许的品种、份额和价差上限使用，规格不一致需买方确认",
                    "exclusions": "采购“不要…外”是只接受，供方“不保证”是风险提示，二者不互换",
                    "same_word": "“青芯”等词按场的名称表区分，缺场名时不猜",
                    "corrections": "行内补正只改指向，不新增承诺；订单确认只关闭条件，不创造供给",
                },
            }
        )
        return self.plan

    def _ordered_orders(self) -> list[MarketOrder]:
        def key(order: MarketOrder):
            hard = 0
            if order.deadline_day is not None and order.deadline_day <= 0:
                hard -= 2
            if not order.allow_substitute:
                hard -= 1
            return (hard, order.market)

        return sorted(self.parsed.orders.values(), key=key)

    def _lock_deliveries(self, orders: list[MarketOrder]) -> None:
        """已装货锁定到场承诺；未过秤的部分只锁承诺、不算已交割。"""
        by_market = {order.market: order for order in orders}
        for item in self._deliveries():
            if item.market not in by_market:
                continue
            self.locked[item.market] += item.kg_low
            conditions: list[Condition] = []
            if item.weighed:
                self.weighed_locked[item.market] += item.kg_low
                note = (
                    "装车回单已签收" + ("并封箱" if item.sealed else "")
                    + "，按到场承诺锁定，不能改划别场"
                )
            else:
                note = (
                    "装车回单已装但未过秤；已装部分照旧承担到场承诺、不能卸下重配，"
                    "但未过秤重量不算已交割"
                )
                conditions.append(
                    Condition(
                        TOPIC_WEIGHING,
                        f"{item.lot_id} 已装车但回单没有过秤净重，"
                        "需车队实秤后才能算作已交割。",
                        Role.CARRIER,
                        item.sources[0] if item.sources else "",
                    )
                )
            self.locked_allocations[item.market].append(
                Allocation(
                    lot_id=item.lot_id,
                    market=item.market,
                    kg_low=item.kg_low,
                    kg_high=item.kg_high,
                    packages=item.packages,
                    role="locked",
                    conditions=conditions,
                    sources=item.sources,
                    note=note,
                )
            )

    def _need(self, order: MarketOrder) -> Decimal:
        return max((order.min_kg or ZERO) - self.locked.get(order.market, ZERO), ZERO)

    # ------------------------------------------------------------------
    # 候选与条件
    # ------------------------------------------------------------------

    def _options_for(
        self, order: MarketOrder, view: LotView, *, tentative: bool
    ) -> list[Option]:
        lot = view.lot
        if not lot.available:
            return []
        if lot.tentative and not tentative:
            return []
        if view.remaining_packages == 0:
            return []
        if view.remaining_low <= 0 and lot.packages is None:
            return []
        if self._arrival_short(order, lot):
            return []
        if self._exclusion_reason(order, lot):
            return []

        rule = order.allowed_substitute if order.allow_substitute else None
        substitute_keys = set(rule.substitute_terms) if rule else set()

        if order.product and lot.product == order.product:
            return [self._make_option(view, order, "primary", tentative=tentative)]
        if rule is not None:
            relation = self._substitute_relation(lot, substitute_keys)
            if relation is not None:
                return [
                    self._substitute_option(
                        view, order, relation, tentative=tentative
                    )
                ]
        if order.product is None and lot.market_hint == order.market:
            return [self._make_option(view, order, "primary", tentative=tentative)]
        return []

    def _substitute_relation(self, lot: QuoteLot, substitute_keys: set[str]) -> Optional[str]:
        """返回 exact / related / unknown；不能确认的交给条件，不静默当成同规格。"""
        if lot.product in substitute_keys:
            return "exact"
        if not substitute_keys:
            return None
        if lot.product is None:
            if lot.attributes:
                return "unknown"
            return None
        target = next(iter(substitute_keys))
        target_spec = lexicon.product_spec(target)
        lot_spec = lexicon.product_spec(lot.product)
        if target_spec is None or lot_spec is None:
            return None
        target_attrs = dict(attr.split(":", 1) for attr in target_spec.attributes)
        lot_attrs = dict(attr.split(":", 1) for attr in lot_spec.attributes)
        # 同一茎型、根型不同的货，可能可以作为替代，但必须由买方确认。
        if target_attrs.get("stem") and target_attrs.get("stem") == lot_attrs.get("stem"):
            return "related"
        return None

    def _substitute_option(
        self,
        view: LotView,
        order: MarketOrder,
        relation: str,
        *,
        tentative: bool,
    ) -> Option:
        lot = view.lot
        rule = order.allowed_substitute
        assert rule is not None
        kg = view.remaining_low
        packages = view.remaining_packages
        if view.discrete and view.unit_weight:
            unit = view.unit_weight
            cap = (
                (order.min_kg or ZERO) * rule.max_share
                if rule.max_share is not None
                else None
            )
            available = Decimal(packages or 0)
            full = (
                min(available, (cap / unit).to_integral_value(rounding="ROUND_FLOOR"))
                if cap is not None
                else available
            )
            if full <= 0 and cap is not None and cap > 0:
                # 替代上限装不下一个整件：只能按上限拆件，且必须让买方明确认可拆筐。
                option = self._make_option(
                    view,
                    order,
                    "substitute",
                    tentative=tentative,
                    kg_override=cap,
                    packages_override=None,
                )
                option.conditions.append(
                    Condition(
                        TOPIC_SUBSTITUTE,
                        f"{lot.id} 每件 {fmt_decimal(unit)}kg，超过{order.market}的替代上限"
                        f" {fmt_decimal(cap)}kg；只能拆件按 {fmt_decimal(cap)}kg 抵，"
                        "需采购确认允许拆筐并接受拆件后的剩余。",
                        Role.BUYER,
                        lot.record_id,
                    )
                )
                option.conditions.extend(self._substitute_common_conditions(lot, order))
                return option
            if full <= 0:
                return Option(lot.id, order.market, "substitute", ZERO, 0, [])
            kg = unit * full
            packages = int(full)

        option = self._make_option(
            view,
            order,
            "substitute",
            tentative=tentative,
            kg_override=kg,
            packages_override=packages,
        )
        conditions = option.conditions
        conditions.extend(self._substitute_common_conditions(lot, order))
        if relation == "related":
            conditions.append(
                Condition(
                    TOPIC_PRODUCT,
                    f"{lot.id} 的规格为{display_product(lot.product)}，与订单允许的替代"
                    f"（{display_product(next(iter(rule.substitute_terms)))}）不完全一致，"
                    f"需{order.market}明确是否接受；不能只按“白梗”这个同词就当成低根。",
                    Role.BUYER,
                    lot.record_id,
                )
            )
        elif relation == "unknown":
            conditions.append(
                Condition(
                    TOPIC_PRODUCT,
                    f"{lot.id} 的品种无法按名称表确认，能否作为{order.market}的替代"
                    "需采购和供方一起确认。",
                    Role.BUYER,
                    lot.record_id,
                )
            )
        return option

    def _substitute_common_conditions(
        self, lot: QuoteLot, order: MarketOrder
    ) -> list[Condition]:
        rule = order.allowed_substitute
        assert rule is not None
        conditions = [
            Condition(
                TOPIC_SUBSTITUTE,
                f"{lot.id} 作为替代必须由{order.market}在订单确认里承认实际到货构成；"
                f"替代份额不超过 {_pct(rule.max_share)}，"
                f"价差不超过每公斤 {_fmt_money(rule.max_price_gap)} 元。",
                Role.BUYER,
                lot.record_id,
                blocking=False,
            )
        ]
        if rule.max_price_gap is not None and lot.unit_price is None:
            conditions.append(
                Condition(
                    TOPIC_PRICE,
                    f"{lot.id} 未报单价，无法核对价差是否在每公斤"
                    f" {fmt_decimal(rule.max_price_gap)} 元内；"
                    "需供方报价确认后才能把它算作替代。",
                    Role.SUPPLIER,
                    lot.record_id,
                )
            )
        return conditions

    def _make_option(
        self,
        view: LotView,
        order: MarketOrder,
        role: str,
        *,
        tentative: bool,
        kg_override: Optional[Decimal] = None,
        packages_override: Optional[int] = None,
    ) -> Option:
        lot = view.lot
        kg = view.remaining_low if kg_override is None else kg_override
        packages = view.remaining_packages if packages_override is None else packages_override
        conditions = self._route_conditions(view, order, role)
        if tentative:
            seen_topics = {
                condition.topic
                for condition in conditions
                if condition.open and condition.blocking
            }
            if TOPIC_REFERENCE not in seen_topics:
                conditions.append(
                    Condition(
                        TOPIC_REFERENCE,
                        f"{lot.id} 原为暂留，本次为补{order.market}缺量移出，"
                        "需供方书面确认指向后才算供给。",
                        Role.SUPPLIER,
                        lot.record_id,
                    )
                )
        return Option(
            lot_id=lot.id,
            market=order.market,
            role="tentative" if tentative else role,
            kg=kg,
            packages=packages,
            conditions=conditions,
        )

    def _route_conditions(
        self, view: LotView, order: MarketOrder, role: str
    ) -> list[Condition]:
        lot = view.lot
        conditions = [condition for condition in lot.conditions if condition.open]
        if lot.arrival_day is None and order.deadline_day == 0:
            conditions.append(
                Condition(
                    TOPIC_ARRIVAL,
                    f"报价未写明到场时间，{order.market}要求今晚到，需供方确认。",
                    Role.SUPPLIER,
                    lot.record_id,
                    blocking=False,
                )
            )
        if lot.market_hint and lot.market_hint != order.market:
            conditions.append(
                Condition(
                    TOPIC_PRODUCT,
                    f"{lot.id} 原报价指向{lot.market_hint}，调给{order.market}"
                    "需供方确认品种与数量不变。",
                    Role.SUPPLIER,
                    lot.record_id,
                )
            )
        return conditions

    def _arrival_ok(self, order: MarketOrder, lot: QuoteLot) -> bool:
        if order.deadline_day is None or lot.arrival_day is None:
            return True
        return lot.arrival_day <= order.deadline_day

    def _arrival_short(self, order: MarketOrder, lot: QuoteLot) -> bool:
        return not self._arrival_ok(order, lot)

    def _arrival_reason(self, order: MarketOrder, lot: QuoteLot) -> str:
        arrival = lot.arrival_text or f"第{lot.arrival_day}天"
        deadline = order.deadline_text or "本场窗口"
        return f"到场时间为{arrival}，晚于{order.market}的{deadline}"

    def _exclusion_reason(self, order: MarketOrder, lot: QuoteLot) -> str:
        for term in order.excluded_terms:
            if term and term in lot.text:
                return f"命中订单排除词“{term}”（采购“不要{term}”）"
        for term in order.only_accept_terms:
            if term and not _lot_has_term(lot, term):
                return (
                    f"订单只接受“{term}”类（采购“不要{term}外的”），"
                    "该批没有对应标记"
                )
        return ""

    def _mismatch_reason(self, order: MarketOrder, lot: QuoteLot) -> str:
        if not order.allow_substitute:
            return "订单明确不换品，且不是本场指定品种"
        if order.product and lot.product:
            return (
                f"品种为{display_product(lot.product)}，既非本场指定品种"
                f"（{display_product(order.product)}），也不在允许替代清单"
            )
        if lot.product is None:
            return "品种无法从文字和名称表确认"
        if lot.market_hint and lot.market_hint != order.market:
            return f"报价指向{lot.market_hint}，且无可用替代关系"
        return "无法确认品种与替代关系"

    # ------------------------------------------------------------------
    # 全局搜索
    # ------------------------------------------------------------------

    def _solve(
        self,
        orders: list[MarketOrder],
        candidates: dict[str, LotView],
        *,
        include_tentative: bool,
    ) -> list[Chunk]:
        """为“哪批给哪场”找一组尽量能共存的承诺。

        * 批次不拆给多个场；替代超过份额上限时只取上限。
        * 少量批次时穷举（有上界剪枝），批次多时用贪心 + 局部改进，
          保证在当班能秒级给出结果，并在 metadata 里标明用了哪种算法。
        """
        lot_ids = sorted(candidates)
        options_by_lot: dict[str, list[Option]] = {}
        for lot_id in lot_ids:
            view = candidates[lot_id]
            options: list[Option] = [Option(lot_id, "", "skip", ZERO, None)]
            for order in orders:
                for option in self._options_for(
                    order, view, tentative=include_tentative and view.lot.tentative
                ):
                    if option.kg > 0:
                        options.append(option)
            options_by_lot[lot_id] = options

        needs = {order.market: self._need(order) for order in orders}
        caps = {
            order.market: (
                (order.min_kg or ZERO) * order.allowed_substitute.max_share
                if order.allowed_substitute
                and order.allowed_substitute.max_share is not None
                else Decimal("999999")
            )
            for order in orders
        }

        if len(lot_ids) <= 8:
            self.plan.metadata["search"] = "exhaustive"
            return self._exhaustive(
                orders, lot_ids, options_by_lot, needs, caps, include_tentative
            )

        self.plan.metadata["search"] = "greedy+local"
        return self._greedy_local(
            orders, lot_ids, options_by_lot, needs, caps, include_tentative
        )

    # -- 精确搜索（小规模） ------------------------------------------------

    def _exhaustive(
        self,
        orders: list[MarketOrder],
        lot_ids: list[str],
        options_by_lot: dict[str, list[Option]],
        needs: dict[str, Decimal],
        caps: dict[str, Decimal],
        include_tentative: bool,
    ) -> list[Chunk]:
        best_score: Optional[tuple] = None
        best: list[Chunk] = []
        caps_remaining = {order.market: caps[order.market] for order in orders}

        def recurse(index: int, chunks: list[Chunk], used: dict[str, Decimal]) -> None:
            nonlocal best_score, best
            if index == len(lot_ids):
                score = self._score(chunks, needs, orders)
                if best_score is None or score > best_score:
                    best_score = score
                    best = list(chunks)
                return
            lot_id = lot_ids[index]
            for option in options_by_lot[lot_id]:
                if option.role == "skip":
                    recurse(index + 1, chunks, used)
                    continue
                kg = option.kg
                if option.role == "substitute":
                    remaining_cap = caps[option.market] - used.get(option.market, ZERO)
                    if remaining_cap <= 0:
                        continue
                    kg = min(kg, remaining_cap)
                    if kg <= 0:
                        continue
                chunk = Chunk(
                    lot_id=option.lot_id,
                    market=option.market,
                    role=option.role,
                    kg=kg,
                    packages=option.packages,
                    conditions=option.conditions,
                )
                if option.role == "substitute":
                    used[option.market] = used.get(option.market, ZERO) + kg
                chunks.append(chunk)
                recurse(index + 1, chunks, used)
                chunks.pop()
                if option.role == "substitute":
                    used[option.market] = used.get(option.market, ZERO) - kg
            recurse(index + 1, chunks, used)

        recurse(0, [], {order.market: ZERO for order in orders})
        return best

    # -- 贪心 + 局部改进（批次数多时） -------------------------------------

    def _greedy_local(
        self,
        orders: list[MarketOrder],
        lot_ids: list[str],
        options_by_lot: dict[str, list[Option]],
        needs: dict[str, Decimal],
        caps: dict[str, Decimal],
        include_tentative: bool,
    ) -> list[Chunk]:
        # 大件先分配，避免小件先占满场次需求。
        ordered = sorted(
            lot_ids,
            key=lambda lot_id: -float(
                max(
                    (option.kg for option in options_by_lot[lot_id] if option.role != "skip"),
                    default=ZERO,
                )
            ),
        )
        assignment: dict[str, Optional[Option]] = {}
        for lot_id in ordered:
            best_option: Optional[Option] = None
            best_score = self._selection_score(
                assignment, lot_id, None, options_by_lot, orders, needs, caps
            )
            for option in options_by_lot[lot_id]:
                score = self._selection_score(
                    assignment, lot_id, option, options_by_lot, orders, needs, caps
                )
                if score > best_score:
                    best_score = score
                    best_option = option
            assignment[lot_id] = best_option

        # 局部改进：逐批尝试换场/换角色/不发，得分提高就接受。
        improved = True
        rounds = 0
        while improved and rounds < 30:
            improved = False
            rounds += 1
            for lot_id in ordered:
                current = assignment.get(lot_id)
                current_score = self._selection_score(
                    assignment, lot_id, current, options_by_lot, orders, needs, caps
                )
                for option in options_by_lot[lot_id]:
                    if option is current:
                        continue
                    score = self._selection_score(
                        assignment, lot_id, option, options_by_lot, orders, needs, caps
                    )
                    if score > current_score:
                        assignment[lot_id] = option
                        current_score = score
                        improved = True

        return self._chunks_from(
            assignment, lot_ids, options_by_lot, caps, orders
        )

    def _selection_score(
        self,
        assignment: dict[str, Optional[Option]],
        lot_id: str,
        option: Optional[Option],
        options_by_lot: dict[str, list[Option]],
        orders: list[MarketOrder],
        needs: dict[str, Decimal],
        caps: dict[str, Decimal],
    ) -> tuple:
        trial = dict(assignment)
        trial[lot_id] = option
        chunks = self._chunks_from(
            trial, list(trial), options_by_lot, caps, orders
        )
        return self._score(chunks, needs, orders)

    def _chunks_from(
        self,
        assignment: dict[str, Optional[Option]],
        lot_ids: list[str],
        options_by_lot: dict[str, list[Option]],
        caps: dict[str, Decimal],
        orders: list[MarketOrder],
    ) -> list[Chunk]:
        used: dict[str, Decimal] = {order.market: ZERO for order in orders}
        chunks: list[Chunk] = []
        for lot_id in lot_ids:
            option = assignment.get(lot_id)
            if option is None or option.role == "skip":
                continue
            kg = option.kg
            if option.role == "substitute":
                remaining = caps[option.market] - used[option.market]
                if remaining <= 0:
                    continue
                kg = min(kg, remaining)
                if kg <= 0:
                    continue
                used[option.market] += kg
            chunks.append(
                Chunk(
                    lot_id=option.lot_id,
                    market=option.market,
                    role=option.role,
                    kg=kg,
                    packages=option.packages,
                    conditions=option.conditions,
                )
            )
        return chunks

    def _score(
        self,
        chunks: list[Chunk],
        needs: dict[str, Decimal],
        orders: list[MarketOrder],
    ) -> tuple:
        covered = {order.market: ZERO for order in orders}
        confirmed = {order.market: ZERO for order in orders}
        for chunk in chunks:
            covered[chunk.market] += chunk.kg
            if not any(c.open and c.blocking for c in chunk.conditions):
                confirmed[chunk.market] += chunk.kg

        fully = 0
        capped_total = ZERO
        confirmed_total = ZERO
        substitute_kg = ZERO
        condition_count = 0
        for order in orders:
            market = order.market
            need = order.min_kg or ZERO
            total = self.locked.get(market, ZERO) + covered[market]
            if need and total + EPS >= need:
                fully += 1
            capped_total += min(total, need) if need else ZERO
            confirmed_total += self.locked.get(market, ZERO) + confirmed[market]
        for chunk in chunks:
            if chunk.role == "substitute":
                substitute_kg += chunk.kg
            condition_count += len([c for c in chunk.conditions if c.open and c.blocking])
        return (
            fully,
            capped_total,
            confirmed_total,
            -substitute_kg,
            sum(covered.values()),
            -condition_count,
            -len(chunks),
        )

    def _second_pass(
        self,
        orders: list[MarketOrder],
        candidates: dict[str, LotView],
        assignments: list[Chunk],
    ) -> list[Chunk]:
        """对仍然缺量的场，把暂留批次移出，重新寻找能共存的组合。"""
        tentative_views = {
            lot_id: view
            for lot_id, view in self.views.items()
            if view.lot.tentative
            and view.lot.available
            and not view.lot.attributes.get("inquiry")
        }
        if not tentative_views:
            return assignments
        current = self._score_for(assignments, orders)
        combined = dict(candidates)
        combined.update(tentative_views)
        trial = self._solve(orders, combined, include_tentative=True)
        trial_score = self._score_for(trial, orders)
        if trial_score > current:
            self.plan.metadata["tentative_promoted"] = sorted(
                {
                    chunk.lot_id
                    for chunk in trial
                    if self.views[chunk.lot_id].lot.tentative
                }
            )
            return trial
        return assignments

    def _score_for(self, assignments: list[Chunk], orders: list[MarketOrder]) -> tuple:
        needs = {order.market: self._need(order) for order in orders}
        return self._score(assignments, needs, orders)

    # ------------------------------------------------------------------
    # 物化
    # ------------------------------------------------------------------

    def _materialize(self, orders: list[MarketOrder], assignments: list[Chunk]) -> None:
        for order in orders:
            market_plan = MarketPlan(
                market=order.market,
                order=order,
                verdict=Verdict.BLOCKED,
                reason="",
            )
            market_plan.sources = (order.record_id,)
            market_plan.locked_kg = self.locked.get(order.market, ZERO)
            market_plan.delivered_kg = self.weighed_locked.get(order.market, ZERO)
            market_plan.allocations.extend(self.locked_allocations.get(order.market, []))
            market_plan.confirmed_kg = market_plan.delivered_kg
            for allocation in self.locked_allocations.get(order.market, []):
                if allocation.conditional:
                    market_plan.conditional_kg += allocation.kg_low

            if order.min_kg and market_plan.delivered_kg > order.min_kg:
                market_plan.alternatives.append(
                    f"已签收 {fmt_decimal(market_plan.delivered_kg)}kg 已超过订单下限"
                    f" {fmt_decimal(order.min_kg)}kg；超出部分仍归本场，"
                    "不能自行转给别场。"
                )

            for chunk in assignments:
                if chunk.market != order.market:
                    continue
                view = self.views[chunk.lot_id]
                lot = view.lot
                allocation = Allocation(
                    lot_id=chunk.lot_id,
                    market=order.market,
                    kg_low=chunk.kg,
                    kg_high=_high_for(view, chunk.kg),
                    packages=chunk.packages,
                    role=chunk.role,
                    tentative=lot.tentative,
                    conditions=_dedupe_conditions(chunk.conditions),
                    sources=(lot.record_id,),
                    note=lot.note,
                )
                market_plan.allocations.append(allocation)
                if allocation.conditional:
                    market_plan.conditional_kg += chunk.kg
                else:
                    market_plan.confirmed_kg += chunk.kg
                if chunk.role == "substitute":
                    market_plan.alternatives.append(
                        f"使用 {chunk.lot_id} {fmt_decimal(chunk.kg)}kg 作为替代，"
                        f"占订单下限 {_share_pct(order, chunk.kg)}，"
                        f"价差上限每公斤 {_fmt_money(order.allowed_substitute.max_price_gap)} 元"
                    )
                if chunk.role == "tentative":
                    market_plan.alternatives.append(
                        f"已把暂留批次 {chunk.lot_id} 移出，仅在供方确认指代后可补"
                        f"{order.market} 的 {fmt_decimal(chunk.kg)}kg 缺量；"
                        "未被确认前不计入已确认。"
                    )

            market_plan.allocated_low_kg = sum(
                (allocation.kg_low for allocation in market_plan.allocations), ZERO
            )
            market_plan.allocated_high_kg = sum(
                (allocation.kg_high for allocation in market_plan.allocations), ZERO
            )
            self._finalize_market(market_plan, order)
            self._market_conditions(market_plan, order)
            self.plan.market_plans[order.market] = market_plan

        self._record_excluded(orders, assignments)

    def _record_excluded(self, orders: list[MarketOrder], assignments: list[Chunk]) -> None:
        assigned = {(chunk.lot_id, chunk.market) for chunk in assignments}
        holders = {chunk.lot_id: chunk.market for chunk in assignments}
        for order in orders:
            market_plan = self.plan.market_plans[order.market]
            for lot_id, view in self.views.items():
                lot = view.lot
                if (lot_id, order.market) in assigned:
                    continue
                if not lot.available:
                    market_plan.alternatives.append(
                        f"{lot_id} 不可用于{order.market}：已被更正释放或重复导入，"
                        "不重复计供给"
                    )
                    continue
                if lot.tentative:
                    market_plan.alternatives.append(
                        f"{lot_id} 不可用于{order.market}：指代未确认，只作暂留"
                    )
                    continue
                if self._arrival_short(order, lot):
                    market_plan.alternatives.append(
                        f"{lot_id} 不可用于{order.market}：{self._arrival_reason(order, lot)}"
                    )
                    continue
                exclusion = self._exclusion_reason(order, lot)
                if exclusion:
                    market_plan.alternatives.append(
                        f"{lot_id} 不可用于{order.market}：{exclusion}"
                    )
                    continue
                if lot_id in holders:
                    owner = holders[lot_id]
                    locked = any(
                        allocation.lot_id == lot_id and allocation.role == "locked"
                        for allocation in self.plan.market_plans[owner].allocations
                    )
                    prefix = "已锁定给" if locked else "已分配给"
                    market_plan.alternatives.append(
                        f"{lot_id} {prefix}{owner}，不能再给{order.market}"
                    )
                    continue
                market_plan.alternatives.append(
                    f"{lot_id} 不可用于{order.market}：{self._mismatch_reason(order, lot)}"
                )

    def _finalize_market(self, market_plan: MarketPlan, order: MarketOrder) -> None:
        need = order.min_kg or ZERO
        confirmed = market_plan.confirmed_kg
        conditional = market_plan.conditional_kg
        total = market_plan.allocated_low_kg

        if (
            need
            and market_plan.delivered_kg + EPS >= need
            and total <= market_plan.delivered_kg + EPS
        ):
            market_plan.verdict = Verdict.DELIVERED
            market_plan.reason = (
                f"已过秤签收 {fmt_decimal(market_plan.delivered_kg)}kg，达到订单下限"
                f" {fmt_decimal(need)}kg，本轮已交割。"
            )
            market_plan.shortage_kg = ZERO
        elif confirmed + EPS >= need:
            market_plan.verdict = Verdict.CONFIRMED
            market_plan.reason = (
                f"已确认 {fmt_decimal(confirmed)}kg（含已过秤签收"
                f" {fmt_decimal(market_plan.delivered_kg)}kg），达到订单下限"
                f" {fmt_decimal(need)}kg，可以兑现。"
            )
            if conditional:
                market_plan.reason += (
                    f" 另有 {fmt_decimal(conditional)}kg 待确认，属增量，不影响本单下限。"
                )
            market_plan.shortage_kg = ZERO
        elif confirmed + conditional + EPS >= need:
            market_plan.verdict = Verdict.CONDITIONAL
            market_plan.reason = (
                f"已确认 {fmt_decimal(confirmed)}kg，距下限 {fmt_decimal(need)}kg"
                f" 还差 {fmt_decimal(need - confirmed)}kg；"
                f"可用的有条件货 {fmt_decimal(conditional)}kg，条件满足后才能承诺。"
            )
            market_plan.shortage_kg = ZERO
        else:
            covered = confirmed + conditional
            market_plan.shortage_kg = max(need - covered, ZERO)
            market_plan.verdict = Verdict.BLOCKED
            market_plan.reason = (
                f"已确认 {fmt_decimal(confirmed)}kg、有条件可覆盖"
                f" {fmt_decimal(conditional)}kg，合计 {fmt_decimal(covered)}kg，"
                f"仍缺 {fmt_decimal(market_plan.shortage_kg)}kg，不能承诺足量。"
            )

        if order.case_tolerance and order.min_kg:
            market_plan.alternatives.append(
                f"订单允许少 {order.case_tolerance} 筐，但净重底线仍是"
                f" {fmt_decimal(order.min_kg)}kg；让筐不等于让净重，"
                "已确认净重不能改划别场。"
            )

    def _market_conditions(self, market_plan: MarketPlan, order: MarketOrder) -> None:
        seen: set[tuple[str, str, bool]] = set()
        for allocation in market_plan.allocations:
            for condition in allocation.conditions:
                if not condition.open:
                    continue
                key = (condition.topic, condition.source_id, condition.blocking)
                if key in seen:
                    continue
                seen.add(key)
                if condition.blocking:
                    market_plan.conditions.append(condition)
                else:
                    market_plan.advisory_conditions.append(condition)

        for condition in order.conditions:
            if condition.open:
                market_plan.advisory_conditions.append(condition)

        if order.reference_quote:
            resolved = any(
                item["record_id"] == order.record_id
                for item in self.parsed.resolved_references
            )
            if not resolved:
                market_plan.advisory_conditions.append(
                    Condition(
                        TOPIC_REFERENCE,
                        f"订单引用“{order.reference_note}”，但本次导入里没有唯一对应批次，"
                        "需供方确认指的是哪次报价。",
                        Role.SUPPLIER,
                        order.record_id,
                        blocking=False,
                    )
                )

        for lot in self.parsed.lots.values():
            for condition in lot.conditions:
                if condition.topic == TOPIC_NON_GUARANTEE and condition.open:
                    market_plan.advisory_conditions.append(condition)

    # ------------------------------------------------------------------
    # 冲突报告
    # ------------------------------------------------------------------

    def _report_conflicts(
        self, orders: list[MarketOrder], assignments: list[Chunk]
    ) -> None:
        """同一批货可能被两个场争用时，写清两种选择的后果。"""
        for order in orders:
            for lot_id, view in self.views.items():
                lot = view.lot
                if not lot.available:
                    continue
                if lot_id in self.locked_lots():
                    # 已装货不能改划，谈不上“争用”，不给取舍建议。
                    continue
                holders = [chunk for chunk in assignments if chunk.lot_id == lot_id]
                if not holders or all(chunk.market == order.market for chunk in holders):
                    continue
                options = self._options_for(
                    order, view, tentative=lot.tentative
                )
                if not options:
                    continue
                gain = min(options[0].kg, self._need(order))
                if gain <= 0:
                    continue
                holder = holders[0]
                holder_plan = self.plan.market_plans.get(holder.market)
                holder_need = (
                    holder_plan.order.min_kg
                    if holder_plan is not None and holder_plan.order is not None
                    else ZERO
                )
                holder_total = self.locked.get(holder.market, ZERO) + sum(
                    (chunk.kg for chunk in assignments if chunk.market == holder.market),
                    ZERO,
                )
                loss = min(holder.kg, max(holder_total - holder_need, ZERO)) or holder.kg
                self.plan.conflicts.append(
                    {
                        "lot_id": lot_id,
                        "allocated_to": holder.market,
                        "contender": order.market,
                        "message": (
                            f"{lot_id} 已给 {holder.market}（{fmt_decimal(holder.kg)}kg）。"
                            f"若改给 {order.market}，可补约 {fmt_decimal(gain)}kg，"
                            f"但 {holder.market} 可能缺 {fmt_decimal(loss)}kg；"
                            "需采购在当班决定优先保哪场，或另找一批可共存的货。"
                        ),
                        "sources": [lot.record_id, order.record_id],
                    }
                )

    def locked_lots(self) -> set[str]:
        return {
            allocation.lot_id
            for allocations in self.locked_allocations.values()
            for allocation in allocations
        }

    # ------------------------------------------------------------------
    # 装车指令
    # ------------------------------------------------------------------

    def _plan_instructions(
        self, orders: list[MarketOrder], assignments: list[Chunk]
    ) -> None:
        assigned_lots = {chunk.lot_id for chunk in assignments}
        for lot_id, view in self.views.items():
            lot = view.lot
            if lot.duplicate_of or lot.released:
                self.plan.load_instructions.append(
                    LoadInstruction(
                        action="do_not_load",
                        lot_id=lot_id,
                        market=None,
                        packages=lot.packages,
                        kg_low=lot.low_kg,
                        kg_high=lot.high_kg,
                        note=(
                            f"{lot_id} 已由更正释放（指回 {lot.duplicate_of}），"
                            f"不再单独计供给；其剩余实物属于 {lot.duplicate_of}，"
                            "不得另记一场。"
                        ),
                        confirm_with=Role.SUPPLIER,
                        sources=(lot.record_id,),
                        original_text=lot.text,
                    )
                )
                continue
            if lot_id in assigned_lots:
                continue
            if lot.tentative:
                self.plan.load_instructions.append(
                    LoadInstruction(
                        action="hold",
                        lot_id=lot_id,
                        market=lot.market_hint,
                        packages=lot.packages,
                        kg_low=lot.low_kg,
                        kg_high=lot.high_kg,
                        note=f"{lot_id} 指代未确认，只能暂留，确认前不能装车。",
                        confirm_with=Role.SUPPLIER,
                        sources=(lot.record_id,),
                        original_text=lot.text,
                    )
                )
                continue
            self.plan.load_instructions.append(
                LoadInstruction(
                    action="hold",
                    lot_id=lot_id,
                    market=None,
                    packages=lot.packages,
                    kg_low=view.remaining_low,
                    kg_high=view.remaining_high,
                    note=(
                        f"{lot_id} 暂未分配（到场、品种或数量条件不满足），"
                        "保留待跟单确认。"
                    ),
                    confirm_with=Role.BUYER,
                    sources=(lot.record_id,),
                    original_text=lot.text,
                )
            )

        for market_plan in self.plan.market_plans.values():
            for allocation in market_plan.allocations:
                lot = self.parsed.lots[allocation.lot_id]
                if allocation.role == "locked":
                    weighed = not any(
                        condition.topic == TOPIC_WEIGHING and condition.open
                        for condition in allocation.conditions
                    )
                    state = "已签收" if weighed else "已装但未过秤"
                    self.plan.load_instructions.append(
                        LoadInstruction(
                            action="do_not_reassign",
                            lot_id=allocation.lot_id,
                            market=allocation.market,
                            packages=allocation.packages,
                            kg_low=allocation.kg_low,
                            kg_high=allocation.kg_high,
                            note=(
                                f"{allocation.lot_id} {state}"
                                f" {fmt_decimal(allocation.kg_low)}kg；"
                                "已装部分照旧承担到场承诺，不能卸下重配，"
                                + (
                                    "但未过秤前不算已交割，需车队实秤。"
                                    if not weighed
                                    else "按回单锁定给本场。"
                                )
                            ),
                            confirm_with=None if weighed else Role.CARRIER,
                            sources=allocation.sources,
                            original_text=lot.text,
                        )
                    )
                    continue
                action, note, confirm_with = self._instruction_for(
                    allocation, lot, market_plan.order
                )
                self.plan.load_instructions.append(
                    LoadInstruction(
                        action=action,
                        lot_id=allocation.lot_id,
                        market=allocation.market,
                        packages=allocation.packages,
                        kg_low=allocation.kg_low,
                        kg_high=allocation.kg_high,
                        note=note,
                        confirm_with=confirm_with,
                        sources=tuple(
                            dict.fromkeys(allocation.sources + (lot.record_id,))
                        ),
                        original_text=lot.text,
                    )
                )

    def _instruction_for(
        self, allocation: Allocation, lot: QuoteLot, order: Optional[MarketOrder]
    ) -> tuple[str, str, Optional[Role]]:
        open_conditions = [
            condition for condition in allocation.conditions if condition.open
        ]
        blocking = [condition for condition in open_conditions if condition.blocking]
        if allocation.role == "tentative":
            return (
                "hold",
                f"{allocation.lot_id} 是移出暂留才进入组合的，先不要装："
                + "；".join(condition.text for condition in open_conditions),
                Role.SUPPLIER,
            )
        if lot.weighed is False or any(
            condition.topic == TOPIC_WEIGHING for condition in blocking
        ):
            return (
                "weigh_first",
                f"{allocation.lot_id} 尚未过秤；先实秤再按实重交接，计划只按"
                f" {fmt_decimal(allocation.kg_low)}kg 下界计，不按袋数中点。",
                Role.CARRIER,
            )
        if blocking:
            return (
                "hold",
                f"{allocation.lot_id} 先不要发："
                + "；".join(condition.text for condition in blocking),
                next((condition.confirm_with for condition in blocking), Role.BUYER),
            )
        if (
            lot.arrival_day is not None
            and order is not None
            and order.deadline_day is not None
            and lot.arrival_day > order.deadline_day
        ):
            return (
                "hold",
                f"{allocation.lot_id} 到场要等到{lot.arrival_text}，赶不上"
                f"{allocation.market}的{order.deadline_text or '到场窗口'}。",
                Role.SUPPLIER,
            )
        return (
            "load_now",
            f"{allocation.lot_id} 可发 {allocation.market}："
            f"{fmt_decimal(allocation.kg_low)}"
            + (
                f"–{fmt_decimal(allocation.kg_high)}kg"
                if allocation.kg_high != allocation.kg_low
                else "kg"
            )
            + (f"，{allocation.packages} 件" if allocation.packages else ""),
            None,
        )


# ---------------------------------------------------------------------------
# 语义解释、交接摘要与按场回查
# ---------------------------------------------------------------------------

def explain_statement(text: str, role: str, market: Optional[str] = None) -> dict:
    """解释一句含否定词的业务话，区分采购排除与供方风险提示。"""
    return describe_statement(text, Role(role), market)


def summarize_handoff(plan_result: Plan) -> dict:
    """给装车员的交接摘要：能发什么、等什么、谁要承认什么。"""
    can_send: list[dict] = []
    hold: list[dict] = []
    for instruction in plan_result.load_instructions:
        entry = {
            "lot": instruction.lot_id,
            "market": instruction.market,
            "packages": instruction.packages,
            "kg": _kg_text(instruction.kg_low, instruction.kg_high),
            "action": instruction.action,
            "confirm_with": instruction.confirm_with.value
            if instruction.confirm_with
            else None,
            "note": instruction.note,
        }
        if instruction.action == "load_now":
            can_send.append(entry)
        elif instruction.action in ("hold", "weigh_first"):
            hold.append(entry)

    acknowledgements: list[dict] = []
    for market, market_plan in plan_result.market_plans.items():
        for condition in market_plan.conditions:
            if not condition.open:
                continue
            acknowledgements.append(
                {
                    "market": market,
                    "topic": condition.topic,
                    "confirm_with": condition.confirm_with.value,
                    "advisory": False,
                    "text": condition.text,
                }
            )
        for condition in market_plan.advisory_conditions:
            if not condition.open:
                continue
            acknowledgements.append(
                {
                    "market": market,
                    "topic": condition.topic,
                    "confirm_with": condition.confirm_with.value,
                    "advisory": True,
                    "text": condition.text,
                }
            )
        if market_plan.shortage_kg > 0:
            acknowledgements.append(
                {
                    "market": market,
                    "topic": "shortage",
                    "confirm_with": Role.BUYER.value,
                    "advisory": False,
                    "text": (
                        f"{market} 缺 {fmt_decimal(market_plan.shortage_kg)}kg，"
                        "需采购确认是接受缺量、接受替代，还是改承诺。"
                    ),
                }
            )
    return {
        "can_send": can_send,
        "hold": hold,
        "acknowledgements": acknowledgements,
        "conflicts": list(plan_result.conflicts),
    }


def market_evidence(plan_result: Plan, market: str) -> dict:
    """按场回查已确认规格、报价原文、来源和是否过秤。"""
    market_plan = plan_result.market_plans.get(market)
    if market_plan is None:
        return {"market": market, "error": "本次导入没有该场订单"}
    evidence = []
    for allocation in market_plan.allocations:
        lot = plan_result.lots.get(allocation.lot_id)
        if lot is None:
            continue
        evidence.append(
            {
                "lot_id": allocation.lot_id,
                "role": allocation.role,
                "product": display_product(lot.product),
                "market_hint": lot.market_hint,
                "quantity": str(lot.quantity) if lot.quantity else None,
                "kg_low": fmt_decimal(allocation.kg_low),
                "kg_high": fmt_decimal(allocation.kg_high),
                "packages": allocation.packages,
                "weighed": lot.weighed,
                "delivered": allocation.role == "locked",
                "tentative": lot.tentative,
                "duplicate_of": lot.duplicate_of,
                "conditions": [
                    {
                        "topic": condition.topic,
                        "confirm_with": condition.confirm_with.value,
                        "text": condition.text,
                        "open": condition.open,
                    }
                    for condition in allocation.conditions
                ],
                "quote_text": lot.text,
                "quote_record": lot.record_id,
                "order_text": market_plan.order.text if market_plan.order else "",
                "sources": list(allocation.sources),
            }
        )
    return {
        "market": market,
        "verdict": market_plan.verdict.value,
        "reason": market_plan.reason,
        "order_text": market_plan.order.text if market_plan.order else "",
        "evidence": evidence,
        "released": [
            {
                "lot_id": lot.id,
                "duplicate_of": lot.duplicate_of,
                "quote_text": lot.text,
                "note": lot.note,
            }
            for lot in plan_result.lots.values()
            if lot.duplicate_of or lot.released
        ],
    }


def _dedupe_conditions(conditions: list[Condition]) -> list[Condition]:
    seen: set[tuple[str, str, bool, str]] = set()
    result: list[Condition] = []
    for condition in conditions:
        key = (condition.topic, condition.source_id, condition.blocking, condition.text)
        if key in seen:
            continue
        seen.add(key)
        result.append(condition)
    return result


def _high_for(view: LotView, kg: Decimal) -> Decimal:
    if view.remaining_low <= 0:
        return kg
    high = view.remaining_high * (kg / view.remaining_low)
    return max(high, kg)


def _lot_has_term(lot: QuoteLot, term: str) -> bool:
    if term in lot.text:
        return True
    return lot.attributes.get("leaf") == term


def _share_pct(order: MarketOrder, kg: Decimal) -> str:
    if not order.min_kg:
        return "未知"
    return f"{(kg / order.min_kg * 100):.1f}%"


def _pct(value: Optional[Decimal]) -> str:
    if value is None:
        return "未设"
    return f"{(value * 100):.0f}%"


def _fmt_money(value: Optional[Decimal]) -> str:
    return fmt_decimal(value) if value is not None else "未设"


def _kg_text(low: Optional[Decimal], high: Optional[Decimal]) -> str:
    if low is None:
        return ""
    if high is None or high == low:
        return f"{fmt_decimal(low)}kg"
    return f"{fmt_decimal(low)}–{fmt_decimal(high)}kg"
