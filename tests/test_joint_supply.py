"""读取项目语料与当班决策的回归测试。

每个测试对应一条业务要求，失败信息写清“为什么这样判”，方便非技术的
采购跟单复核。
"""
import json
import unittest
from decimal import Decimal
from pathlib import Path

from joint_supply.lexicon import (
    cn_to_decimal,
    parse_quantity,
    parse_range,
    to_kg,
)
from joint_supply.model import Role, Unit, Verdict
from joint_supply.parser import describe_statement, parse_records
from joint_supply.planner import (
    explain_statement,
    market_evidence,
    plan,
    summarize_handoff,
)
from materials import load_corpus

ROOT = Path(__file__).resolve().parent.parent


def corpus_records():
    return load_corpus()["records"]


def plan_of(records):
    return plan(parse_records(records))


class CorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_corpus()

    def test_project_has_materials(self):
        self.assertEqual(self.data["project"], "联合供货报价里的替代承诺与装车交接")
        self.assertEqual(self.data["material_nature"], "本地合成演练资料")

    def test_record_identifiers_are_unique(self):
        identifiers = [record["id"] for record in self.data["records"]]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_records_retain_their_raw_context(self):
        self.assertTrue(
            all(record["kind"] and record["text"] for record in self.data["records"])
        )
        self.assertTrue(any(len(record["text"]) > 12 for record in self.data["records"]))

    def test_every_quote_keeps_original_text(self):
        parsed = parse_records(corpus_records())
        originals = {record["id"]: record["text"] for record in corpus_records()}
        for lot in parsed.lots.values():
            self.assertIn(lot.record_id, originals)
            self.assertTrue(lot.text)

    def test_markets_have_distinct_name_tables(self):
        parsed = parse_records(corpus_records())
        self.assertIn("东场", parsed.profiles)
        self.assertIn("西场", parsed.profiles)
        east = parsed.profiles["东场"].resolve("青芯")
        west = parsed.profiles["西场"].resolve("青芯")
        self.assertNotEqual(east, west, "同一个“青芯”在两场必须指向不同品种")


class NumbersAndUnitsTest(unittest.TestCase):
    def test_chinese_numerals(self):
        self.assertEqual(cn_to_decimal("九十"), Decimal("90"))
        self.assertEqual(cn_to_decimal("一百零八"), Decimal("108"))
        self.assertEqual(cn_to_decimal("两千三百"), Decimal("2300"))
        self.assertEqual(cn_to_decimal("二点五"), Decimal("2.5"))
        self.assertEqual(cn_to_decimal("半"), Decimal("0.5"))

    def test_lb_converts_to_kg(self):
        self.assertAlmostEqual(float(to_kg(Decimal("200"), Unit.LB)), 90.72, places=2)

    def test_jin_converts_to_kg(self):
        self.assertEqual(to_kg(Decimal("200"), Unit.JIN), Decimal("100.000"))

    def test_range_parsing_keeps_both_bounds(self):
        low, high = parse_range("15–20")
        self.assertEqual((low, high), (Decimal("15"), Decimal("20")))

    def test_bag_range_uses_lower_bound_not_midpoint(self):
        parsed = parse_quantity("B7 三袋，每袋 15–20 公斤")
        self.assertEqual(parsed.quantity.low, Decimal("45"))
        self.assertEqual(parsed.quantity.high, Decimal("60"))
        # 中点 52.5 公斤绝不能当成已确认量。
        self.assertEqual(parsed.quantity.certain_low_kg, Decimal("45"))
        self.assertNotEqual(parsed.quantity.certain_low_kg, Decimal("52.5"))

    def test_case_count_without_per_case_weight_is_not_kilograms(self):
        parsed = parse_quantity("两筐")
        self.assertEqual(parsed.packages, 2)
        self.assertEqual(parsed.quantity.unit, Unit.CASE)
        self.assertEqual(parsed.quantity.certain_low_kg, Decimal("0"))

    def test_pounds_are_kept_as_bag_unit(self):
        parsed = parse_quantity("三袋，每袋十五磅")
        self.assertEqual(parsed.package_unit, Unit.LB)
        self.assertEqual(parsed.quantity.unit, Unit.LB)


class NegationTest(unittest.TestCase):
    def test_buyer_only_accept_is_not_supplier_non_guarantee(self):
        buyer = explain_statement("不要细叶外的", "buyer")
        supplier = explain_statement("不保证细叶", "supplier")
        self.assertEqual(buyer["statements"][0]["kind"], "buyer_exclusion")
        self.assertEqual(buyer["statements"][0]["semantics"], "only_accept")
        self.assertIsNone(buyer["statements"][0]["confirm_with"])
        self.assertEqual(supplier["statements"][0]["kind"], "supplier_non_guarantee")
        self.assertEqual(supplier["statements"][0]["confirm_with"], "buyer")

    def test_plain_buyer_exclusion_is_not_only_accept(self):
        result = explain_statement("不要细叶", "buyer")
        self.assertEqual(result["statements"][0]["semantics"], "exclude")

    def test_rootless_is_attribute_not_negation(self):
        statements = describe_statement("无根的那批可给西场抵缺", Role.SUPPLIER)["statements"]
        self.assertFalse(
            any(item["kind"] == "fact_negative" and item["target"].startswith("根") for item in statements)
        )

    def test_unweighed_is_fact_negative_routed_to_carrier(self):
        statements = describe_statement("尚未过秤", Role.CARRIER)["statements"]
        self.assertTrue(any(item["kind"] == "fact_negative" for item in statements))
        self.assertEqual(statements[0]["confirm_with"], "carrier")

    def test_order_only_accept_becomes_hard_constraint(self):
        records = [
            {"id": "o1", "kind": "order", "text": "东场订单|不要细叶外的，要一百斤，不换品。"}
        ]
        parsed = parse_records(records)
        order = parsed.orders["东场"]
        self.assertEqual(order.only_accept_terms, ("细叶",))
        self.assertEqual(order.excluded_terms, ())
        self.assertEqual(order.min_kg, Decimal("50.000"))
        self.assertFalse(order.allow_substitute)

    def test_supplier_non_guarantee_does_not_become_order_exclusion(self):
        records = [
            {"id": "o1", "kind": "order", "text": "东场订单|要白梗，净重底线五十公斤。"},
            {"id": "q1", "kind": "quote", "text": "档口|不保证细叶，E1 白梗一筐净重五十公斤。"},
        ]
        parsed = parse_records(records)
        order = parsed.orders["东场"]
        self.assertEqual(order.excluded_terms, ())
        self.assertEqual(order.only_accept_terms, ())
        lot = parsed.lots["E1"]
        topics = {condition.topic for condition in lot.conditions}
        self.assertIn("non_guarantee", topics)


class ParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = parse_records(corpus_records())
        cls.lots = cls.parsed.lots

    def test_orders_use_min_net_weight(self):
        east = self.parsed.orders["东场"]
        west = self.parsed.orders["西场"]
        self.assertEqual(east.min_kg, Decimal("90"))
        self.assertEqual(east.case_tolerance, 1)
        self.assertFalse(east.allow_substitute)
        self.assertEqual(west.min_kg, Decimal("60"))
        self.assertTrue(west.allow_substitute)
        self.assertEqual(west.allowed_substitute.max_share, Decimal("0.5"))
        self.assertEqual(west.allowed_substitute.max_price_gap, Decimal("0.3"))

    def test_same_word_resolved_by_market(self):
        self.assertEqual(self.lots["A7"].product, "white_stem")
        self.assertEqual(self.lots["B7"].product, "short_stem")

    def test_bag_lot_lower_bound(self):
        lot = self.lots["B7"]
        self.assertEqual(lot.quantity.low, Decimal("45"))
        self.assertEqual(lot.quantity.high, Decimal("60"))
        self.assertEqual(lot.low_kg, Decimal("45"))
        self.assertIn("不取中点", lot.note)

    def test_unweighed_load_line_has_no_weight(self):
        b7_lines = [line for line in self.parsed.loads if line.lot_id == "B7"]
        self.assertTrue(b7_lines)
        self.assertTrue(all(line.quantity is None for line in b7_lines))
        self.assertTrue(all(not line.delivered for line in b7_lines))

    def test_loaded_line_records_fact(self):
        a7_lines = [line for line in self.parsed.loads if line.lot_id == "A7"]
        self.assertEqual(len(a7_lines), 1)
        self.assertTrue(a7_lines[0].loaded)
        self.assertTrue(a7_lines[0].sealed)
        self.assertFalse(a7_lines[0].weighed)
        self.assertEqual(a7_lines[0].market, "东场")

    def test_correction_releases_alias_without_double_counting(self):
        q2 = self.lots["q2"]
        self.assertEqual(q2.duplicate_of, "A7")
        self.assertTrue(q2.released)
        self.assertFalse(q2.available)

    def test_correction_adds_new_lot_separately(self):
        c7 = self.lots["C7"]
        self.assertEqual(c7.product, "low_root_white_stem")
        self.assertEqual(c7.low_kg, Decimal("20"))
        self.assertEqual(c7.arrival_day, 1)

    def test_correction_of_label_mismatch_is_reported(self):
        kinds = {issue.kind for issue in self.parsed.issues}
        self.assertIn("product_label_conflict", kinds)

    def test_product_spec_difference_is_kept(self):
        # A7 是白梗，q2 说“低根白梗”，被指回同一批实物后不能把 A7 改标成低根。
        self.assertEqual(self.lots["A7"].product, "white_stem")
        self.assertIn("label_conflict", self.lots["A7"].attributes)


class PlanningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = parse_records(corpus_records())
        cls.plan = plan(cls.parsed)
        cls.handoff = summarize_handoff(cls.plan)

    def test_markets_present(self):
        self.assertEqual(set(self.plan.market_plans), {"东场", "西场"})

    def test_loaded_lot_is_locked_to_its_market(self):
        east = self.plan.market_plans["东场"]
        locked = [a for a in east.allocations if a.role == "locked"]
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0].lot_id, "A7")
        self.assertEqual(locked[0].market, "东场")

    def test_loaded_unweighed_weight_is_not_delivered(self):
        east = self.plan.market_plans["东场"]
        self.assertEqual(east.locked_kg, Decimal("45"))
        self.assertEqual(east.delivered_kg, Decimal("0"))

    def test_loaded_part_cannot_move_to_other_market(self):
        west = self.plan.market_plans["西场"]
        self.assertNotIn("A7", {allocation.lot_id for allocation in west.allocations})
        alternatives = "\n".join(west.alternatives)
        self.assertIn("已锁定给东场", alternatives)

    def test_case_concession_does_not_cut_net_weight(self):
        east = self.plan.market_plans["东场"]
        text = "\n".join(east.alternatives)
        self.assertIn("让筐不等于让净重", text)
        # 东场净重底线仍是 90kg；A7 剩余一筐 45kg 只能补 45kg。
        self.assertEqual(east.order.min_kg, Decimal("90"))

    def test_released_lot_is_not_supply(self):
        for market_plan in self.plan.market_plans.values():
            self.assertNotIn(
                "q2", {allocation.lot_id for allocation in market_plan.allocations}
            )
        self.assertIn("q2", self.plan.released)
        do_not_load = [
            item for item in self.plan.load_instructions if item.lot_id == "q2"
        ]
        self.assertEqual(do_not_load[0].action, "do_not_load")

    def test_west_accepts_two_products_within_limit(self):
        west = self.plan.market_plans["西场"]
        products = {
            self.plan.lots[allocation.lot_id].product
            for allocation in west.allocations
            if allocation.role != "locked"
        }
        self.assertIn("short_stem", products)
        self.assertIn("low_root_white_stem", products)

    def test_substitute_respects_share_cap(self):
        west = self.plan.market_plans["西场"]
        substitute_kg = sum(
            (a.kg_low for a in west.allocations if a.role == "substitute"), Decimal("0")
        )
        self.assertLessEqual(substitute_kg, Decimal("60") * Decimal("0.5"))

    def test_substitute_without_price_needs_supplier(self):
        west = self.plan.market_plans["西场"]
        conditions = [c for c in west.conditions if c.topic == "price"]
        self.assertTrue(conditions)
        self.assertEqual(conditions[0].confirm_with, Role.SUPPLIER)

    def test_west_needs_buyer_for_mixed_stall(self):
        west = self.plan.market_plans["西场"]
        mixed = [c for c in west.conditions if c.topic == "mixed_stall"]
        self.assertTrue(mixed)
        self.assertEqual(mixed[0].confirm_with, Role.BUYER)

    def test_west_needs_carrier_for_weighing(self):
        west = self.plan.market_plans["西场"]
        weighing = [c for c in west.conditions if c.topic == "weighing"]
        self.assertTrue(weighing)
        self.assertEqual(weighing[0].confirm_with, Role.CARRIER)

    def test_late_lot_is_not_allocated_to_east(self):
        east = self.plan.market_plans["东场"]
        self.assertNotIn("C7", {a.lot_id for a in east.allocations})
        text = "\n".join(east.alternatives)
        self.assertIn("明早", text)

    def test_east_verdict_is_conditional_or_blocked_not_confirmed(self):
        east = self.plan.market_plans["东场"]
        self.assertIn(east.verdict, {Verdict.CONDITIONAL, Verdict.BLOCKED})
        # 第一筐已装但没秤：不能算“已确认”；A7 剩下的第二筐可以补到 90kg。
        self.assertLess(east.confirmed_kg, east.order.min_kg)
        self.assertEqual(east.shortage_kg, Decimal("0"))
        self.assertGreater(east.conditional_kg, Decimal("0"))

    def test_handoff_lists_what_can_be_sent(self):
        can_send = {item["lot"] for item in self.handoff["can_send"]}
        self.assertIn("A7", can_send)
        # 未过秤的 B7 只能先称，不能直接发。
        self.assertNotIn("B7", can_send)
        hold_actions = {item["lot"]: item["action"] for item in self.handoff["hold"]}
        self.assertEqual(hold_actions.get("B7"), "weigh_first")

    def test_acknowledgements_name_who_must_confirm(self):
        entries = {(item["market"], item["topic"]): item for item in self.handoff["acknowledgements"]}
        self.assertIn(("西场", "mixed_stall"), entries)
        self.assertEqual(entries[("西场", "mixed_stall")]["confirm_with"], "buyer")
        self.assertIn(("西场", "weighing"), entries)
        self.assertEqual(entries[("西场", "weighing")]["confirm_with"], "carrier")

    def test_market_evidence_returns_quote_text(self):
        evidence = market_evidence(self.plan, "西场")
        self.assertEqual(evidence["market"], "西场")
        self.assertTrue(evidence["evidence"])
        for item in evidence["evidence"]:
            self.assertTrue(item["quote_text"])
            self.assertTrue(item["quote_record"])


class CorrectionTest(unittest.TestCase):
    def test_two_sentences_point_to_same_common_lot(self):
        records = [
            {"id": "q1", "kind": "quote", "text": "档口|A7 东场品两筐净重四十五公斤一筐。"},
            {"id": "q2", "kind": "quote", "text": "档口|白梗那批还剩一筐，每筐四十五公斤。"},
            {"id": "q3", "kind": "quote", "text": "档口|D4 那批也是白梗，一筐四十五公斤。"},
            {
                "id": "c1",
                "kind": "correction",
                "text": "档口补记|q2 说的其实还是 A7 装车后留下的一筐。D4 那批也指 A7 那一筐。",
            },
        ]
        parsed = parse_records(records)
        self.assertEqual(parsed.lots["q2"].duplicate_of, "A7")
        self.assertEqual(parsed.lots["D4"].duplicate_of, "A7")
        self.assertTrue(parsed.lots["q2"].released)
        self.assertTrue(parsed.lots["D4"].released)
        self.assertEqual(parsed.lots["A7"].attributes.get("post_load_remaining_packages"), 1)

    def test_released_alias_does_not_create_second_supply(self):
        records = [
            {"id": "o1", "kind": "order", "text": "东场订单|要白梗，净重底线九十公斤。"},
            {"id": "q1", "kind": "quote", "text": "档口|A7 东场品两筐净重四十五公斤一筐。"},
            {"id": "q2", "kind": "quote", "text": "档口|白梗那批还剩一筐，每筐四十五公斤。"},
            {"id": "l1", "kind": "load", "text": "车队签收|A7 已装第一筐给东场，司机封箱。"},
            {
                "id": "c1",
                "kind": "correction",
                "text": "档口补记|q2 说的其实还是 A7 装车后留下的一筐。",
            },
        ]
        result = plan(parse_records(records))
        east = result.market_plans["东场"]
        self.assertEqual(east.locked_kg, Decimal("45"))
        self.assertEqual(
            sum((a.kg_low for a in east.allocations), Decimal("0")), Decimal("90")
        )
        actions = {item.lot_id: item.action for item in result.load_instructions}
        self.assertEqual(actions["q2"], "do_not_load")


class RepeatImportTest(unittest.TestCase):
    def test_duplicate_record_ids_are_not_double_counted(self):
        records = corpus_records()
        doubled = records + records
        parsed = parse_records(doubled)
        result = plan(parsed)
        self.assertTrue(parsed.ignored_duplicates)
        self.assertEqual(len(parsed.lots), len(parse_records(records).lots))
        east = result.market_plans["东场"]
        self.assertEqual(east.confirmed_kg + east.conditional_kg, Decimal("90"))

    def test_duplicate_lot_marker_keeps_first_source(self):
        records = corpus_records() + [
            {"id": "q9", "kind": "quote", "text": "另一个档口|A7 东场品两筐，每筐四十五公斤。"}
        ]
        parsed = parse_records(records)
        self.assertEqual(parsed.lots["A7"].record_id, "q1")
        self.assertIn("q9", parsed.lots["A7"].note + parsed.ignored_duplicates.__str__())


class JointSolverTest(unittest.TestCase):
    """两场争用同一批货时，要给出可共存的组合或明确的取舍选项。"""

    def _records(self):
        return [
            {
                "id": "g1",
                "kind": "glossary",
                "text": "名称表|东场的青芯是白梗菜，西场的青芯是辣味短梗。",
            },
            {
                "id": "o1",
                "kind": "order",
                "text": "东场订单|要白梗，净重底线六十公斤，必须今晚到。",
            },
            {
                "id": "o2",
                "kind": "order",
                "text": (
                    "西场订单|要短梗，要实到四十公斤。"
                    "白梗菜可以抵到一半，价差每公斤不超过三角。"
                ),
            },
            {"id": "q1", "kind": "quote", "text": "档口|E1 白梗两筐，每筐三十公斤。"},
        ]

    def test_solver_explains_tradeoff(self):
        result = plan_of(self._records())
        east = result.market_plans["东场"]
        west = result.market_plans["西场"]
        self.assertEqual(east.verdict, Verdict.CONFIRMED)
        self.assertEqual(west.verdict, Verdict.BLOCKED)
        self.assertTrue(result.conflicts)
        message = result.conflicts[0]["message"]
        self.assertIn("E1", message)
        self.assertIn("东场", message)
        self.assertIn("西场", message)
        self.assertIn("缺", message)

    def test_split_case_substitute_requires_buyer_acknowledgement(self):
        records = [
            {
                "id": "g1",
                "kind": "glossary",
                "text": "名称表|东场的青芯是白梗菜，西场的青芯是辣味短梗。",
            },
            {"id": "o1", "kind": "order", "text": "东场订单|要白梗，净重底线六十公斤。"},
            {
                "id": "o2",
                "kind": "order",
                "text": (
                    "西场订单|要短梗，要实到三十公斤。"
                    "白梗菜可以抵到一半，价差每公斤不超过三角。"
                ),
            },
            {"id": "q0", "kind": "quote", "text": "档口|E8 白梗两筐，每筐三十公斤。"},
            {"id": "q1", "kind": "quote", "text": "档口|E9 白梗两筐，每筐三十公斤。"},
            {"id": "q2", "kind": "quote", "text": "档口|E2 短梗两筐，每筐十公斤。"},
        ]
        result = plan_of(records)
        west = result.market_plans["西场"]
        substitute = [a for a in west.allocations if a.role == "substitute"]
        self.assertTrue(substitute, "西场缺量应触发替代，即使是拆筐")
        self.assertLessEqual(
            sum((a.kg_low for a in substitute), Decimal("0")), Decimal("15")
        )
        # 拆筐替代必须由买方明确认可，不能自行取整成整筐。
        self.assertTrue(
            any(
                condition.topic == "substitute" and condition.open
                for allocation in substitute
                for condition in allocation.conditions
            )
        )


class TentativePromotionTest(unittest.TestCase):
    """暂留批次只有在缺量时才移出，并且保住“指向未确认”这一条件。"""

    def test_tentative_lot_promoted_with_condition(self):
        records = [
            {
                "id": "g1",
                "kind": "glossary",
                "text": "名称表|东场的青芯是白梗菜，西场的青芯是辣味短梗。",
            },
            {
                "id": "o2",
                "kind": "order",
                "text": "西场订单|要短梗，要实到五十公斤。低根白梗可以抵到一半。",
            },
            {
                "id": "q2",
                "kind": "quote",
                "text": "联系人|低根白梗那批还剩两筐，每筐二十五公斤。",
            },
        ]
        result = plan_of(records)
        west = result.market_plans["西场"]
        self.assertTrue(west.allocations)
        tentative = [a for a in west.allocations if a.tentative]
        self.assertTrue(tentative)
        self.assertTrue(tentative[0].conditional)
        topics = {condition.topic for condition in tentative[0].conditions if condition.open}
        self.assertIn("reference", topics)
        instruction = next(
            item for item in result.load_instructions if item.lot_id == "q2"
        )
        self.assertEqual(instruction.action, "hold")
        self.assertEqual(instruction.confirm_with, Role.SUPPLIER)


class ReferenceTest(unittest.TestCase):
    def test_unresolved_reference_is_flagged(self):
        records = [
            {"id": "o1", "kind": "order", "text": "东场订单|按上次白梗的青芯，净重底线五十公斤。"}
        ]
        result = plan_of(records)
        east = result.market_plans["东场"]
        refs = [c for c in east.advisory_conditions if c.topic == "reference"]
        self.assertTrue(refs)

    def test_unique_reference_is_resolved(self):
        records = [
            {"id": "o1", "kind": "order", "text": "东场订单|按上次白梗的青芯，净重底线五十公斤。"},
            {"id": "q1", "kind": "quote", "text": "档口|A1 白梗一筐，每筐五十公斤。"},
        ]
        parsed = parse_records(records)
        result = plan(parsed)
        self.assertTrue(parsed.resolved_references)
        self.assertEqual(parsed.resolved_references[0]["resolved_to"], "A1")

    def test_ambiguous_reference_needs_buyer(self):
        records = [
            {"id": "o1", "kind": "order", "text": "东场订单|按上次白梗的青芯，净重底线五十公斤。"},
            {"id": "q1", "kind": "quote", "text": "档口|A1 白梗一筐，每筐五十公斤。"},
            {"id": "q2", "kind": "quote", "text": "档口|A2 白梗一筐，每筐六十公斤。"},
        ]
        parsed = parse_records(records)
        kinds = {issue.kind for issue in parsed.issues}
        self.assertIn("ambiguous_reference", kinds)
        issue = next(item for item in parsed.issues if item.kind == "ambiguous_reference")
        self.assertEqual(issue.confirm_with, Role.BUYER)


class InquiryAndConfirmationTest(unittest.TestCase):
    def test_inquiry_is_not_a_commitment(self):
        records = [
            {"id": "o1", "kind": "order", "text": "西场订单|要短梗，要实到五十公斤。"},
            {"id": "i1", "kind": "inquiry", "text": "档口|F1 短梗一筐，每筐五十公斤，是否要？"},
        ]
        parsed = parse_records(records)
        self.assertTrue(parsed.lots["F1"].tentative)
        result = plan(parsed)
        west = result.market_plans["西场"]
        self.assertEqual(west.shortage_kg, Decimal("50"))
        self.assertNotEqual(west.verdict, Verdict.CONFIRMED)

    def test_order_confirmation_closes_condition_without_new_supply(self):
        records = [
            {
                "id": "o1",
                "kind": "order",
                "text": "西场订单|要短梗，要实到五十公斤。",
            },
            {
                "id": "q1",
                "kind": "quote",
                "text": "档口|G1 西场青芯五十公斤，仍需确认是否接受不同档口混装。",
            },
            {
                "id": "n1",
                "kind": "confirmation",
                "text": "西场确认|接受混装，按 G1 实到。",
            },
        ]
        parsed = parse_records(records)
        lot = parsed.lots["G1"]
        mixed = [c for c in lot.conditions if c.topic == "mixed_stall"]
        self.assertTrue(mixed)
        self.assertFalse(mixed[0].open, "订单确认后混装条件应关闭")
        result = plan(parsed)
        west = result.market_plans["西场"]
        self.assertEqual(west.verdict, Verdict.CONFIRMED)
        self.assertEqual(west.confirmed_kg, Decimal("50"))


class CliTest(unittest.TestCase):
    def test_cli_runs_and_outputs_json(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "joint_supply.cli", str(ROOT / "data" / "corpus.json"), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("markets", payload)
        self.assertIn("handoff", payload)
        self.assertIn("东场", payload["markets"])

    def test_cli_market_evidence(self):
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "joint_supply.cli",
                str(ROOT / "data" / "corpus.json"),
                "--market",
                "西场",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["market"], "西场")
        self.assertIn("order_text", payload)


if __name__ == "__main__":
    unittest.main()
