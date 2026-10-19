import csv
import json
import time
from datetime import datetime
from pathlib import Path

from agent import build_agent

# ---- 설정 ----
KIND = "sparql"                       # "sparql" 또는 "sql"
QUESTIONS_FILE = Path("data/ACME_Insurance/query.csv")
CONTEXT_FILE = Path("context.txt")
OUT_FILE = Path("results/queries.jsonl")
LIMIT = 3                             # 처음엔 몇 개만 시험. 전체는 None


def load_questions(path):
    """[(id, question), ...] 로 돌려준다."""
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return [(r.get("id") or str(i + 1), r["question"].strip()) for i, r in enumerate(rows)]
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [(str(i + 1), q) for i, q in enumerate(lines)]


def done_ids(path):
    """이미 처리한 질문 id 목록 (중간에 끊겨도 이어서 하려고)."""
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            if rec.get("kind") == KIND:
                ids.add(rec["id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return ids


def collect_calls(messages):
    calls, results = [], []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls.append(tc["args"].get("query", ""))
        if m.__class__.__name__ == "ToolMessage":
            results.append(m.content)
    return [{"query": q, "result": r} for q, r in zip(calls, results)]


def run_one(agent, question):
    out = agent.invoke({"messages": [{"role": "user", "content": question}]})
    messages = out["messages"]
    answer = messages[-1].content
    if isinstance(answer, list):
        answer = "".join(b.get("text", "") for b in answer if isinstance(b, dict))
    return answer, collect_calls(messages)


def main():
    context = CONTEXT_FILE.read_text(encoding="utf-8") if CONTEXT_FILE.exists() else ""
    agent = build_agent(KIND, context)

    questions = load_questions(QUESTIONS_FILE)
    if LIMIT:
        questions = questions[:LIMIT]
    finished = done_ids(OUT_FILE)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUT_FILE.open("a", encoding="utf-8") as f:
        for n, (qid, question) in enumerate(questions, 1):
            if qid in finished:
                print(f"[{n}/{len(questions)}] {qid} 건너뜀 (이미 처리됨)")
                continue
            start = time.time()
            try:
                answer, steps = run_one(agent, question)
                error = None
            except Exception as e:           # 한 질문이 실패해도 다음 질문으로
                answer, steps, error = None, [], str(e)
            record = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "kind": KIND,
                "id": qid,
                "question": question,
                "steps": steps,
                "answer": answer,
                "error": error,
                "seconds": round(time.time() - start, 1),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()                        # 바로 디스크에 기록 (끊겨도 남음)
            print(f"[{n}/{len(questions)}] {qid} {'ERROR' if error else 'ok'} ({record['seconds']}s)")


if __name__ == "__main__":
    main()