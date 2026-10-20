"""에이전트 결과를 정답과 나란히 놓고, 기계적으로 채점해 jsonl / csv 에 쌓는다.

- 최종 정답(final) = (1) 에이전트 SQL 의 실행 결과가 BIRD 정답 결과와 같다(엄격 EX: 행과 컬럼이 정확히 같음) 이거나
  (2) 에이전트의 **자연어 답변** 글에 정답 값이 들어 있다. 둘 중 하나면 정답이다. (2)의 확인 방법: 정답 결과의 값(행마다 모든 값)이 답변 글 안에 들어 있는가를 규칙으로 확인한다 (LLM 판정 아님).
  숫자는 쉼표·$·% 를 무시하고 반올림(0.01 또는 0.5%) 차이를 허용, 글자는 대소문자·공백을 무시하고 포함 여부를 본다.
  모든 정답 행이 들어 있으면 correct, 절반 이상이면 partial, 아니면 wrong.
- (참고) SQL 결과 기준 판정(ex, ex_value)도 같이 남기지만 요약 점수에는 쓰지 않는다.
- 사람이 직접 판단할 수 있게 질문, 정답 결과, 에이전트의 자연어 답변, 에이전트 SQL 의 실행 결과를 나란히 보여 준다.
- LLM 판정은 기본으로 끈다 (USE_LLM_JUDGE). 켜면 답변이 맞는지를 LLM 이 말로 설명하는 열이 추가로 채워진다.
- 정답 SQL 자체가 이 DB 에서 오류가 나면 그 질문은 점수 계산에서 뺀다 (ex = gold_error).
- 채점 결과는 log/<DB>/evaluation.jsonl 에 한 줄씩 **이어 붙인다**. 이미 채점한 결과 줄은 다시 채점하지 않는다.
- evaluation.csv 는 jsonl 에서 매번 새로 만든다 (엑셀 등으로 열어 보는 용도).

실행:
    python evaluate.py          새로 생긴 결과 줄만 채점하고, 방금 채점한 것을 나란히 출력
    python evaluate.py show     지금까지 채점한 전체를 나란히 출력 (채점은 안 함)
"""
import csv
import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from itertools import combinations, permutations
from pathlib import Path

from db import DB_NAME, connect

LOG_DIR = Path("log") / DB_NAME
EVAL_JSONL = LOG_DIR / "evaluation.jsonl"
EVAL_CSV = LOG_DIR / "evaluation.csv"
DIFFICULTY = Path("data") / "difficulty.json"   # python question_difficulty.py 가 만든 질문 난이도 (Spider hardness)
GOLD_PG = Path("data") / f"gold_pg_{DB_NAME}.json"   # 2. query_filter/filter.py 가 만든 PostgreSQL 용 정답
USE_LLM_JUDGE = False                 # True 면 자연어 답변을 LLM 이 판정하고 이유를 말로 적는다 (API 비용)
JUDGE_MODEL = "claude-haiku-5-5"      # LLM 판정을 켰을 때의 모델
SHOW_ROWS = 30                        # 저장·표시할 결과 행 수 (정답 결과, 에이전트 결과 각각)

FIELDS = ["evaluated_at", "db", "condition", "kind", "id", "run_id", "model",
          "difficulty", "sp_c1", "sp_c2", "sp_co", "question", "evidence", "gold_sql", "gold_sql_pg", "gold_result", "final_sql", "agent_result", "answer",
          "final", "final_basis", "ans", "ans_note", "ex", "ex_note", "ex_value", "ex_value_note", "llm_verdict", "llm_reason", "judge_model",
          "gold_rows", "pred_rows", "steps", "sparql_calls", "seconds", "error"]

JUDGE_PROMPT = (
    "You grade an AI agent's natural-language answer to a question about a database. "
    "You get the question, the CORRECT result (rows from the reference SQL), and the agent's answer. "
    "The answer is correct if it states the same facts as the correct result. Harmless extra "
    "information is fine; wrong values or missing items are not. If the correct result is shown only "
    "partly (first rows of a long list), judge by the shown rows and the total row count. "
    "If the answer only gives a count when a list was asked for (or the reverse), it is incorrect. "
    "Reply with JSON only: {\"verdict\": \"correct\" | \"partial\" | \"incorrect\", "
    "\"reason\": \"one or two sentences in Korean explaining what matches or what is wrong\"}."
)


def cell(v):
    """값을 비교하기 좋은 형태로: 숫자는 소수 6자리까지, 나머지는 문자열."""
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return round(float(v), 6)
    return None if v is None else str(v).strip()


def run(sql):
    """(행 목록, 컬럼 이름들, 오류) — 행 수 제한 없이 전부 가져온다."""
    if not sql:
        return None, [], "no_sql"
    try:
        with connect() as con:
            res = con.execute(sql)
            if res.description is None:
                return None, [], "no result set"
            return res.fetchall(), [d.name for d in res.description], None
    except Exception as e:
        return None, [], str(e).splitlines()[0]


def as_set(rows):
    return {tuple(cell(v) for v in r) for r in rows}


def has_extra_columns(pred_rows, pred_n, gold_set, gold_n):
    """예측에 컬럼이 더 있고, 그중 일부만 보면 정답과 같은가 (참고용 메모)."""
    if pred_n <= gold_n or pred_n > 8:
        return False
    return any({tuple(cell(r[i]) for i in idx) for r in pred_rows} == gold_set
               for idx in combinations(range(pred_n), gold_n))


def result_text(res, limit=SHOW_ROWS):
    """(행, 컬럼, 오류) → 사람이 읽는 텍스트."""
    rows, cols, err = res
    if err:
        return f"(오류: {err[:120]})"
    head = "\t".join(cols) + "\n" + "\n".join("\t".join(str(v) for v in r) for r in rows[:limit])
    more = f"\n... (총 {len(rows)}행 중 {limit}행만 표시)" if len(rows) > limit else f"\n(총 {len(rows)}행)"
    return head + more


def close(a, b):
    """숫자는 반올림 차이(0.01 또는 0.5%) 안이면 같은 값으로 본다."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    return abs(a - b) <= max(0.01, 0.005 * max(abs(a), abs(b)))


def rows_close(P, G):
    """두 행 집합이 숫자 반올림 차이만 빼고 같은가."""
    if len(P) != len(G):
        return False
    P, G = sorted(P, key=str), sorted(G, key=str)
    return all(len(p) == len(g) and all(close(x, y) for x, y in zip(p, g)) for p, g in zip(P, G))


def value_grade(gold, pred):
    """값이 맞는지만 본다. 컬럼 이름, 순서, 질문에 없는 추가 컬럼, 숫자 반올림은 무시한다.

    correct  : 정답의 값(행 × 컬럼)을 에이전트 결과에서 그대로 찾을 수 있다 (정확히 같은 행들)
    contains : 정답 행을 모두 포함하지만 행이 더 있다 (동점 전부 반환, 목록이 더 길다 등)
    wrong    : 그 밖 (값이 다르거나, 정답의 일부만 있거나, 컬럼이 모자람)
    """
    g_rows, g_cols, g_err = gold
    p_rows, p_cols, p_err = pred
    if g_err:
        return "gold_error", ""
    if p_err:
        return "wrong", f"에이전트 SQL 오류: {p_err[:60]}"
    G, gn, pn = as_set(g_rows), len(g_cols), len(p_cols)
    if pn < gn:
        return "wrong", f"컬럼이 더 적음 (정답 {gn}열, 에이전트 {pn}열)"
    contains = False
    for idx in combinations(range(pn), gn):
        for perm in (permutations(idx) if gn <= 3 else [idx]):
            P = {tuple(cell(x[k]) for k in perm) for x in p_rows}
            if P == G or rows_close(P, G):
                return "correct", "" if pn == gn else f"추가 컬럼 {pn - gn}개 무시"
            if G < P:
                contains = True
    if contains:
        return "contains", "정답 행을 모두 포함하지만 행이 더 있음"
    return "wrong", ""


def ex_grade(gold, pred):
    """(ex 판정, 메모)"""
    g_rows, g_cols, g_err = gold
    p_rows, p_cols, p_err = pred
    if g_err:
        return "gold_error", g_err[:80]
    if p_err:
        return "wrong", f"에이전트 SQL 오류: {p_err[:60]}"
    g, p = as_set(g_rows), as_set(p_rows)
    if g == p:
        return "correct", ""
    if has_extra_columns(p_rows, len(p_cols), g, len(g_cols)):
        return "wrong", "값은 맞지만 컬럼이 더 많음"
    if len(p_cols) == len(g_cols) and len(g) != len(p):
        return "wrong", f"행 수 다름 (정답 {len(g)}, 예측 {len(p)})"
    return "wrong", f"컬럼 {len(p_cols)}개 (정답 {len(g_cols)}개), 행 {len(p)}개 (정답 {len(g)}개)"


NUM_RE = re.compile(r"(?<![\w.-])-?\d[\d,]*(?:\.\d+)?")


def answer_numbers(text):
    """답변 글에 나온 숫자들 (8,212 → 8212). 날짜(2015-08-19)의 조각은 숫자로 세지 않는다."""
    out = []
    for m in NUM_RE.finditer(text):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def norm_text(t):
    return " ".join(str(t).lower().replace("*", " ").split())


def cell_in_answer(v, text, nums):
    """정답 값 하나가 답변 글에 들어 있는가."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return True                                           # 빈 값은 따지지 않는다
    num = None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        num = float(v)
    elif re.fullmatch(r"-?(0|[1-9]\d*)(\.\d+)?", str(v).replace(",", "").strip()):   # 숫자 모양 글자 ('2006'); 앞자리가 0인 값('0123')은 글자로 본다
        num = float(str(v).replace(",", ""))
    if num is None:
        return norm_text(v) in text
    if num.is_integer():
        return any(x == num for x in nums)
    return any(close(x, num) or (abs(num) <= 1 and close(x, num * 100)) for x in nums)   # 0.123 ↔ 12.3%


def answer_grade(gold, answer):
    """(ans 판정, 메모). 정답 결과의 행이 답변 글에 얼마나 들어 있는가.

    correct : 정답 행이 모두 들어 있다 (행마다 그 행의 값이 전부 답변에 있음)
    partial : 정답 행의 절반 이상
    wrong   : 그 밖 (답변 없음 포함)
    gold_empty : 정답이 빈 결과라 글로는 확인할 수 없어 점수에서 뺀다"""
    g_rows, g_cols, g_err = gold
    if g_err:
        return "gold_error", g_err[:80]
    G = sorted(as_set(g_rows), key=str)
    if not G:
        return "gold_empty", "정답이 빈 결과"
    if not (answer or "").strip():
        return "wrong", "답변 없음"
    text, nums = norm_text(answer), answer_numbers(answer)
    hit = sum(all(cell_in_answer(v, text, nums) for v in row) for row in G)
    note = f"정답 행 {hit}/{len(G)}개가 답변에 있음"
    if hit == len(G):
        weak = len(G) == 1 and len(G[0]) == 1 and isinstance(G[0][0], float) and abs(G[0][0]) <= 10 and float(G[0][0]).is_integer()
        return "correct", "정답이 작은 숫자라 우연히 일치했을 수 있음" if weak else note
    return ("partial" if 2 * hit >= len(G) else "wrong"), note


def final_grade(r):
    """(final 판정, 근거). 정답 = SQL 결과가 정답과 같다(엄격 EX) 또는 답변 글에 정답 값이 들어 있다."""
    if r["ex"].startswith("gold_") or str(r.get("ans", "")).startswith("gold_"):
        return str(r.get("ans") or r["ex"]), ""
    sql_ok, ans_ok = r["ex"] == "correct", r.get("ans") == "correct"
    if sql_ok and ans_ok:
        return "correct", "SQL 결과 일치 + 답변에 정답 포함"
    if sql_ok:
        return "correct", "SQL 결과만 일치 (답변 글에는 정답이 다 없음)"
    if ans_ok:
        return "correct", "답변에 정답 포함 (SQL 결과는 다름)"
    return ("partial" if r.get("ans") == "partial" else "wrong"), r.get("ans_note", "")


def llm_judge(llm, rec, gold_text):
    """(선택) 자연어 답변을 정답 결과와 비교해 (verdict, reason) 을 돌려준다."""
    user = (f"Question: {rec['question']}\n"
            f"Hint given to the agent: {rec.get('evidence') or '(none)'}\n\n"
            f"CORRECT result:\n{gold_text}\n\n"
            f"Agent's answer:\n{rec.get('answer') or '(no answer)'}")
    try:
        out = llm.invoke([("system", JUDGE_PROMPT), ("user", user)]).content
        if isinstance(out, list):
            out = "".join(b.get("text", "") for b in out if isinstance(b, dict))
        d = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
        v = str(d.get("verdict", "")).lower()
        return (v if v in ("correct", "partial", "incorrect") else "unknown"), str(d.get("reason", ""))
    except Exception as e:
        return "unknown", f"판정 실패: {str(e)[:80]}"


def load_gold():
    """filter.py(steps/gold_to_pg)의 결과. 없으면 원래 정답 SQL 을 그대로 쓴다."""
    return json.loads(GOLD_PG.read_text(encoding="utf-8")) if GOLD_PG.exists() else {}


def gold_for(rec, gold_map):
    """(채점에 쓸 정답 SQL, 채점에서 뺄 때의 판정, 이유).

    변환된 정답이 ok 면 그 SQL 을 쓰고, unstable / unresolved 면 점수에서 뺀다."""
    g = gold_map.get(rec["id"])
    if g is None:
        return rec["gold_sql"], None, None
    if g["status"] == "ok":
        return g["sql"], None, None
    return None, ("gold_unstable" if g["status"] == "unstable" else "gold_error"), g["note"]


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def row_key(r):
    return (r["condition"], r["id"], r.get("run_id"))


def attach_difficulty(rows):
    """질문 난이도(question_difficulty.py)를 각 행에 붙인다. 파일이 없으면 그대로 둔다.

    difficulty : Spider hardness (easy / medium / hard / extra),  sp_c1 / sp_c2 / sp_co : 그 계산에 쓴 세 개수"""
    if not DIFFICULTY.exists():
        return rows
    d = json.loads(DIFFICULTY.read_text(encoding="utf-8"))
    for r in rows:
        info = d.get(r["id"], {})
        r["difficulty"] = info.get("difficulty", "-")
        r["sp_c1"], r["sp_c2"], r["sp_co"] = info.get("c1", ""), info.get("c2", ""), info.get("co", "")
    return rows


def latest(rows):
    """같은 (조건, 질문)이 여러 줄이면 가장 나중 줄만 남긴다 (실패해서 다시 돌린 질문의 이전 줄을 빼기 위해). 최종 판정(final)도 붙인다."""
    out = {}
    for r in rows:
        out[(r["condition"], r["id"])] = r
    for r in out.values():
        if "ans" in r:
            r["final"], r["final_basis"] = final_grade(r)
    return list(out.values())


def show(rows):
    """질문별로 정답과 각 조건의 결과를 나란히 출력한다 (사람이 직접 판단하는 용도)."""
    ids = sorted({r["id"] for r in rows}, key=lambda s: int(s.rsplit("-", 1)[1]))
    for qid in ids:
        rs = sorted((r for r in rows if r["id"] == qid), key=lambda r: r["condition"])
        print("\n" + "=" * 78)
        print(f"{qid} | {rs[0]['question']}")
        print(f"힌트: {rs[0].get('evidence') or '-'}")
        print(f"\n[정답 SQL] {rs[0]['gold_sql']}")
        print("[정답 결과]\n" + "\n".join("   " + l for l in (rs[0].get("gold_result") or "").splitlines()))
        for r in rs:
            tag = {"correct": "정답", "partial": "부분(정답 행 절반 이상이 답변에 있음)", "wrong": "오답", "gold_error": "채점 불가(정답 SQL 오류)",
                   "gold_unstable": "채점 불가(정답이 불안정)", "gold_empty": "채점 불가(정답이 빈 결과)"}.get(r.get("final"), r.get("final", "-"))
            print(f"\n--- {r['condition']} | {r['model']} | 도구 {r['steps']}번(SPARQL {r.get('sparql_calls', r.get('ontology_calls', 0))}번) | {r['seconds']}초 | 판정: {tag}"
                  + (f" ({r['final_basis']})" if r.get("final_basis") else "") + f" | SQL 결과: {r['ex']} | 답변: {r.get('ans', '-')}")
            print("[에이전트 답변]\n" + "\n".join("   " + l for l in (r.get("answer") or "(없음)").strip().splitlines()[:12]))
            print("[에이전트 SQL] " + " ".join((r.get("final_sql") or "(없음)").split()))
            print("[에이전트 SQL 실행 결과]\n" + "\n".join("   " + l for l in (r.get("agent_result") or "").splitlines()[:12]))
            if r.get("llm_verdict", "-") != "-":
                print(f"[LLM 판정] {r['llm_verdict']} — {r['llm_reason']}")


def graded(rows):
    """점수에 쓰는 행: 정답 SQL 문제(gold_error / gold_unstable)나 빈 정답(gold_empty)은 뺀다."""
    return [r for r in rows if not r["ex"].startswith("gold_") and not str(r.get("ans", "")).startswith("gold_")]


def summary(rows):
    print("\n요약 (정답이 불안정·오류·빈 결과인 질문은 점수에서 제외)")
    print("  정답   : (1) 에이전트 SQL 결과가 정답과 같다(엄격 EX) 또는 (2) 답변 글에 정답이 들어 있다 — 둘 중 하나")
    print("  근거   : 정답 중 SQL 결과로만 / 답변으로만 / 둘 다로 인정된 수")
    print(f"{'조건':36}{'질문':>5}{'채점':>5}{'정답':>6}{'정답률':>7}{'SQL만':>7}{'답변만':>7}{'둘다':>6}{'평균초':>7}{'도구호출':>8}")
    for cond in sorted({r["condition"] for r in rows}):
        v = [r for r in rows if r["condition"] == cond]
        gr = graded(v)
        val = sum(r.get("final") == "correct" for r in gr)
        only_sql = sum(r.get("final") == "correct" and r["ex"] == "correct" and r.get("ans") != "correct" for r in gr)
        only_ans = sum(r.get("final") == "correct" and r["ex"] != "correct" and r.get("ans") == "correct" for r in gr)
        both = sum(r.get("final") == "correct" and r["ex"] == "correct" and r.get("ans") == "correct" for r in gr)
        pct = f"{100 * val / len(gr):.0f}%" if gr else "-"
        print(f"{cond:36}{len(v):>5}{len(gr):>5}{val:>6}{pct:>7}{only_sql:>7}{only_ans:>7}{both:>6}"
              f"{sum(r['seconds'] for r in v) / len(v):>7.1f}{sum(r['steps'] for r in v) / len(v):>8.1f}")


def summary_by(rows, field, levels, title):
    if not any(r.get(field, "-") != "-" for r in rows):
        return
    levels = [lv for lv in levels if any(r.get(field) == lv for r in rows)]
    print(f"\n{title}  (정답률 기준, gold_ 는 제외)")
    print(f"{'조건':20}" + "".join(f"{lv:>16}" for lv in levels))
    for cond in sorted({r["condition"] for r in rows}):
        cells = []
        for lv in levels:
            gr = graded([r for r in rows if r["condition"] == cond and r.get(field) == lv])
            ok = sum(r.get("final") == "correct" for r in gr)
            cells.append(f"{ok:>3}/{len(gr):<3} {100 * ok / len(gr):>3.0f}%" if gr else "-")
        print(f"{cond:20}" + "".join(f"{c:>16}" for c in cells))


def summary_by_difficulty(rows):
    summary_by(rows, "difficulty", ("easy", "medium", "hard", "extra"), "SQL 난이도별 (Spider hardness)")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        rows = attach_difficulty(latest(read_jsonl(EVAL_JSONL)))
        show(rows)
        summary(rows)
        summary_by_difficulty(rows)
        return
    files = sorted(LOG_DIR.glob("*_evidence.jsonl")) + sorted(LOG_DIR.glob("*_no_evidence.jsonl"))
    if not files:
        raise SystemExit(f"{LOG_DIR} 에 채점할 *_evidence.jsonl 이 없습니다.")
    done = {row_key(r) for r in read_jsonl(EVAL_JSONL) if "ans" in r}   # ans 가 없는 옛 채점 줄은 다시 채점한다
    todo = [({**rec, "condition": f.stem}) for f in files for rec in read_jsonl(f)
            if (f.stem, rec["id"], rec.get("run_id")) not in done]
    print(f"결과 줄 중 새로 채점할 것: {len(todo)}개 (이미 채점됨: {len(done)}개)")

    llm = None
    if USE_LLM_JUDGE and todo:
        from llm import get_llm
        llm = get_llm(JUDGE_MODEL)
    gold_cache, new_rows, gold_map = {}, [], load_gold()
    print(f"정답 SQL: {'PostgreSQL 용으로 변환한 ' + str(GOLD_PG) if gold_map else '원본 정답 SQL (2. query_filter/filter.py 를 돌리면 변환본을 쓴다)'}")
    EVAL_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with EVAL_JSONL.open("a", encoding="utf-8") as out:
        for rec in todo:
            gold_sql, forced, why = gold_for(rec, gold_map)
            if forced:                             # 정답이 불안정하거나 변환 불가 → 채점에서 뺀다
                gold = (None, [], f"정답 제외: {why}")
            else:
                if rec["id"] not in gold_cache:    # 정답은 질문마다 한 번만 실행
                    gold_cache[rec["id"]] = run(gold_sql)
                gold = gold_cache[rec["id"]]
            pred = run(rec.get("final_sql"))
            ex, note = (forced, why[:80]) if forced else ex_grade(gold, pred)
            exv, exv_note = (forced, why[:80]) if forced else value_grade(gold, pred)
            ans, ans_note = (forced, why[:80]) if forced else answer_grade(gold, rec.get("answer"))
            verdict, reason = "-", ""
            if llm and not ex.startswith("gold_"):
                verdict, reason = llm_judge(llm, rec, result_text(gold))
            row = {
                "evaluated_at": datetime.now().isoformat(timespec="seconds"),
                "db": rec["db_id"], "condition": rec["condition"], "kind": rec["kind"], "id": rec["id"],
                "run_id": rec.get("run_id"), "model": rec.get("model"),
                "question": rec["question"], "evidence": rec.get("evidence"),
                "gold_sql": rec["gold_sql"], "gold_sql_pg": gold_sql, "gold_result": result_text(gold),
                "final_sql": rec.get("final_sql"), "agent_result": result_text(pred),
                "answer": rec.get("answer"),
                "ans": ans, "ans_note": ans_note, "ex": ex, "ex_note": note, "ex_value": exv, "ex_value_note": exv_note, "llm_verdict": verdict, "llm_reason": reason,
                "judge_model": JUDGE_MODEL if llm else None,
                "gold_rows": None if gold[2] else len(gold[0]), "pred_rows": None if pred[2] else len(pred[0]),
                "steps": len(rec["steps"]),
                "sparql_calls": sum(s["tool"] in ("ontology_query", "sparql_query") for s in rec["steps"]),
                "seconds": rec["seconds"], "error": rec["error"],
            }
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            new_rows.append(row)

    rows = attach_difficulty(latest(read_jsonl(EVAL_JSONL)))
    with EVAL_CSV.open("w", encoding="utf-8-sig", newline="") as f:   # utf-8-sig: 엑셀에서 한글이 안 깨지게
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    show(new_rows)
    summary(rows)
    summary_by_difficulty(rows)
    print(f"\n채점 결과 → {EVAL_JSONL}\n표 형식    → {EVAL_CSV}\n(전체를 다시 보려면: python evaluate.py show)")


if __name__ == "__main__":
    main()
