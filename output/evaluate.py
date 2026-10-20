"""에이전트 결과를 정답과 나란히 놓고, 규칙으로 채점해 jsonl / csv 에 쌓는다 (LLM 은 쓰지 않는다).

정답(final) 판정은 두 단계다.
  1. SQL 결과: 에이전트 SQL 을 실행한 결과가 정답 SQL 의 결과와 행·컬럼까지 같으면 정답 (엄격 EX, BIRD 방식)
  2. 답변 글: 1번에서 오답인 것 중, 에이전트의 자연어 답변 글에 정답 값이 모두 들어 있으면 정답
       - 정답 결과의 모든 행에서, 그 행의 모든 값이 답변 글 안에 있어야 한다
       - 숫자는 쉼표·$ 를 무시하고 반올림(0.01 또는 0.5%) 차이를 허용, % 는 0.123 ↔ 12.3 으로 본다
       - 글자는 대소문자·공백을 무시하고, 단어 경계로 포함 여부를 본다 (짧은 값이 다른 단어 안에서 우연히 맞는 것을 막음)
       - 정답 행의 절반 이상만 들어 있으면 partial (정답으로 세지 않음), 그보다 적으면 wrong
  정답 SQL 이 불안정·변환 불가(gold_unstable / gold_error)이거나 결과가 빈 질문(gold_empty)은 점수에서 뺀다.

- 사람이 직접 판단할 수 있게 질문, 정답 결과, 에이전트의 자연어 답변, 에이전트 SQL 의 실행 결과를 나란히 보여 준다.
- 에이전트 원본은 log/<DB>/*_evidence.jsonl 에서 읽고, 채점 결과는 output/<DB>/evaluation.jsonl 에 한 줄씩 **이어 붙인다**. 이미 채점한 결과 줄은 다시 채점하지 않는다.
- evaluation.csv 는 jsonl 에서 매번 새로 만든다 (엑셀 등으로 열어 보는 용도).

실행 (프로젝트 루트에서, DB 는 PG_DB 로 정한다):
    PG_DB=movie_3 python output/evaluate.py         새로 생긴 결과 줄만 채점하고, 방금 채점한 것을 나란히 출력
    PG_DB=movie_3 python output/evaluate.py show    지금까지 채점한 전체를 나란히 출력 (채점은 안 함)
"""
import csv
import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # 프로젝트 루트 (db.py 가 있는 곳)
sys.path.insert(0, str(ROOT))
from db import DB_NAME, connect  # noqa: E402

LOG_DIR = ROOT / "log" / DB_NAME
OUT_DIR = ROOT / "output" / DB_NAME               # 채점 결과는 output/<DB>/ 에 저장한다 (log/ 는 에이전트 원본만)
EVAL_JSONL = OUT_DIR / "evaluation.jsonl"
EVAL_CSV = OUT_DIR / "evaluation.csv"
DIFFICULTY = ROOT / "data" / "difficulty.json"   # data/2. query_filter 가 만든 질문 난이도 (Spider hardness)
GOLD_PG = ROOT / "data" / f"gold_pg_{DB_NAME}.json"   # data/2. query_filter 가 만든 PostgreSQL 용 정답
SHOW_ROWS = 30                        # 저장·표시할 결과 행 수 (정답 결과, 에이전트 결과 각각)

FIELDS = ["evaluated_at", "db", "condition", "kind", "id", "run_id", "model",
          "difficulty", "sp_c1", "sp_c2", "sp_co", "question", "evidence", "gold_sql", "gold_sql_pg", "gold_result", "final_sql", "agent_result", "answer",
          "final", "final_basis", "ex", "ex_note", "ans", "ans_note",
          "gold_rows", "pred_rows", "steps", "sparql_calls", "seconds", "error"]

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
    if num is None:                                           # 글자: 단어 경계로 찾는다 (짧은 값 'D' 가 'and' 안에서 우연히 맞는 것을 막는다)
        return re.search(r"(?<!\w)" + re.escape(norm_text(v)) + r"(?!\w)", text) is not None
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
    """(final 판정, 근거). 1) SQL 결과가 정답과 같으면 정답, 2) 아니면 답변 글에 정답 값이 모두 있으면 정답."""
    if r["ex"].startswith("gold_") or str(r.get("ans", "")).startswith("gold_"):
        return str(r.get("ans") or r["ex"]), ""
    if r["ex"] == "correct":
        return "correct", "1. SQL 결과(행·컬럼)가 정답과 같음"
    if r.get("ans") == "correct":
        return "correct", "2. SQL 결과는 다르지만 답변 글에 정답이 들어 있음"
    return ("partial" if r.get("ans") == "partial" else "wrong"), r.get("ans_note", "")


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


def graded(rows):
    """점수에 쓰는 행: 정답 SQL 문제(gold_error / gold_unstable)나 빈 정답(gold_empty)은 뺀다."""
    return [r for r in rows if not r["ex"].startswith("gold_") and not str(r.get("ans", "")).startswith("gold_")]


def summary(rows):
    print("\n요약 (정답이 불안정·오류·빈 결과인 질문은 점수에서 제외)")
    print("  정답   : 1) SQL 결과(행·컬럼)가 정답과 같다, 또는 2) 1이 아닌 것 중 답변 글에 정답이 모두 들어 있다")
    print("  근거   : 정답 중 1번(SQL 결과)으로 인정된 수 / 2번(답변 글)으로 인정된 수")
    print(f"{'조건':36}{'질문':>5}{'채점':>5}{'정답':>6}{'정답률':>7}{'1.SQL':>7}{'2.답변':>7}{'평균초':>7}{'도구호출':>8}")
    for cond in sorted({r["condition"] for r in rows}):
        v = [r for r in rows if r["condition"] == cond]
        gr = graded(v)
        val = sum(r.get("final") == "correct" for r in gr)
        by_sql = sum(r.get("final") == "correct" and r["ex"] == "correct" for r in gr)
        by_ans = val - by_sql
        pct = f"{100 * val / len(gr):.0f}%" if gr else "-"
        print(f"{cond:36}{len(v):>5}{len(gr):>5}{val:>6}{pct:>7}{by_sql:>7}{by_ans:>7}"
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
            ans, ans_note = (forced, why[:80]) if forced else answer_grade(gold, rec.get("answer"))
            row = {
                "evaluated_at": datetime.now().isoformat(timespec="seconds"),
                "db": rec["db_id"], "condition": rec["condition"], "kind": rec["kind"], "id": rec["id"],
                "run_id": rec.get("run_id"), "model": rec.get("model"),
                "question": rec["question"], "evidence": rec.get("evidence"),
                "gold_sql": rec["gold_sql"], "gold_sql_pg": gold_sql, "gold_result": result_text(gold),
                "final_sql": rec.get("final_sql"), "agent_result": result_text(pred),
                "answer": rec.get("answer"),
                "ex": ex, "ex_note": note, "ans": ans, "ans_note": ans_note,
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
    print(f"\n채점 결과 → {EVAL_JSONL}\n표 형식    → {EVAL_CSV}\n(전체를 다시 보려면: PG_DB={DB_NAME} python output/evaluate.py show)")


if __name__ == "__main__":
    main()
