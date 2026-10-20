"""질문 난이도를 문헌의 기준으로 매긴다 (LLM 없이, 규칙으로만).

1) SQL 난이도 (difficulty): Spider 의 hardness 기준을 그대로 옮겼다. easy / medium / hard / extra
   출처: Yu et al., "Spider: A Large-Scale Human-Labeled Dataset for Complex and Cross-Domain Semantic Parsing and
         Text-to-SQL Task", EMNLP 2018 (SQL Hardness Criteria) 와 공식 평가 코드 evaluation.py 의 eval_hardness.
   정답 SQL 에서 세 가지를 센다.
     c1 (SQL 요소): WHERE, GROUP BY, ORDER BY, LIMIT 각각 1 + 조인 수(FROM 의 테이블 수 - 1)
                    + OR 연결자 개수 + LIKE 조건 개수
     c2 (중첩)    : 조건(WHERE / HAVING / ON) 안의 서브쿼리 수 + INTERSECT / EXCEPT / UNION 수
     co (그 밖)   : 집계 함수 2개 이상 / SELECT 항목 2개 이상 / WHERE 조건 2개 이상 / GROUP BY 컬럼 2개 이상, 각각 1 (0~4)
   등급 (eval_hardness 와 같은 조건):
     easy   : c1 <= 1, co == 0, c2 == 0
     medium : c2 == 0 이고, (co <= 2 이고 c1 <= 1) 또는 (c1 <= 2 이고 co <= 1)
     hard   : (c2 == 0 이고 ((co > 2 이고 c1 <= 2) 또는 (c1 == 3 이고 co <= 2))) 또는 (c1 <= 1, co == 0, c2 == 1)
     extra  : 나머지
   Spider 는 자체 SQL 파서를 쓰고 여기서는 sqlglot 으로 같은 요소를 세므로 경계 사례에서 달라질 수 있다.
   (FROM 절의 파생 테이블 SELECT 는 Spider 처럼 c2 에 세지 않는다.)


실행: python "data/2. query_filter/steps/question_difficulty.py"   → data/difficulty.json  (질문 id → {difficulty, c1, c2, co})
"""
import json
import re
from collections import Counter
from pathlib import Path

import sqlglot
from sqlglot import exp

DATA = Path(__file__).resolve().parents[2]      # data/
QUESTIONS = DATA / "questions.json"
OUT = DATA / "difficulty.json"
SET_OPS = tuple(c for c in (getattr(exp, n, None) for n in ("Union", "Except", "Intersect")) if c)


def own(node):
    """node 아래의 요소를 돌려주되, 안쪽 SELECT(서브쿼리)로는 들어가지 않는다."""
    if node is None:
        return iter(())
    return node.walk(prune=lambda n: isinstance(n, exp.Select) and n is not node)


def count_nested(select):
    """조건(WHERE / HAVING / JOIN ON) 안에 있는 서브쿼리 수. FROM 의 파생 테이블은 세지 않는다 (Spider 와 같음)."""
    clauses = [select.args.get("where"), select.args.get("having")] + [j.args.get("on") for j in select.args.get("joins", [])]
    n = 0
    for clause in clauses:
        if clause is not None:
            n += sum(1 for node in clause.walk(prune=lambda x: isinstance(x, exp.Select)) if isinstance(node, exp.Select))
    return n


def flat_units(e):
    """AND / OR 로 이어진 조건을 낱개로 풀어 센다."""
    if isinstance(e, (exp.And, exp.Or)):
        return flat_units(e.left) + flat_units(e.right)
    if isinstance(e, exp.Paren) and isinstance(e.this, (exp.And, exp.Or)):
        return flat_units(e.this)
    return [e]


def hardness_counts(sql):
    tree = sqlglot.parse_one(sql, read="sqlite")
    n_set_ops = 0
    while isinstance(tree, SET_OPS):          # INTERSECT / EXCEPT / UNION: 왼쪽 쿼리를 본체로 보고 연산 수를 c2 에 더한다
        n_set_ops += 1
        tree = tree.this
    s = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    where, having, group = s.args.get("where"), s.args.get("having"), s.args.get("group")
    order, limit, joins = s.args.get("order"), s.args.get("limit"), s.args.get("joins", [])

    conds = [c for c in (where, having) if c is not None] + [j.args["on"] for j in joins if j.args.get("on") is not None]
    n_or = sum(1 for c in conds for n in own(c) if isinstance(n, exp.Or))
    n_like = sum(1 for c in conds for n in own(c) if isinstance(n, (exp.Like, exp.ILike)))
    c1 = sum(x is not None for x in (where, group, order, limit)) + len(joins) + n_or + n_like
    c2 = count_nested(s) + n_set_ops

    parts = list(s.expressions) + [x for x in (where, group, order, having) if x is not None]
    n_agg = sum(1 for p in parts for n in own(p) if isinstance(n, exp.AggFunc))
    n_where = len(flat_units(where.this)) if where is not None else 0
    co = sum([n_agg > 1, len(s.expressions) > 1, n_where > 1, group is not None and len(group.expressions) > 1])
    return c1, c2, co


def spider_level(c1, c2, co):
    if c1 <= 1 and co == 0 and c2 == 0:
        return "easy"
    if (co <= 2 and c1 <= 1 and c2 == 0) or (c1 <= 2 and co < 2 and c2 == 0):
        return "medium"
    if (co > 2 and c1 <= 2 and c2 == 0) or (2 < c1 <= 3 and co <= 2 and c2 == 0) or (c1 <= 1 and co == 0 and c2 <= 1):
        return "hard"
    return "extra"


def main():
    qs = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    seq, out = Counter(), {}
    for q in qs:
        i = seq[q["db_id"]]
        seq[q["db_id"]] += 1
        qid = f"{q['db_id']}-{i}"
        try:
            c1, c2, co = hardness_counts(q["SQL"])
            out[qid] = {"db_id": q["db_id"], "difficulty": spider_level(c1, c2, co), "c1": c1, "c2": c2, "co": co}
        except Exception as e:
            out[qid] = {"db_id": q["db_id"], "difficulty": "unknown", "error": str(e)[:80]}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"질문 {len(out)}개 → {OUT}")
    for db in sorted({v["db_id"] for v in out.values()}):
        c = Counter(v["difficulty"] for v in out.values() if v["db_id"] == db)
        n = sum(c.values())
        print(f"  [SQL 난이도] {db:9} " + "  ".join(f"{k} {c[k]}({100 * c[k] / n:.0f}%)" for k in ("easy", "medium", "hard", "extra", "unknown") if c[k]))


if __name__ == "__main__":
    main()
