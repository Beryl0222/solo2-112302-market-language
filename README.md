# 联合供货报价里的替代承诺与装车交接

面向“菜场对话、报价和冷链装车回单”的 Python 工具。它可以**重复导入**报价对话
和装车回单，把文字里的品种、净重、箱数、可混装条件、价差上限、到场窗口和数量
下界读出来，然后回答采购跟单当班最关心的问题：

- 哪些承诺现在就能一起兑现？
- 哪些要等某一方确认，找谁确认？
- 已装的可不可以改给别场？缺量由谁承认？
- 报价里那句“那批”到底指哪一批？

仅依赖 Python 标准库，Python 3.9+。

## 快速开始

```bash
# 当班总表（data/corpus.json 是练习语料）
python3 -m joint_supply.cli data/corpus.json

# 两场争用同一批货的示例：会给出“改给谁、另一场缺多少”的取舍
python3 -m joint_supply.cli data/scenario_shared_lots.json

# 按场回查已确认的规格和报价原文
python3 -m joint_supply.cli data/corpus.json --market 西场

# JSON 输出，便于接进别的系统
python3 -m joint_supply.cli data/corpus.json --json

# 解释一句含否定词的话：采购“不要…外”与供方“不保证”不是一回事
python3 -m joint_supply.cli data/corpus.json --explain "不保证细叶" --role supplier

# 运行测试
python3 -m unittest discover -s tests -v
```

Python API：

```python
from joint_supply import parse_records, plan, summarize_handoff, market_evidence

parsed = parse_records(records)          # records 是 [{"id","kind","text","refs"?}, ...]
result = plan(parsed)
handoff = summarize_handoff(result)      # can_send / hold / acknowledgements / conflicts
west = market_evidence(result, "西场")    # 按场回查规格、报价原文、来源
```

## 输入格式

```json
{
  "records": [
    {"id": "g1", "kind": "glossary",     "text": "名称表|东场的青芯是白梗菜，西场的青芯是辣味短梗，袋装按磅，整筐按实秤公斤。"},
    {"id": "o1", "kind": "order",        "text": "东场订单|按上次白梗的青芯。能少一筐但净重底线九十公斤，不换菜，必须今晚到。"},
    {"id": "q1", "kind": "quote",        "text": "档口|A7 东场品两筐净重四十五公斤一筐，B7 西场青芯三袋，每袋 15–20 公斤。"},
    {"id": "l1", "kind": "load",         "text": "车队签收|A7 已装第一筐给东场，司机封箱。B7 尚无过秤净重。"},
    {"id": "c1", "kind": "correction",   "text": "档口补记|q2 说的其实还是 A7 装车后留下的一筐。C7 另有低根品，实秤二十公斤但明早才到。"},
    {"id": "n1", "kind": "confirmation", "text": "西场确认|接受混装。"},
    {"id": "i1", "kind": "inquiry",      "text": "档口|F1 短梗一筐，是否要？"}
  ]
}
```

`kind` 的作用严格区分：

| kind | 作用 |
| --- | --- |
| `glossary` | 场的名称表、规格词汇、秤单单位；决定同词异义的指向 |
| `order` | 场方硬约束：品种、净重下界、筐数让步、排除/只接受、替代份额与价差 |
| `quote` | 供方可供货与条件；批次号用 `A7` / `B7` 这类标记 |
| `correction` | 行内补正：只改指向/数量，不新增承诺；用 `q2`、`A7` 明确引用 |
| `load` | 装车回单 / 签收；已装事实与过秤重量分开记录 |
| `confirmation` | 订单确认：只关闭已存在的条件，不创造新供给 |
| `inquiry` | 有条件询盘：未形成承诺，不进组合 |

## 业务规则（可核对）

这些规则写在 `joint_supply/planner.py` 顶部与 `Plan.metadata["rules"]` 里。

1. **区间不取中点。** 袋装只给 `15–20 公斤` 时，3 袋记为 `45–60kg`，计入承诺
   的只有下界 `45kg`；上界永远不会被当成“已确认”。
2. **只有件数不算重量。** “两筐”没有每筐净重时只记件数，公斤数为 0，绝不猜。
3. **同词异义按场判。** “青芯”在东场是白梗、在西场是短梗；缺场名时不强行归属。
4. **采购“不要细叶外的” ≠ 供方“不保证细叶”。**
   - 采购：`only_accept`，属订单硬约束，不需要供方确认；
   - 供方：`risk_notice`，是风险提示，必须找**采购**确认是否接受。
5. **已装部分照旧承担承诺。** 装车回单里已装的货按回单锁定给该场；即使该场
   “愿意让量”，也不能把已确认净重改划别场。未过秤的已装货不算“已交割”，
   只算“已装”，要车队实秤。
6. **让筐不等于让净重。** 订单“能少一筐”只放宽件数；净重底线（如 90kg）不变。
7. **替代受三重约束。** 品种必须在订单允许清单内、替代量不超过份额上限、价差
   不超过每公斤上限。规格只“部分一致”（同一茎型、根型不同）时，不静默当成
   同一品种，而是加一条“需买方确认”的阻断条件。缺单价时无法核对价差，找供方。
8. **更正只改指向。** 供方说“q2 其实就是 A7 剩下的一筐”时，`q2` 被释放为
   `A7` 的别名，不重复计供给；A7 的剩余实物归原报价规格，若要当别场替代必须
   重新确认品种（会在 `issues` 里报 `product_label_conflict`）。
9. **暂留批次先不占组合。** 指代不明的“那批”只作暂留；某场缺量时，规划器
   单独做第二轮搜索，把暂留批次移出寻找能共存的组合，但保留“指向未确认”这一
   阻断条件，装车指令为 `hold` 而不是 `load_now`。
10. **重复导入不重复计数。** 同一 `id` 的记录再次导入只保留首次原文；重复的批次号
    也不会产生第二份供给，`ignored_duplicates` 会说明。
11. **两场争同一批货时给取舍。** 搜索会先让更多场达到下限，再比较覆盖量、已确认
    量、替代用量；无法两全时在 `conflicts` 里写清“改给谁、另一场会缺多少”。

## 为什么这样判（几个关键取舍）

- **采购与供方的否定不能互换。** 采购说“不要细叶外的”是只接受细叶（订单硬约束）；
  供方说“不保证细叶”是风险提示，必须回头找采购确认是否接受。工具用
  `--explain` 或 `explain_statement()` 显式区分这两类。
- **让量不等于让净重。** “能少一筐”只放宽件数，订单净重下界不变；已确认净重
  不能因为某场“愿意让”就改划别场。
- **整件优先，拆件要人认可。** 替代份额只够半筐时，工具按上限给出“拆件”选项，
  但一定附一条需买方确认的条件，而不是自行取整。
- **更正句逐句处理。** 一个补正记录里可以有两句话指向两个不同批次；工具逐句
  改指向，不会把整段共用一个目标。指回同一批实物后，旧条目只作别名释放。
- **未过秤 ≠ 已交割。** 装车回单写“已装、未过秤”时，只锁“已装部分承担到场
  承诺”，公斤数仍等车队实称；`delivered_kg` 与 `locked_kg` 分开统计。

## 输出里的判定

每个场会得到 `confirmed` / `conditional` / `blocked` / `delivered` 之一：

- `confirmed`：已确认（含已过秤签收）达到下单净重；
- `conditional`：有货但缺条件，`conditions` 里列明找 `buyer` / `supplier` / `carrier`；
- `blocked`：即使条件全满足也不够，`shortage_kg` 给出缺口；
- `delivered`：已过秤签收达到下限。

装车指令 `load_instructions` 的 `action`：

| action | 含义 |
| --- | --- |
| `load_now` | 现在可以发，条件已满足 |
| `weigh_first` | 先过秤，再按实重交接 |
| `hold` | 先别发，等特定一方确认 |
| `do_not_load` | 该批次已被更正释放，不是独立供给 |
| `do_not_reassign` | 已装货锁定给本场，不能卸下重配 |

`market_evidence(plan, "西场")` 会逐条返回该场用到的批次：规格、数量、是否过秤、
条件、**报价原文**与来源记录 id，便于跟单回查。

## 目录

```
joint_supply/
  lexicon.py   中文数字、单位、数量区间、否定作用域、品种别名
  model.py     数据模型、单位换算、结果序列化
  parser.py    原始记录 → 结构化数据；名称表、订单、报价、更正、装车、确认
  planner.py   多场联合搜索、条件归属、装车指令、交接摘要、按场回查
  cli.py       命令行入口
data/
  corpus.json               合成演练语料（8 条）
  scenario_shared_lots.json 两场争用同一批货的示例
tests/
  test_joint_supply.py  业务规则回归测试
```

全部数据均为合成演练文本，不含真实个人、机构或服务响应。
