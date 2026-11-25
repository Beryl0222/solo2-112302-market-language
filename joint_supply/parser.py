"""把原始记录解析成结构化数据。

解析层刻意保守：
* 指代（“那批”“按上次”）无法唯一确定时，标为暂留并生成待确认问题；
* 区间数量只保留下界和上界，不产生中点；
* 装车回单与报价分开存放，未过秤的装车行不产生公斤数；
* 更正只通过显式 refs 生效，不按相同词面把两个批次合并。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from . import lexicon
from .lexicon import (
    NUM,
    RANGE,
    UNIT,
    QuantityParse,
    canonical_product,
    clean_lot_marker,
    display_product,
    find_lot_markers,
    parse_day,
    parse_number,
    parse_quantity,
    parse_share,
)
from .model import (
    Condition,
    Confirmation,
    Kind,
    LoadLine,
    MarketOrder,
    MarketProfile,
    Quantity,
    QuoteLot,
    Record,
    ReferenceIssue,
    Role,
    SubstituteRule,
    TOPIC_MIXED_STALL,
    TOPIC_NON_GUARANTEE,
    TOPIC_PRICE,
    TOPIC_QUANTITY,
    TOPIC_REFERENCE,
    TOPIC_SUBSTITUTE,
    TOPIC_WEIGHING,
    Unit,
)

DEFAULT_MARKETS = ("东场", "西场")
# 已知方位场名任意位置匹配；其它“某某场”只在分句开头匹配，避免把
# “一筐给东场”“和西场”整段吞进场名。
MARKET_PATTERN = r"(?:[东西南北中]场|(?<![\u4e00-\u9fa5])[\u4e00-\u9fa5]{1,2}场)"
MARKET_RE = re.compile(MARKET_PATTERN)
RECORD_REF_RE = re.compile(r"(?<![A-Za-z0-9])(?P<id>[a-z]\d{1,3})(?![A-Za-z0-9])")

ROLE_BY_KIND = {
    Kind.ORDER: Role.BUYER,
    Kind.CONFIRMATION: Role.BUYER,
    Kind.QUOTE: Role.SUPPLIER,
    Kind.CORRECTION: Role.SUPPLIER,
    Kind.LOAD: Role.CARRIER,
    Kind.GLOSSARY: Role.UNKNOWN,
    Kind.INQUIRY: Role.BUYER,
    Kind.NOTE: Role.UNKNOWN,
}


@dataclass
class Parsed:
    records: list[Record]
    ignored_duplicates: list[dict] = field(default_factory=list)
    profiles: dict[str, MarketProfile] = field(default_factory=dict)
    orders: dict[str, MarketOrder] = field(default_factory=dict)
    lots: dict[str, QuoteLot] = field(default_factory=dict)
    loads: list[LoadLine] = field(default_factory=list)
    confirmations: list[Confirmation] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)
    issues: list[ReferenceIssue] = field(default_factory=list)
    resolved_references: list[dict] = field(default_factory=list)
    glossary_text: str = ""

    def records_by_id(self) -> dict[str, Record]:
        return {record.id: record for record in self.records}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_records(raw_records: Iterable[dict]) -> Parsed:
    records: list[Record] = []
    seen_ids: dict[str, Record] = {}
    ignored: list[dict] = []
    for index, raw in enumerate(raw_records):
        kind_text = str(raw.get("kind", "note")).lower()
        try:
            kind = Kind(kind_text)
        except ValueError:
            kind = Kind.NOTE
        text = str(raw.get("text", "")).strip()
        speaker = str(raw.get("speaker") or _header_of(text) or kind.value)
        role = Role(raw["role"]) if raw.get("role") in {item.value for item in Role} else ROLE_BY_KIND[kind]
        refs = tuple(raw.get("refs") or ())
        record_id = str(raw["id"])
        if record_id in seen_ids:
            original = seen_ids[record_id]
            ignored.append(
                {
                    "id": record_id,
                    "reason": "同一记录被重复导入，保留首次原文，不重复计数",
                    "kept_text": original.text,
                    "ignored_text": text,
                }
            )
            continue
        record = Record(
                id=record_id,
                kind=kind,
                speaker=speaker,
                role=role,
                text=text,
                refs=refs,
                sequence=index,
            )
        seen_ids[record_id] = record
        records.append(record)

    parsed = Parsed(records=records, ignored_duplicates=ignored)
    _parse_glossary(parsed)
    _ensure_profiles(parsed)
    _parse_orders(parsed)
    _parse_quotes(parsed)
    _parse_corrections(parsed)
    _parse_loads(parsed)
    _parse_confirmations(parsed)
    _resolve_references(parsed)
    _apply_confirmations(parsed)
    return parsed


def _header_of(text: str) -> str:
    return text.split("|", 1)[0].strip() if "|" in text else ""


def _body_of(record: Record) -> str:
    return record.text.split("|", 1)[1].strip() if "|" in record.text else record.text.strip()


# ---------------------------------------------------------------------------
# 名称表 / 规格词汇
# ---------------------------------------------------------------------------

def _parse_glossary(parsed: Parsed) -> None:
    texts = [record.text for record in parsed.records if record.kind is Kind.GLOSSARY]
    parsed.glossary_text = "\n".join(texts)

    for text in texts:
        body = text.split("|", 1)[1] if "|" in text else text
        for clause in re.split(r"[，,。；;]", body):
            clause = clause.strip()
            alias_match = re.match(rf"(?P<market>{MARKET_PATTERN})的(?P<alias>[^是]+)是(?P<target>.+)", clause)
            if alias_match:
                market = alias_match.group("market")
                alias = alias_match.group("alias").strip()
                target = alias_match.group("target").strip()
                profile = parsed.profiles.setdefault(market, MarketProfile(name=market))
                key = _product_key_for_display(target)
                if key:
                    profile.aliases[alias] = key
                    if profile.default_product is None:
                        profile.default_product = key
                continue

            bag_match = re.search(r"袋装(?:按|用|以)(磅|公斤|斤)", clause)
            if bag_match:
                unit = Unit.parse(bag_match.group(1))
                for market in _markets_in(text) or DEFAULT_MARKETS:
                    profile = parsed.profiles.setdefault(market, MarketProfile(name=market))
                    profile.bag_unit = unit
                continue

            case_match = re.search(r"整筐(?:按|用|以)?(?:实秤)?(公斤|磅|斤)", clause)
            if case_match:
                unit = Unit.parse(case_match.group(1))
                for market in _markets_in(text) or DEFAULT_MARKETS:
                    profile = parsed.profiles.setdefault(market, MarketProfile(name=market))
                    profile.case_unit = unit

    for text in texts:
        for market in _markets_in(text):
            parsed.profiles.setdefault(market, MarketProfile(name=market))
    for text in texts:
        for market, profile in parsed.profiles.items():
            if not profile.raw_text:
                profile.raw_text = next(
                    (item.text for item in parsed.records if item.kind is Kind.GLOSSARY), ""
                )


def _product_key_for_display(display: str) -> Optional[str]:
    for spec in lexicon.PRODUCTS:
        if display in spec.aliases or display == spec.display:
            return spec.key
    return None


def _ensure_profiles(parsed: Parsed) -> None:
    for record in parsed.records:
        for market in _markets_in(record.text):
            parsed.profiles.setdefault(market, MarketProfile(name=market))
    for market, profile in parsed.profiles.items():
        if not profile.aliases:
            # 没有名称表时只使用全局同名词，绝不把“青芯”这类同词异义强判给某场。
            if market == "东场":
                profile.default_product = "white_stem"
            elif market == "西场":
                profile.default_product = "short_stem"


def _markets_in(text: str) -> list[str]:
    markets: list[str] = []
    for match in MARKET_RE.finditer(text):
        market = match.group(0)
        if market not in markets:
            markets.append(market)
    return markets


def _market_from_header(header: str) -> Optional[str]:
    match = MARKET_RE.search(header)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# 订单
# ---------------------------------------------------------------------------

DEADLINE_WORDS = ("今晚", "今天", "当日", "马上", "明早", "明天", "后天", "次日")


def _parse_orders(parsed: Parsed) -> None:
    for record in parsed.records:
        if record.kind is not Kind.ORDER:
            continue
        body = _body_of(record)
        header = _header_of(record.text)
        market = _market_from_header(header) or (_markets_in(body) or [None])[0]
        if not market:
            parsed.issues.append(
                ReferenceIssue(
                    "order_market_unknown",
                    f"订单 {record.id} 未写明场名，需采购确认归属场。",
                    Role.BUYER,
                    (record.id,),
                )
            )
            continue

        order = MarketOrder(market=market, record_id=record.id, text=record.text)
        order.product_terms = _extract_order_product_terms(body, market, parsed.profiles)
        order.product = _first_product(order.product_terms, market, parsed.profiles)

        min_kg = _extract_min_kg(body)
        order.min_kg = min_kg

        case_match = re.search(rf"能少\s*(?P<num>{NUM})?\s*筐", body)
        if case_match:
            count = int(parse_number(case_match.group("num") or "一"))
            order.case_tolerance = count

        if re.search(
            r"(?:不能|不可|不得|不|不要|不接受|不允许)换(?:菜|品|品种)"
            r"|不要替代|不接受替代|不能替代|不换货",
            body,
        ):
            order.allow_substitute = False

        substitute = _extract_substitute(body, market, parsed.profiles)
        if substitute:
            order.allowed_substitute = substitute
            order.allow_substitute = True
            order.combine_allowed = substitute is not None

        deadline = _extract_deadline(body)
        if deadline["text"]:
            order.deadline_day = deadline["day"]
            order.deadline_text = deadline["text"]

        reference = re.search(r"(?:按|照|同)(?:上次|上一批|上批)(?P<ref>[^。；;，,]{1,24})", body)
        if reference:
            order.reference_quote = reference.group("ref").strip()
            order.reference_note = reference.group(0)

        if "实到" in body or "过秤" in body:
            order.required_attributes = order.required_attributes + ("weighed",)

        parsed.orders[market] = order
        _parse_order_exclusions(order, body, parsed)


def _parse_order_exclusions(order: MarketOrder, body: str, parsed: Parsed) -> None:
    """区分采购的排除条件与供方的不保证声明。

    * “不要细叶外的” = 只接受细叶 → only_accept_terms，属订单硬约束。
    * “不要细叶” = 排除细叶 → excluded_terms。
    * 供方“不保证细叶”不写进订单，而是记在报价条件里，找采购确认是否接受。
    """
    for negation in lexicon.find_negations(body):
        if negation.kind != lexicon.BUYER_EXCLUSION:
            continue
        term = parser_exclusion_target(negation.obj)
        if not term:
            continue
        if negation.marker == "不要…外":
            order.only_accept_terms = tuple(_dedupe((*order.only_accept_terms, term)))
        else:
            order.excluded_terms = tuple(_dedupe((*order.excluded_terms, term)))

    for negation in lexicon.find_negations(body):
        if negation.kind != lexicon.SUPPLIER_NON_GUARANTEE:
            continue
        term = parser_exclusion_target(negation.obj)
        order.conditions.append(
            Condition(
                TOPIC_NON_GUARANTEE,
                f"订单记录里出现供方口吻的“不保证{term}”，需采购确认是否接受该风险；"
                "它不等同于采购“不要”。",
                Role.BUYER,
                order.record_id,
                blocking=False,
            )
        )
        parsed.issues.append(
            ReferenceIssue(
                "supplier_non_guarantee_in_order",
                f"{order.record_id} 含供方“不保证{term}”的说法，已按风险提示处理，需采购确认是否接受。",
                Role.BUYER,
                (order.record_id,),
            )
        )


def parser_exclusion_target(obj: str) -> str:
    target = obj.strip()
    for suffix in ("之外", "以外", "外的", "外"):
        if target.endswith(suffix):
            target = target[: -len(suffix)]
    for prefix in ("要", "要的是", "只要", "细叶"):
        if target.startswith(prefix) and prefix != "细叶":
            target = target[len(prefix):]
    return target.strip(" 的，,。；;：:")


def _extract_order_product_terms(
    body: str, market: str, profiles: dict[str, MarketProfile]
) -> tuple[str, ...]:
    """只提取“品种说法”，不要把重量、筐数、时间混进来。"""
    terms: list[str] = []
    reference = re.search(
        r"(?:按|照|同)(?:上次|上一批|上批)(?P<ref>[^。；;，,]{1,24})", body
    )
    if reference:
        terms.append(reference.group("ref").strip())
    for match in re.finditer(
        rf"(?:要|按|给|要的是|需要)\s*(?P<term>[^。；;，,]{{1,20}})", body
    ):
        term = _cut_at_quantity(match.group("term").strip())
        if not term:
            continue
        if any(word in term for word in ("今晚", "明天", "一半", "价差")):
            continue
        if term in {"实到", "到", "货", "实称", "低根"} and term != "低根":
            continue
        terms.append(term)
    # 正文里直接出现的已知品种别名。
    for alias in sorted(lexicon._ALIAS_INDEX, key=len, reverse=True):
        if alias in body and not any(alias in term for term in terms):
            terms.append(alias)
    return tuple(_dedupe(terms))


_QUANTITY_CUT = re.compile(
    r"(?:[零〇一二两三四五六七八九十百千万\d.]+\s*(?:公斤|千克|kg|KG|磅|斤|筐|箱|件|袋|包))"
)


def _cut_at_quantity(text: str) -> str:
    """把“东场两百斤原规格”截成“东场原规格”。"""
    cut = _QUANTITY_CUT.search(text)
    if cut:
        text = text[: cut.start()] + text[cut.end():]
    text = re.sub(r"[东西南北中]场", "", text)
    return text.strip(" 的，,。；;：:")


def _first_product(terms: Iterable[str], market: str, profiles: dict[str, MarketProfile]) -> Optional[str]:
    for term in terms:
        product = canonical_product(term, market, profiles)
        if product:
            return product
    return None


def _extract_min_kg(body: str) -> Optional[Decimal]:
    """读订单的净重下界。

    只把“底线/实到/至少/给…N斤”这类数量表达当净重；区间取上界作为订单要求
    时容易高估，因此一律取区间下界，并在区间被当作最低要求时加提示。
    """
    patterns = (
        rf"净重(?:底线|下限|最低|至少|不低于|不少于)?\s*(?P<num>{RANGE}|{NUM})\s*(?P<unit>{UNIT})",
        rf"(?:要)?实到\s*(?P<num>{RANGE}|{NUM})\s*(?P<unit>{UNIT})",
        rf"(?:至少|不少于|不低于|底线)\s*(?P<num>{RANGE}|{NUM})\s*(?P<unit>{UNIT})",
        rf"(?:给|发|送|供)[^，,。；;]{{0,8}}?(?P<num>{RANGE}|{NUM})\s*(?P<unit>{UNIT})",
        rf"(?:要|需|需要)\s*(?P<num>{RANGE}|{NUM})\s*(?P<unit>{UNIT})",
    )
    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            low, high = lexicon.parse_range(match.group("num"))
            unit = Unit.parse(match.group("unit"))
            converted = lexicon.to_kg(low, unit)
            if unit is Unit.KG:
                pass
            order_high = lexicon.to_kg(high, unit)
            if order_high is not None and converted is not None and order_high != converted:
                # 区间订单只按下界承诺；上界不当作必须达到。
                return converted
            return converted
    return None


def _extract_substitute(body: str, market: str, profiles: dict[str, MarketProfile]) -> Optional[SubstituteRule]:
    match = re.search(
        rf"(?P<sub>[^，,。；;]{{1,14}}?)(?:可以|可)?抵(?:到|至)?\s*(?P<share>一半|[一二三四五六七八九十]成|\d+\s*[%％])",
        body,
    )
    if not match:
        match = re.search(
            rf"(?P<sub>[^，,。；;]{{1,14}}?)(?:可以|可)(?:作为|当|算)(?:替代|替补)",
            body,
        )
    if not match:
        return None
    sub_term = match.group("sub").strip()
    key = canonical_product(sub_term, None, profiles)
    if not key:
        return None
    share = parse_share(match.group("share")) if "share" in match.groupdict() and match.group("share") else None
    gap_match = re.search(
        rf"价差(?:每公斤)?(?:不超过|不高于|最多)\s*(?P<num>{NUM})\s*(?P<unit>角|元|分)",
        body,
    )
    max_gap = None
    if gap_match:
        max_gap = lexicon.money_to_yuan(parse_number(gap_match.group("num")), gap_match.group("unit"))
    return SubstituteRule(
        substitute_terms=(key,),
        max_share=share,
        max_price_gap=max_gap,
        note=match.group(0),
        accepted_by_buyer=True,
    )


def _extract_deadline(body: str) -> dict:
    for word in DEADLINE_WORDS:
        index = body.find(word)
        if index < 0:
            continue
        # 只取时间词附近的最小短语，避免把“实秤二十公斤但明早才到”整句当时间。
        window = body[max(0, index - 6) : index + len(word)]
        for marker in ("但", "，", ",", "；", ";", "且", "，"):
            if marker in window:
                window = window.rsplit(marker, 1)[-1]
        return {"text": window.strip(), "day": parse_day(word)}
    return {"text": "", "day": None}


# ---------------------------------------------------------------------------
# 报价
# ---------------------------------------------------------------------------

def _parse_quotes(parsed: Parsed) -> None:
    for record in parsed.records:
        if record.kind not in (Kind.QUOTE, Kind.INQUIRY):
            continue
        body = _body_of(record)
        origin = _header_of(record.text)
        segments = _split_lots(body)
        if not segments:
            segments = [(record.id, body)]
        for lot_id, segment in segments:
            lot = _parse_lot_segment(
                lot_id=lot_id,
                segment=segment,
                record=record,
                origin=origin,
                parsed=parsed,
            )
            if record.kind is Kind.INQUIRY:
                lot.tentative = True
                lot.attributes["inquiry"] = True
                lot.conditions.append(
                    Condition(
                        TOPIC_QUANTITY,
                        "有条件的询盘，尚未形成供方承诺。",
                        Role.SUPPLIER,
                        record.id,
                    )
                )
            if lot_id in parsed.lots:
                # 同一批次被重复导入：保留原文，不重复计供给。
                existing = parsed.lots[lot_id]
                if existing.record_id != record.id:
                    existing.note += f"；重复导入 {record.id}，未重复计数"
                continue
            parsed.lots[lot_id] = lot


def _split_lots(body: str) -> list[tuple[str, str]]:
    markers = find_lot_markers(body)
    if not markers:
        return []
    prelude = body[: markers[0][1]].strip(" ，,。；;")
    segments: list[tuple[str, str]] = []
    for index, (marker, start, _end) in enumerate(markers):
        end = markers[index + 1][1] if index + 1 < len(markers) else len(body)
        text = body[start:end].strip(" ，,。；;")
        if index == 0 and prelude:
            # 批次号前的说明（如“不保证细叶”）属于第一段的限定语，保留下来。
            text = f"{prelude}；{text}"
        segments.append((clean_lot_marker(marker), text))
    return segments


def _parse_lot_segment(
    *,
    lot_id: str,
    segment: str,
    record: Record,
    origin: str,
    parsed: Parsed,
) -> QuoteLot:
    market_hint = (_markets_in(segment) or [None])[0]
    destination = re.search(
        r"(?:可|能|可以)(?:给|供|发)(?P<market>[东西南北中]场)(?:抵|补|填|顶)?",
        segment,
    )
    product_market = market_hint or (destination.group("market") if destination else None)
    product = _lot_product(segment, product_market, parsed.profiles)
    profile = parsed.profiles.get(market_hint) if market_hint else None
    qty = parse_quantity(segment, profile=profile, source_id=record.id)

    lot = QuoteLot(
        id=lot_id,
        record_id=record.id,
        text=segment if segment.lstrip().upper().startswith(lot_id) else f"{lot_id} {segment}",
        product=product,
        market_hint=market_hint,
        quantity=qty.quantity,
        packages=qty.packages,
        package_kg_low=qty.package_kg_low,
        package_kg_high=qty.package_kg_high,
        package_unit=qty.package_unit,
        origin=origin,
        note="；".join(qty.notes),
        refs=(record.id,),
    )

    priced = re.search(rf"(?P<num>{NUM})\s*(?:元|块)(?:/|每)?(?:公斤|kg)", segment)
    if priced:
        lot.unit_price = parse_number(priced.group("num"))
    if "原价" in segment:
        lot.price_ref = "原价"
    if "按上次" in segment:
        lot.price_ref = "按上次报价"

    arrival = _extract_deadline(segment)
    lot.arrival_text = arrival["text"]
    lot.arrival_day = arrival["day"]

    lot.weighed = _weighed_status(segment)
    lot.attributes = lexicon.attributes_in(segment)
    destination = re.search(
        r"(?:可|能|可以)(?:给|供|发)(?P<market>[东西南北中]场)(?:抵|补|填|顶)?",
        segment,
    )
    if destination:
        lot.attributes["destination_market"] = destination.group("market")
    if _has_unresolved_anaphor(segment):
        lot.attributes["anaphor"] = True

    for condition in _lot_conditions(segment, record, lot, qty):
        lot.conditions.append(condition)

    if _has_unresolved_anaphor(segment):
        lot.tentative = True
        lot.conditions.append(
            Condition(
                TOPIC_REFERENCE,
                "“那批”指代不明，需供方确认是哪一批，确认前只作暂留。",
                Role.SUPPLIER,
                record.id,
            )
        )
    return lot


def _lot_product(segment: str, market_hint: Optional[str], profiles: dict[str, MarketProfile]) -> Optional[str]:
    text = segment
    # 去掉批次号，避免 A7 之类干扰。
    text = re.sub(r"^[A-Z]\d{1,3}\s*", "", text)
    for market in _markets_in(text):
        text = text.replace(market, "")
    if market_hint and market_hint in profiles:
        profile = profiles[market_hint]
        if profile.default_product and (
            re.search(r"(?:场品|原规格|原品|本场品)", text)
            or re.match(r"^\s*品", text)
        ):
            return profile.default_product
    product = None
    if market_hint and market_hint in profiles:
        candidate = profiles[market_hint].resolve(text)
        if candidate:
            product = candidate
    if product is None:
        product = canonical_product(text, market_hint, profiles)
    return product


def _weighed_status(text: str) -> Optional[bool]:
    if re.search(r"(?:尚无|尚未|没有|未)过秤", text):
        return False
    if "过秤" in text or "实秤" in text:
        return True
    return None


def _has_unresolved_anaphor(text: str) -> bool:
    """“那批/这车”没有在同句里说清是哪一批时，一律先标暂留。"""
    for match in re.finditer(r"(?:那批|这批|那车|这车)", text):
        rest = text[match.end():]
        if re.match(r"\s*(?:就是|指的是|即是|即|为)", rest):
            continue
        return True
    return False


def _lot_conditions(
    segment: str, record: Record, lot: QuoteLot, qty: "QuantityParse"
) -> list[Condition]:
    conditions: list[Condition] = []
    if re.search(r"(?:是否接受|能否接受|需确认是否接受).{0,8}混装|混装.{0,8}(?:需确认|待确认|是否接受)", segment):
        conditions.append(
            Condition(
                TOPIC_MIXED_STALL,
                "不同档口混装需场方确认。",
                Role.BUYER,
                record.id,
            )
        )
    status = _weighed_status(segment)
    if status is False:
        conditions.append(
            Condition(
                TOPIC_WEIGHING,
                "该批未过秤，公斤数需车队实秤后确认。",
                Role.CARRIER,
                record.id,
            )
        )
    if re.search(r"不保证|不能保证|无法保证", segment):
        conditions.append(
            Condition(
                TOPIC_NON_GUARANTEE,
                "供方明确不保证该规格，需采购确认是否接受，不能视同采购的排除条件。",
                Role.BUYER,
                record.id,
            )
        )
    if "原价" in segment or "按上次" in segment:
        conditions.append(
            Condition(
                TOPIC_PRICE,
                "价格沿用上一次报价，需供方确认对应哪次报价及每公斤单价。",
                Role.SUPPLIER,
                record.id,
            )
        )
    if qty.is_range:
        conditions.append(
            Condition(
                TOPIC_QUANTITY,
                f"数量为区间 {qty.quantity}，未过秤前只按下界计入，"
                "上界不当作已确认供给。",
                Role.SUPPLIER,
                record.id,
                blocking=False,
            )
        )
    return conditions


# ---------------------------------------------------------------------------
# 更正
# ---------------------------------------------------------------------------

def _parse_corrections(parsed: Parsed) -> None:
    """逐句处理补正：每句只改它自己点名的那一批。

    同一个补正记录可以有两句，各自指回不同批次；不做“整段共享一个目标”。
    """
    for record in parsed.records:
        if record.kind is not Kind.CORRECTION:
            continue
        body = _body_of(record)
        sentences = lexicon.split_sentences(body) or [body]
        all_markers = [clean_lot_marker(marker) for marker, _s, _e in find_lot_markers(body)]

        parsed.corrections.append(
            {
                "record_id": record.id,
                "refs": tuple(
                    dict.fromkeys(
                        match.group("id")
                        for match in RECORD_REF_RE.finditer(body)
                        if match.group("id") in parsed.lots
                    )
                ),
                "lots": tuple(dict.fromkeys(all_markers)),
                "text": body,
            }
        )

        for sentence in sentences:
            refs = [
                match.group("id")
                for match in RECORD_REF_RE.finditer(sentence)
                if match.group("id") in parsed.lots
            ]
            markers = [
                clean_lot_marker(marker)
                for marker, _s, _e in find_lot_markers(sentence)
            ]
            target = _correction_target(sentence)
            executive = _executive_source(sentence, refs, markers, parsed)
            if target is None and executive is not None:
                # “D4 那批也是 A7 剩的”这类无“就是”标记的说法。
                target = executive[1]

            subjects = [
                item
                for item in sorted(
                    set(refs) | {marker for marker in markers if marker in parsed.lots},
                    key=sentence.find,
                )
                if item != target
            ]
            for ref in subjects:
                self_lot = parsed.lots[ref]
                if target and target == ref:
                    self_lot.note = _append_note(
                        self_lot.note, f"{record.id} 确认 {ref} 即该批本身，未新增供给"
                    )
                    for condition in self_lot.conditions:
                        if condition.topic == TOPIC_REFERENCE:
                            condition.satisfied_by = record.id
                    continue
                if target and target in parsed.lots:
                    _release_alias(parsed, record, self_lot, target, sentence)
                elif target:
                    self_lot.note = _append_note(
                        self_lot.note, f"{record.id} 指认目标 {target} 尚未登记"
                    )
                quantity = _correction_quantity(sentence)
                if quantity is not None:
                    self_lot.quantity = quantity
                    if quantity.unit is Unit.CASE:
                        # 只给件数时保留原每件净重，不把件数当公斤。
                        self_lot.packages = int(quantity.low)
                        if self_lot.package_kg_low is not None:
                            self_lot.quantity = Quantity(
                                self_lot.package_kg_low * quantity.low,
                                (self_lot.package_kg_high or self_lot.package_kg_low)
                                * quantity.low,
                                self_lot.package_unit,
                                self_lot.record_id,
                            )
                    self_lot.note = _append_note(
                        self_lot.note, f"{record.id} 更正数量为 {quantity}"
                    )

            # 句子里被点名、但还没登记成批次的那一批：按补正新增。
            for marker in markers:
                if marker in parsed.lots:
                    continue
                segment = _segment_for_marker(sentence, marker)
                if not segment:
                    continue
                lot = _parse_lot_segment(
                    lot_id=marker,
                    segment=segment,
                    record=record,
                    origin=_header_of(record.text),
                    parsed=parsed,
                )
                lot.note = _append_note(lot.note, f"由 {record.id} 补记新增")
                parsed.lots[marker] = lot


def _executive_source(
    sentence: str, refs: list[str], markers: list[str], parsed: Parsed
) -> Optional[tuple[str, str]]:
    """在“D4 那批也是 A7 剩的”这类句子里找（被指认批次, 目标批次）。"""
    candidates = [item for item in markers + refs if item in parsed.lots]
    if len(candidates) < 2:
        return None
    # 目标通常出现在“是/即/指”之后；这里取最后一个出现的批次号。
    order = sorted(candidates, key=lambda item: sentence.rfind(item))
    return order[0], order[-1]


def _release_alias(
    parsed: Parsed,
    record: Record,
    lot: QuoteLot,
    target: str,
    sentence: str,
) -> None:
    """把被更正的一条释放为目标的别名，剩余量归目标批次。"""
    lot.duplicate_of = target
    lot.tentative = False
    lot.released = True
    lot.note = _append_note(
        lot.note,
        f"{record.id} 更正：{lot.id} 实际指 {target} 的剩余货，{lot.id} 不再单独计供给",
    )
    lot.attributes["anaphor"] = True
    for condition in lot.conditions:
        if condition.topic == TOPIC_REFERENCE:
            condition.satisfied_by = record.id

    target_lot = parsed.lots[target]
    for sentence_part in lexicon.split_sentences(sentence):
        remainder = _correction_remainder(sentence_part)
        if remainder is not None:
            if not target_lot.packages:
                remainder = 1
            target_lot.attributes["post_load_remaining_packages"] = remainder
    if "post_load_remaining_packages" not in target_lot.attributes:
        if not target_lot.packages:
            target_lot.attributes["post_load_remaining_packages"] = 1
    target_lot.attributes["post_load_note"] = sentence

    if lot.product and target_lot.product and lot.product != target_lot.product:
        message = (
            f"{record.id} 把 {lot.id} 指回 {target} 的剩余货，但 {lot.id} 描述的品种"
            f"（{display_product(lot.product)}）与 {target} 原报价"
            f"（{display_product(target_lot.product)}）不一致；"
            "按“同一批实物”归原报价规格，若要当别场替代必须重新确认品种。"
        )
        target_lot.note = _append_note(target_lot.note, message)
        target_lot.attributes["label_conflict"] = {
            "claimed_product": lot.product,
            "original_product": target_lot.product,
            "source_id": record.id,
            "source_lot": lot.id,
        }
        parsed.issues.append(
            ReferenceIssue(
                "product_label_conflict",
                message,
                Role.SUPPLIER,
                (record.id, lot.id, target),
            )
        )


def _correction_remainder(body: str) -> Optional[int]:
    match = re.search(rf"留下(?:的)?\s*(?P<num>{NUM})?\s*(?:筐|袋|箱|件)", body)
    if match:
        return int(parse_number(match.group("num") or "一"))
    return None


def _correction_target(body: str) -> Optional[str]:
    match = re.search(r"(?:其实|实际上|实际)?(?:还是|就是|指的是|即是|等于)\s*(?P<target>[A-Z]\d{1,3})", body)
    if match:
        return clean_lot_marker(match.group("target"))
    match = re.search(r"指(?:的)?(?:是)?\s*(?P<target>[A-Z]\d{1,3})", body)
    if match:
        return clean_lot_marker(match.group("target"))
    return None


def _correction_quantity(body: str) -> Optional[Quantity]:
    match = re.search(
        rf"(?:改为|改成|更正为|修正为|实际(?:只)?有|只剩)\s*(?P<num>{NUM})\s*(?P<unit>筐|袋|公斤|kg|磅)",
        body,
    )
    if not match:
        return None
    value = parse_number(match.group("num"))
    unit = Unit.parse(match.group("unit"))
    # “一筐”没有每筐净重时只是件数，不能换算公斤。
    return Quantity(value, value, unit, body[:0])


def _segment_for_marker(body: str, marker: str) -> Optional[str]:
    markers = find_lot_markers(body)
    for index, (found, start, _end) in enumerate(markers):
        if clean_lot_marker(found) != marker:
            continue
        end = markers[index + 1][1] if index + 1 < len(markers) else len(body)
        return body[start:end].strip(" ，,。；;")
    return None


# ---------------------------------------------------------------------------
# 装车回单
# ---------------------------------------------------------------------------

def _parse_loads(parsed: Parsed) -> None:
    for record in parsed.records:
        if record.kind is not Kind.LOAD:
            continue
        body = _body_of(record)
        for sentence in lexicon.split_sentences(body):
            market = (_markets_in(sentence) or [None])[0]
            marker_match = re.search(r"(?<![A-Za-z0-9])(?P<id>[A-Z]\d{1,3})(?![A-Za-z0-9])", sentence)
            marker = clean_lot_marker(marker_match.group("id")) if marker_match else None
            if not marker:
                continue
            lot = parsed.lots.get(marker)
            weighed = _weighed_status(sentence)
            sealed = bool(re.search(r"封箱|封车|已封|施封", sentence))
            loaded = bool(re.search(r"已装|已发|已上|装车|装好|装了", sentence))
            quantity = None
            packages = None

            if weighed is False or re.search(r"(?:尚无|尚未|没有|未)过秤|无净重", sentence):
                # 未过秤的装车行只记录“装了这一批”，不产生公斤数。
                parsed.loads.append(
                    LoadLine(
                        record_id=record.id,
                        lot_id=marker,
                        market=market or (lot.market_hint if lot else ""),
                        quantity=None,
                        packages=None,
                        sealed=sealed,
                        weighed=False,
                        loaded=loaded,
                        text=sentence,
                        note="尚未过秤，不能按已交割重量计入",
                    )
                )
                if lot is not None:
                    lot.weighed = False
                    existing = {condition.topic for condition in lot.conditions}
                    if TOPIC_WEIGHING not in existing:
                        lot.conditions.append(
                            Condition(
                                TOPIC_WEIGHING,
                                "车队回单显示尚未过秤净重，需实秤后确认。",
                                Role.CARRIER,
                                record.id,
                            )
                        )
                continue

            case_match = re.search(rf"(?:第(?P<ord>{NUM})?)?\s*(?P<count>{NUM})?\s*(?P<container>筐|箱|件|袋|包)", sentence)
            if case_match:
                count = case_match.group("count")
                packages = int(parse_number(count)) if count else 1
                per_low, per_high = _lot_package_weight(lot)
                if per_low is not None:
                    unit = lot.package_unit if lot else Unit.KG
                    quantity = Quantity(per_low * packages, (per_high or per_low) * packages, unit)
            else:
                weight_match = re.search(rf"(?P<num>{RANGE}|{NUM})\s*(?P<unit>{UNIT})", sentence)
                if weight_match:
                    low, high = lexicon.parse_range(weight_match.group("num"))
                    quantity = Quantity(low, high, Unit.parse(weight_match.group("unit")))

            parsed.loads.append(
                LoadLine(
                    record_id=record.id,
                    lot_id=marker,
                    market=market or (lot.market_hint if lot else ""),
                    quantity=quantity,
                    packages=packages,
                    sealed=sealed,
                    weighed=weighed is True,
                    loaded=loaded,
                    text=sentence,
                    note="" if quantity else "装了货但回单没有可核净重",
                )
            )
            if lot is not None and not loaded and weighed is False:
                # 只有“还没装、又明确没秤”的情况才把批次本身标为未过秤；
                # 已装行的未过秤由该行自己承担，不能连带影响尚未装运的剩余货。
                lot.weighed = False


def _lot_package_weight(lot: Optional[QuoteLot]) -> tuple[Optional[Decimal], Optional[Decimal]]:
    if lot is None:
        return None, None
    if lot.package_kg_low is not None:
        return lot.package_kg_low, lot.package_kg_high
    if lot.quantity and lot.packages and lot.quantity.unit is not Unit.CASE:
        return (
            lot.quantity.low / lot.packages,
            lot.quantity.high / lot.packages,
        )
    return None, None


# ---------------------------------------------------------------------------
# 确认
# ---------------------------------------------------------------------------

def _parse_confirmations(parsed: Parsed) -> None:
    for record in parsed.records:
        if record.kind is not Kind.CONFIRMATION:
            continue
        body = _body_of(record)
        market = _market_from_header(_header_of(record.text)) or (_markets_in(body) or [None])[0]
        topics: list[str] = []
        if "混装" in body:
            topics.append(TOPIC_MIXED_STALL)
        if "过秤" in body or "实秤" in body:
            topics.append(TOPIC_WEIGHING)
        if "价差" in body or "价格" in body or "单价" in body:
            topics.append(TOPIC_PRICE)
        if "替代" in body or "抵" in body:
            topics.append(TOPIC_SUBSTITUTE)
        parsed.confirmations.append(
            Confirmation(
                record_id=record.id,
                market=market,
                topics=tuple(dict.fromkeys(topics)),
                text=body,
            )
        )


def _apply_confirmations(parsed: Parsed) -> None:
    for confirmation in parsed.confirmations:
        # 订单确认只关闭已存在的条件，不创造新供给。
        for lot in parsed.lots.values():
            if confirmation.market and lot.market_hint and lot.market_hint != confirmation.market:
                continue
            if not confirmation.market and lot.market_hint and confirmation.market is None:
                pass
            for condition in lot.conditions:
                if condition.topic in confirmation.topics and condition.open:
                    if not confirmation.market or _lot_may_serve(lot, confirmation.market):
                        condition.satisfied_by = confirmation.record_id
        for order in parsed.orders.values():
            if confirmation.market and order.market != confirmation.market:
                continue
            if TOPIC_NON_GUARANTEE in confirmation.topics:
                pass


def _lot_may_serve(lot: QuoteLot, market: str) -> bool:
    return lot.market_hint in (None, market)


# ---------------------------------------------------------------------------
# 指代解析
# ---------------------------------------------------------------------------

def _resolve_references(parsed: Parsed) -> None:
    for order in parsed.orders.values():
        if not order.reference_quote:
            continue
        candidates = [
            lot
            for lot in parsed.lots.values()
            if lot.available and lot.product == order.product
        ]
        if len(candidates) == 1:
            order.reference_note += f"；本次导入中唯一同品种批次为 {candidates[0].id}"
            parsed.resolved_references.append(
                {
                    "record_id": order.record_id,
                    "reference": order.reference_quote,
                    "resolved_to": candidates[0].id,
                    "rule": "本次导入中同品种唯一可售批次",
                    "sources": [order.record_id, candidates[0].record_id],
                }
            )
        elif len(candidates) > 1:
            parsed.issues.append(
                ReferenceIssue(
                    "ambiguous_reference",
                    f"订单 {order.record_id} 的“{order.reference_note}”可指 {', '.join(lot.id for lot in candidates)}，需采购明确。",
                    Role.BUYER,
                    (order.record_id, *[lot.record_id for lot in candidates]),
                )
            )

    for lot in parsed.lots.values():
        if not lot.available:
            if lot.duplicate_of and not any(
                item.get("reference") == "那批" and item.get("record_id") == lot.record_id
                for item in parsed.resolved_references
            ):
                parsed.resolved_references.append(
                    {
                        "record_id": lot.record_id,
                        "reference": "那批",
                        "resolved_to": lot.duplicate_of,
                        "sources": [lot.record_id, lot.duplicate_of],
                    }
                )
            continue
        if not lot.tentative:
            continue
        if lot.duplicate_of:
            parsed.resolved_references.append(
                {
                    "record_id": lot.record_id,
                    "reference": "那批",
                    "resolved_to": lot.duplicate_of,
                    "sources": [lot.record_id, lot.duplicate_of],
                }
            )
            continue
        parsed.issues.append(
            ReferenceIssue(
                "ambiguous_anaphor",
                f"报价 {lot.record_id} 用了“那批”，无法确定指向，已暂留，需供方确认具体批次。",
                Role.SUPPLIER,
                (lot.record_id,),
            )
        )


def _record_index(parsed: Parsed, record_id: str) -> int:
    for record in parsed.records:
        if record.id == record_id:
            return record.sequence
    return -1


def _append_note(existing: str, addition: str) -> str:
    return f"{existing}；{addition}" if existing else addition


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# 语义辅助：采购“不要…外” vs 供方“不保证”
# ---------------------------------------------------------------------------

def describe_statement(text: str, role: Role, market: Optional[str] = None) -> dict:
    """解释一句含有否定词的业务话，返回作用类型和确认对象。

    * 采购“不要细叶外的” = 只接受细叶；这是排除条件，不需要供方确认。
    * 供方“不保证细叶” = 风险提示，不是排除；必须由采购确认是否接受。
    """
    negations = lexicon.find_negations(text)
    results = []
    for negation in negations:
        if negation.kind == lexicon.BUYER_EXCLUSION:
            target = _exclusion_target(negation.obj)
            only_accept = negation.marker == "不要…外" or negation.marker == "除外"
            semantics = "only_accept" if only_accept else "exclude"
            results.append(
                {
                    "kind": "buyer_exclusion",
                    "target": target,
                    "target_product": canonical_product(target, market, {}) if target else None,
                    "semantics": semantics,
                    "confirm_with": None,
                    "note": "采购/场方的排除条件，属于订单硬约束。",
                }
            )
        elif negation.kind == lexicon.SUPPLIER_NON_GUARANTEE:
            target = _exclusion_target(negation.obj)
            results.append(
                {
                    "kind": "supplier_non_guarantee",
                    "target": target,
                    "target_product": canonical_product(target, market, {}) if target else None,
                    "semantics": "risk_notice",
                    "confirm_with": Role.BUYER.value,
                    "note": "供方不保证，不等于采购不要；需采购确认是否接受。",
                }
            )
        elif negation.kind == lexicon.FACT_NEGATIVE:
            results.append(
                {
                    "kind": "fact_negative",
                    "target": negation.obj,
                    "semantics": "fact_absent",
                    "confirm_with": Role.CARRIER.value,
                    "note": "事实性缺失（如未过秤），需相应一方补足事实。",
                }
            )
        elif negation.kind == lexicon.ORDER_NO_SUBSTITUTE:
            results.append(
                {
                    "kind": "order_no_substitute",
                    "target": negation.obj,
                    "semantics": "no_substitution",
                    "confirm_with": None,
                    "note": "订单禁止替代，任何替代货都不能抵该场承诺。",
                }
            )
    return {"text": text, "role": role.value, "statements": results}


def _exclusion_target(obj: str) -> str:
    return parser_exclusion_target(obj)
