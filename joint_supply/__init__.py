"""联合供货报价与装车交接的当班决策助手。

模块分工：

* ``lexicon``  —— 中文数字、单位、数量区间、否定作用域、品种别名；
* ``model``    —— 记录、订单、报价批次、装车行、承诺结果等数据模型；
* ``parser``   —— 把原始记录解析成结构化数据（保守、可追溯）；
* ``planner``  —— 在多个场之间搜索可共存的承诺组合，输出装车指令与交接摘要；
* ``cli``      —— 重复导入报价对话与装车回单，打印当班清单。

所有业务规则都写在 ``joint_supply/planner.py`` 模块顶部和 ``README`` 中，
便于跟单核对，而不是藏在散落的正则里。
"""
from .lexicon import (
    cn_to_decimal,
    find_negations,
    parse_quantity,
    parse_range,
)
from .model import (
    Condition,
    Kind,
    MarketOrder,
    Plan,
    QuoteLot,
    Role,
    Unit,
    Verdict,
)
from .parser import Parsed, describe_statement, parse_records
from .planner import (
    explain_statement,
    market_evidence,
    plan,
    summarize_handoff,
)

__all__ = [
    "Condition",
    "Kind",
    "MarketOrder",
    "Parsed",
    "Plan",
    "QuoteLot",
    "Role",
    "Unit",
    "Verdict",
    "cn_to_decimal",
    "describe_statement",
    "explain_statement",
    "find_negations",
    "market_evidence",
    "parse_quantity",
    "parse_range",
    "parse_records",
    "plan",
    "summarize_handoff",
]

__version__ = "0.1.0"
