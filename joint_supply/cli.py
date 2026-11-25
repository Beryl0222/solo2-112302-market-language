"""命令行入口：重复导入报价对话与装车回单，打印当班清单。

用法::

    python3 -m joint_supply.cli data/corpus.json
    python3 -m joint_supply.cli data/corpus.json --market 东场 --json
    python3 -m joint_supply.cli data/corpus.json --explain "不要细叶外的" --role buyer

同一个文件可以反复导入：记录 id 重复时只保留首次原文，并在
ignored_duplicates 中说明，不重复计数。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .model import Role
from .parser import parse_records
from .planner import explain_statement, market_evidence, plan, summarize_handoff


def load_records(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        records = payload.get("records", [])
    elif isinstance(payload, list):
        records = payload
    else:  # pragma: no cover - 防御性分支
        raise ValueError("输入必须是记录数组或含 records 的对象")
    if not isinstance(records, list):
        raise ValueError("records 必须是数组")
    return records


def _print_human(plan_result, handoff: dict, market: Optional[str]) -> None:
    if market:
        evidence = market_evidence(plan_result, market)
        if "error" in evidence:
            print(evidence["error"])
            return
        print(f"【{market}】{evidence['verdict']}")
        print(f"  订单原文：{evidence['order_text']}")
        print(f"  判定：{evidence['reason']}")
        for item in evidence["evidence"]:
            weight = f"{item['kg_low']}"
            if item["kg_high"] != item["kg_low"]:
                weight += f"–{item['kg_high']}"
            print(
                f"  - {item['lot_id']}｜{item['role']}｜{item['product']}｜"
                f"{weight}kg｜过秤={_tri(item['weighed'])}｜暂留={item['tentative']}"
            )
            print(f"      报价原文：{item['quote_text']}（来源 {item['quote_record']}）")
            for condition in item["conditions"]:
                state = "待确认" if condition["open"] else "已满足"
                print(
                    f"      条件[{state}] {condition['topic']} → "
                    f"{condition['confirm_with']}：{condition['text']}"
                )
        if evidence["released"]:
            print("  已释放批次：")
            for item in evidence["released"]:
                print(f"    {item['lot_id']} → {item['duplicate_of']}：{item['note']}")
        return

    print("=== 当班承诺 ===")
    for name, market_plan in plan_result.market_plans.items():
        print(f"【{name}】{market_plan.verdict.value}：{market_plan.reason}")
        print(
            f"  锁定 {market_plan.locked_kg}kg｜已交割 {market_plan.delivered_kg}kg｜"
            f"已确认 {market_plan.confirmed_kg}kg｜有条件 {market_plan.conditional_kg}kg｜"
            f"缺 {market_plan.shortage_kg}kg"
        )
        for allocation in market_plan.allocations:
            conds = [c for c in allocation.conditions if c.open]
            print(
                f"  - {allocation.lot_id}（{allocation.role}）"
                f"{allocation.kg_low}kg"
                + (f"–{allocation.kg_high}kg" if allocation.kg_high != allocation.kg_low else "")
                + (f"，{allocation.packages} 件" if allocation.packages else "")
                + ("；待确认：" + "；".join(c.text for c in conds) if conds else "")
            )
        for condition in market_plan.conditions:
            print(f"    ! {condition.topic} → {condition.confirm_with.value}：{condition.text}")
        for condition in market_plan.advisory_conditions:
            print(f"    · {condition.topic} → {condition.confirm_with.value}：{condition.text}")

    print("\n=== 装车指令 ===")
    for instruction in plan_result.load_instructions:
        print(
            f"- [{instruction.action}] {instruction.lot_id}"
            f" → {instruction.market or '未定'}：{instruction.note}"
        )

    print("\n=== 交接摘要 ===")
    print(f"可以发：{len(handoff['can_send'])} 项")
    for item in handoff["can_send"]:
        print(f"  + {item['lot']} → {item['market']} {item['kg']}：{item['note']}")
    print(f"要等/先别发：{len(handoff['hold'])} 项")
    for item in handoff["hold"]:
        print(f"  - {item['lot']}（{item['action']}，找 {item['confirm_with']}）：{item['note']}")
    if handoff["conflicts"]:
        print("冲突选项：")
        for item in handoff["conflicts"]:
            print(f"  ! {item['message']}")
    if plan_result.issues:
        print("\n需人工确认的指代/表述：")
        for issue in plan_result.issues:
            print(f"  ? [{issue.kind}] {issue.message}（找 {issue.confirm_with.value}）")
    if plan_result.ignored_duplicates:
        print("\n重复导入（未重复计数）：")
        for item in plan_result.ignored_duplicates:
            print(f"  = {item['id']}：{item['reason']}")


def _tri(value) -> str:
    if value is None:
        return "未说明"
    return "是" if value else "否"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m joint_supply.cli",
        description="重复导入报价对话和装车回单，确定当班哪些承诺可以一起兑现。",
    )
    parser.add_argument("path", help="语料 JSON（含 records 数组）")
    parser.add_argument("--market", help="只看某个场，并按场回查报价原文")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    parser.add_argument(
        "--explain",
        help="解释一句含否定词的话，例如“不要细叶外的”或“不保证细叶”",
    )
    parser.add_argument(
        "--role",
        default="buyer",
        choices=[role.value for role in Role],
        help="配合 --explain 使用：说话方角色",
    )
    args = parser.parse_args(argv)

    if args.explain:
        result = explain_statement(args.explain, args.role, args.market)
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"原文：{result['text']}（说话方：{result['role']}）")
            for statement in result["statements"]:
                print(
                    f"- {statement['kind']} / {statement['semantics']}："
                    f"{statement.get('target', '')}；{statement['note']}"
                )
        return 0

    path = Path(args.path)
    if not path.exists():
        print(f"找不到文件：{path}", file=sys.stderr)
        return 2
    try:
        records = load_records(path)
    except (ValueError, json.JSONDecodeError) as error:
        print(f"读取失败：{error}", file=sys.stderr)
        return 2

    parsed = parse_records(records)
    plan_result = plan(parsed)
    handoff = summarize_handoff(plan_result)

    if args.as_json:
        if args.market:
            payload = market_evidence(plan_result, args.market)
        else:
            payload = plan_result.as_dict()
            payload["handoff"] = handoff
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.market:
        _print_human(plan_result, handoff, args.market)
    else:
        _print_human(plan_result, handoff, None)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
