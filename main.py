import json
import os
import time
from datetime import datetime
from pathlib import Path

from agent import PROMPTS, build_agent, reset_gate
from db import DB_NAME, R2RML, ontology_file
from llm import DEFAULT_MODEL, MAX_TOKENS

# ---- 설정 ----
KIND = os.getenv("AGENT_KIND") or "ontop"   # 환경변수 AGENT_KIND 로도 정할 수 있다. "sql": SQL 만 / "sparql": OWL 을 SPARQL 로 검색 → SQL 로 DB 검색 / "ontop": SPARQL 로 데이터 직접 질의(Ontop)
QUESTIONS_FILE = Path("data/questions.json")   # BIRD train.json 에서 걸러 낸 질문
USE_EVIDENCE = True                   # 질문에 BIRD evidence(힌트)를 붙일지
CONTEXT_FILE = Path("context.txt")
LIMIT = None                          # 처음엔 몇 개만 시험(예: 3). None 이면 전체
RETRY_ERRORS = True                   # True 면 이전에 에이전트가 실패(error)한 질문만 결과에서 빼서 다시 돌린다
RECURSION_LIMIT = 50                  # 에이전트 한 질문당 최대 단계 수 (LangGraph 기본은 25)
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")   # 결과 한 줄 한 줄이 어느 실행에서 나왔는지 잇는 번호

# 로그는 전부 log/<DB>/ 안에 만든다. 실험 조건(kind, 모델, evidence)별로 파일을 나눈다.
LOG_DIR = Path("log") / DB_NAME
_cond = f"{KIND}_{DEFAULT_MODEL}_{'evidence' if USE_EVIDENCE else 'no_evidence'}"   # 파일 이름에 모델도 넣는다 (모델별 결과가 섞이지 않게)
OUT_FILE = LOG_DIR / f"{_cond}.jsonl"             # 질문별 결과만 (쿼리, DB 결과, 답변)
RUN_LOG = LOG_DIR / f"{RUN_ID}_{KIND}_{DEFAULT_MODEL}.log"   # 실행 한 번의 설정(맨 위) + 진행 상황


def log(msg):
    """화면에 출력하고 같은 내용을 log/<DB>/<run_id>_<kind>.log 에도 남긴다."""
    print(msg)
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_questions(path, db_id):
    """questions.json 에서 지금 연결된 DB의 질문만 [dict, ...] 로 돌려준다.

    id 는 "<db_id>-<그 DB 안에서의 순번>". 정답 SQL(gold_sql)은 채점용으로만 쓰고 에이전트에는 주지 않는다.
    """
    items = json.loads(path.read_text(encoding="utf-8"))
    items = [q for q in items if q["db_id"] == db_id]
    if not items:
        raise SystemExit(f"{path}에 db_id={db_id} 질문이 없습니다 (.env 의 PG_DB 확인).")
    return [
        {"id": f"{db_id}-{i}", "db_id": db_id, "question": q["question"].strip(),
         "evidence": q.get("evidence", "").strip(), "gold_sql": q["SQL"]}
        for i, q in enumerate(items)
    ]


def make_prompt(q):
    if USE_EVIDENCE and q["evidence"]:
        return f"{q['question']}\n\nHint: {q['evidence']}"
    return q["question"]


def done_ids(path):
    """이미 처리한 질문 id 목록 (중간에 끊겨도 이어서 하려고)."""
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            if rec.get("kind") == KIND and rec.get("use_evidence") == USE_EVIDENCE:
                ids.add(rec["id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return ids


def drop_failed(path):
    """결과 파일에서 error 가 있는 줄(에이전트가 중간에 실패한 질문)을 빼서 다시 돌릴 수 있게 한다.

    뺀 줄은 지우지 않고 <결과파일>.failed.jsonl 에 옮겨 둔다. 몇 줄을 뺐는지 돌려준다."""
    if not path.exists():
        return 0
    keep, failed = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            bad = json.loads(line).get("error") is not None
        except json.JSONDecodeError:
            bad = False
        (failed if bad else keep).append(line)
    if failed:
        with path.with_suffix(".failed.jsonl").open("a", encoding="utf-8") as f:
            f.write("\n".join(failed) + "\n")
        path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return len(failed)


def collect_calls(messages):
    """에이전트가 부른 도구(sql_query / ontology_query / sparql_query)와 그 쿼리, 결과를 순서대로 모은다.

    ontop 의 sparql_query 는 Ontop 이 번역한 SQL(native_sql)도 함께 담는다."""
    calls, results = [], []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls.append({"tool": tc["name"], "query": tc["args"].get("query", "")})
        if m.__class__.__name__ == "ToolMessage":
            results.append((m.content, getattr(m, "artifact", None)))
    steps = []
    for c, (r, artifact) in zip(calls, results):
        step = dict(c)
        if isinstance(artifact, dict) and artifact.get("sql"):
            step["native_sql"] = artifact["sql"]
        step["result"] = r
        steps.append(step)
    return steps


class RunFailed(Exception):
    """에이전트가 도중에 실패했을 때, 그때까지 시도한 쿼리(steps)를 같이 들고 나간다."""

    def __init__(self, error, steps):
        super().__init__(str(error))
        self.steps = steps


def run_one(agent, question):
    reset_gate()  # 질문마다 "온톨로지 검색 여부"를 처음 상태로
    last = {"messages": []}
    try:
        # stream 으로 한 단계씩 받아 두면, 한도 초과 같은 오류가 나도 지금까지의 대화를 건질 수 있다
        for state in agent.stream({"messages": [{"role": "user", "content": question}]},
                                  config={"recursion_limit": RECURSION_LIMIT}, stream_mode="values"):
            last = state
    except Exception as e:
        raise RunFailed(e, collect_calls(last["messages"])) from e
    messages = last["messages"]
    answer = messages[-1].content
    if isinstance(answer, list):
        answer = "".join(b.get("text", "") for b in answer if isinstance(b, dict))
    return answer, collect_calls(messages)


def run_config(context, n_questions, n_skipped):
    """이번 실행의 설정. 나중에 결과를 볼 때 '어떤 조건으로 돌린 건지' 알 수 있게 남긴다."""
    return {
        "run_id": RUN_ID,
        "time": datetime.now().isoformat(timespec="seconds"),
        "kind": KIND,
        "db": DB_NAME,
        "model": DEFAULT_MODEL,
        "max_tokens": MAX_TOKENS,
        "use_evidence": USE_EVIDENCE,
        "limit": LIMIT,
        "recursion_limit": RECURSION_LIMIT,
        "retry_errors": RETRY_ERRORS,
        "questions_file": str(QUESTIONS_FILE),
        "n_questions": n_questions,          # 이번에 대상으로 잡은 질문 수
        "n_skipped": n_skipped,              # 이미 처리돼 건너뛴 수
        "ontology_file": str(ontology_file()) if KIND in ("sparql", "ontop") else None,
        "r2rml_file": str(R2RML) if KIND == "ontop" else None,
        "context_file": str(CONTEXT_FILE) if context else None,
        "system_prompt": PROMPTS[KIND] + ("\n\n" + context if context else ""),
        "out_file": str(OUT_FILE),
    }


def log_config(cfg):
    """실행 설정을 로그 파일 맨 위에 사람이 읽기 쉬운 텍스트로 적는다 (시스템 프롬프트는 블록으로)."""
    cfg = dict(cfg)
    prompt = cfg.pop("system_prompt")
    log("=" * 60)
    for k, v in cfg.items():
        log(f"{k}: {v}")
    log("--- system_prompt ---")
    log(prompt)
    log("=" * 60)


def main():
    context = CONTEXT_FILE.read_text(encoding="utf-8") if CONTEXT_FILE.exists() else ""
    agent = build_agent(KIND, context)

    questions = load_questions(QUESTIONS_FILE, DB_NAME)
    if LIMIT:
        questions = questions[:LIMIT]
    n_retry = drop_failed(OUT_FILE) if RETRY_ERRORS else 0   # 실패한 질문은 결과에서 빼서 다시 돌린다
    finished = done_ids(OUT_FILE)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = run_config(context, len(questions), sum(q["id"] in finished for q in questions))
    log_config(cfg)
    log(f"[run {RUN_ID}] kind={KIND} db={DB_NAME} model={DEFAULT_MODEL} evidence={USE_EVIDENCE} "
        f"limit={LIMIT} 질문 {cfg['n_questions']}개 (건너뜀 {cfg['n_skipped']}, 실패해서 다시 돌림 {n_retry}) → {OUT_FILE}")
    n_ok = n_err = 0
    run_start = time.time()
    with OUT_FILE.open("a", encoding="utf-8") as f:
        for n, q in enumerate(questions, 1):
            qid = q["id"]
            if qid in finished:
                log(f"[{n}/{len(questions)}] {qid} 건너뜀 (이미 처리됨)")
                continue
            start = time.time()
            try:
                answer, steps = run_one(agent, make_prompt(q))
                error = None
            except RunFailed as e:           # 한 질문이 실패해도 다음 질문으로 (그때까지의 쿼리는 남긴다)
                answer, steps, error = None, e.steps, str(e)
            except Exception as e:
                answer, steps, error = None, [], str(e)
            # SPARQL 단계: sparql 조건은 온톨로지(OWL) 검색, ontop 조건은 데이터를 직접 질의
            sparql_steps = [s for s in steps if s["tool"] in ("ontology_query", "sparql_query")]
            sql_steps = [s for s in steps if s["tool"] == "sql_query"]
            if sql_steps:
                last_sql = sql_steps[-1]
            else:   # ontop: 마지막으로 성공한 SPARQL 을 Ontop 이 번역한 SQL 이 최종 SQL (채점 때 다시 실행한다)
                ok = [s for s in steps if s["tool"] == "sparql_query" and s.get("native_sql")
                      and not str(s["result"]).startswith("ERROR")]
                last_sql = {"query": ok[-1]["native_sql"], "result": ok[-1]["result"]} if ok else {}
            record = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "run_id": RUN_ID,                # RUNS_FILE 의 설정과 연결
                "model": DEFAULT_MODEL,
                "kind": KIND,
                "db_id": q["db_id"],
                "id": qid,
                "use_evidence": USE_EVIDENCE,
                "question": q["question"],
                "evidence": q["evidence"],
                "gold_sql": q["gold_sql"],
                "sparql_queries": [s["query"] for s in sparql_steps],       # 온톨로지를 검색한 SPARQL
                "ontology_results": [s["result"] for s in sparql_steps],     # 거기서 나온 스키마(클래스, 속성, 테이블/컬럼)
                "final_sql": last_sql.get("query"),                          # DB 에 마지막으로 실행한 SQL
                "final_result": last_sql.get("result"),                      # 그 SQL 의 DB 결과 (채점에 쓸 값)
                "steps": steps,                                              # 시도한 모든 쿼리와 결과
                "answer": answer,                                            # LLM 의 최종 답변
                "error": error,
                "seconds": round(time.time() - start, 1),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()                        # 바로 디스크에 기록 (끊겨도 남음)
            n_err += bool(error)
            n_ok += not error
            log(f"[{n}/{len(questions)}] {qid} {'ERROR: ' + error[:200] if error else 'ok'} ({record['seconds']}s)")
    log(f"[run {RUN_ID}] 끝: 성공 {n_ok}, 오류 {n_err}, 총 {round(time.time() - run_start, 1)}s")


if __name__ == "__main__":
    main()