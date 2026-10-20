"""BIRD 정답 SQL(SQLite 문법)을 PostgreSQL 에서 돌아가게 바꾸고, 원본 SQLite 의 결과와 같은지 검증한다.

질문 필터(filter.py)가 만든 data/questions.json 의 정답 SQL 을 대상으로, 이미 PostgreSQL 에 옮겨 둔 DB(1. sqlite to pg)에서 실행해 본다.
filter.py 가 이 모듈을 불러 쓴다. 단독 실행: python "data/2. query_filter/steps/gold_to_pg.py" movie_3 --dsn "postgresql://user@localhost:5432/movie_3"
"""
import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import psycopg

DATA = Path(__file__).resolve().parents[2]      # data/


def gold(db, sqlite_path, dsn, questions_path, out_path):
    """BIRD 정답 SQL(SQLite 문법)을 PostgreSQL 에서 돌아가게 바꾸고, 원본 SQLite 의 결과와 같은지 검증한다.

    각 질문의 정답 SQL 에 대해
      1) 원본을 PostgreSQL 에서 그대로 실행 → 오류가 없으면 SQLite 결과와 비교
      2) 오류가 나면 sqlglot 으로 SQLite → PostgreSQL 문법 변환 (컬럼 대소문자 따옴표, LIKE→ILIKE, iif, 백틱, strftime 등)
      3) 그래도 'text = 숫자' 오류가 나면, 텍스트 컬럼과 비교하는 숫자 리터럴을 문자열로 바꾼다
         (SQLite 는 텍스트 컬럼과 숫자를 비교할 때 숫자를 텍스트로 바꿔 비교하므로 의미가 같다)
    결과 상태 (status)
      ok         : PostgreSQL 결과가 원본 SQLite 결과와 같다 → 이 SQL 을 정답으로 쓴다
      unstable   : 원본이 실행은 되지만 결과가 SQLite 와 다르다 (정렬 없는 LIMIT, 동점 처리 등) → 채점에서 뺀다
      unresolved : 고쳐도 실행되지 않거나 결과가 다르다 → 채점에서 뺀다
    """
    from decimal import Decimal
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope

    qpath, out_path = Path(questions_path), Path(out_path)
    qs = [q for q in json.loads(qpath.read_text(encoding="utf-8")) if q["db_id"] == db]
    if not qs:
        raise SystemExit(f"{qpath} 에 db_id={db} 질문이 없습니다.")

    def cell(v):
        if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            return round(float(v), 6)
        if v is None:
            return None
        v = str(v).strip()
        return round(float(v), 6) if re.fullmatch(r"[-+]?\d+(\.\d+)?", v) else v    # 숫자 모양 문자열은 숫자로 (SQLite 는 한 컬럼에 숫자/문자가 섞임)

    pg = psycopg.connect(dsn, autocommit=True)
    pg.execute("set default_transaction_read_only = on")
    pg.execute("set statement_timeout = '60s'")
    lite = sqlite3.connect(f"file:{Path(sqlite_path).resolve()}?mode=ro", uri=True)   # 원본은 읽기 전용

    def run_pg(sql_text):
        try:
            return {tuple(cell(v) for v in r) for r in pg.execute(sql_text).fetchall()}, None
        except Exception as e:
            return None, str(e).splitlines()[0]

    def run_lite(sql_text):
        try:
            return {tuple(cell(v) for v in r) for r in lite.execute(sql_text).fetchall()}, None
        except Exception as e:
            return None, str(e)[:100]

    kinds, real_cols = {}, {}
    for t, c, dt in pg.execute("select table_name, column_name, data_type from information_schema.columns where table_schema='public'"):
        real_cols.setdefault(t, {})[c.lower()] = c
        kinds.setdefault(c, set()).add(dt in ("text", "character varying", "character"))
    text_cols = {n for n, k in kinds.items() if k == {True}}      # 모든 테이블에서 text 계열인 컬럼 이름

    def convert_sql(sql_text):
        """컬럼 이름을 실제 대소문자로 맞춰 따옴표로 감싸고(PostgreSQL 은 구분함), LIKE 를 ILIKE 로(SQLite 는 구분 안 함) 바꾼 뒤 sqlglot 변환."""
        tree = sqlglot.parse_one(sql_text, read="sqlite")
        for scope in traverse_scope(tree):
            tabs = {alias: src.name for alias, src in scope.sources.items() if isinstance(src, exp.Table)}
            for col in scope.columns:
                low = col.name.lower()
                if col.table:
                    t = tabs.get(col.table)
                    cands = [real_cols.get(t, {}).get(low)] if t else []
                else:
                    cands = [real_cols[t][low] for t in tabs.values() if low in real_cols.get(t, {})]
                real = next((c for c in cands if c), None)
                if real and (real != real.lower() or " " in real):
                    col.set("this", exp.to_identifier(real, quoted=True))
        for like in list(tree.find_all(exp.Like)):
            like.replace(exp.ILike(this=like.this, expression=like.expression))
        sqlite_semantics(tree)
        return tree.sql(dialect="postgres")

    P = lambda t: sqlglot.parse_one(t, read="postgres")
    as_text = lambda e: f"CAST(({e.sql('postgres')}) AS TEXT)"

    def sqlite_semantics(tree):
        """SQLite 가 자동으로 해 주는 동작을 PostgreSQL 식으로 옮기는 규칙 (결과는 SQLite 와 대조해 검증한다)."""
        # 1) strftime(형식, 값): 값이 날짜 모양('YYYY-MM-DD…')이 아니면 NULL (SQLite 도 NULL). 숫자 컬럼에 쓰면 결과가 비게 된다.
        for n in list(tree.find_all(exp.TimeToStr)):
            if n.args["format"].name == "%J":      # %J = 율리우스 일수(소수 포함, 숫자). 뺄셈으로 날짜 차이를 구할 때 쓴다
                v = n.this.this if isinstance(n.this, exp.TsOrDsToTimestamp) else n.this
                n.replace(P(f"(EXTRACT(EPOCH FROM CAST({as_text(v)} AS TIMESTAMP)) / 86400.0 + 2440587.5)"))
                continue
            inner = n.this.this if isinstance(n.this, exp.TsOrDsToTimestamp) else n.this
            s_ = as_text(inner)
            n.set("this", P(f"CASE WHEN {s_} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN CAST({s_} AS TIMESTAMP) END"))
        # 2) 텍스트 컬럼 산술(a - b): SQLite 는 텍스트의 앞쪽 숫자만 읽고(없으면 0) 계산한다. '2005-05-24 …' → 2005
        for n in list(tree.find_all(exp.Sub, exp.Add, exp.Mul, exp.Div)):
            for side in ("this", "expression"):
                c = n.args.get(side)
                if (isinstance(c, exp.Column) and c.name in text_cols) or isinstance(c, exp.Substring):
                    n.set(side, P(f"COALESCE(CAST(SUBSTRING({as_text(c)} FROM '^\\s*[-+]?\\d+(\\.\\d+)?') AS NUMERIC), 0)"))
        # 3) IIF(조건, 텍스트컬럼, 숫자): PostgreSQL 은 CASE 의 가지 타입이 같아야 하므로 숫자를 문자열로 (비교는 숫자 모양 문자열을 숫자로 본다)
        for n in list(tree.find_all(exp.If)):
            a, b = n.args.get("true"), n.args.get("false")
            if isinstance(a, exp.Column) and a.name in text_cols and isinstance(b, exp.Literal) and not b.is_string:
                b.replace(exp.Literal.string(b.name))
        # 4) date(값, '-N day') 같은 날짜 계산 → DATE + INTERVAL
        for n in list(tree.find_all(exp.Date)):
            if n.args.get("zone") is not None:
                n.replace(P(f"CAST(CAST(({n.this.sql('postgres')}) AS DATE) + CAST(({n.args['zone'].sql('postgres')}) AS INTERVAL) AS DATE)"))
        # 5) HAVING 에서 SELECT 별칭을 쓰는 것(SQLite 허용, PostgreSQL 불허) → 별칭의 식으로 바꾼다
        for sel in tree.find_all(exp.Select):
            h = sel.args.get("having")
            if h:
                alias = {e.alias: e.this for e in sel.expressions if isinstance(e, exp.Alias)}
                for c in list(h.find_all(exp.Column)):
                    if not c.table and c.name in alias:
                        c.replace(alias[c.name].copy())

    def quote_text_literals(sql_text):
        """텍스트 컬럼과 비교되는 숫자 리터럴을 문자열 리터럴로 바꾼다 (release_year = 2006 → '2006')."""
        tree = sqlglot.parse_one(sql_text, read="postgres")
        for cmp in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
            for col, lit in ((cmp.left, cmp.right), (cmp.right, cmp.left)):
                if isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and not lit.is_string and col.name in text_cols:
                    lit.replace(exp.Literal.string(lit.name))
        return tree.sql(dialect="postgres")

    out, stat = {}, Counter()
    for i, q in enumerate(qs):
        qid, orig = f"{db}-{i}", q["SQL"]
        ref, _ = run_lite(orig)                                   # 원본 SQLite 에서의 결과 = 기준
        how, sql_text = "original", orig
        res, err = run_pg(orig)
        if err:                                                   # 2단계: sqlglot 변환
            how = "sqlglot"
            try:
                sql_text = convert_sql(orig)
                res, err = run_pg(sql_text)
            except Exception as e:
                res, err = None, f"변환 실패: {str(e)[:60]}"
            if err and ("text = integer" in err or "operator does not exist: text" in err):
                how = "sqlglot+quote"                             # 3단계: 텍스트 컬럼과 숫자 비교
                try:
                    sql_text = quote_text_literals(sql_text)
                    res, err = run_pg(sql_text)
                except Exception as e:
                    res, err = None, f"변환 실패: {str(e)[:60]}"
        if err:
            status, note = "unresolved", f"변환해도 실행 안 됨: {err[:80]}"
        elif ref is not None and res == ref:
            status, note = "ok", ""
        elif how == "original":
            status, note = "unstable", "PostgreSQL 결과가 SQLite 결과와 다름 (정렬 없는 LIMIT, 동점 처리 등)"
        else:
            status, note = "unresolved", "변환했지만 SQLite 결과와 다름"
        out[qid] = {"status": status, "how": how, "sql": sql_text if status == "ok" else None, "original": orig, "note": note}
        stat[(status, how)] += 1
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"질문 {len(qs)}개 → {out_path}")
    for (status, how), n in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {status:11} {how:14} {n:4}개")
    bad = [(k, v) for k, v in out.items() if v["status"] != "ok"]
    print(f"\n채점에 쓰는 정답(ok): {len(out) - len(bad)}개 | 뺄 것: {len(bad)}개")
    for k, v in bad[:15]:
        print(f"  {k:14} {v['status']:10} {v['note'][:90]}")
    if len(bad) > 15:
        print(f"  ... 외 {len(bad) - 15}개 ({out_path} 의 note 참고)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db", help="movie_3 | synthea (questions.json 의 db_id, 원본은 data/1. sqlite to pg/<db>.sqlite)")
    ap.add_argument("--dsn", required=True)
    a = ap.parse_args()
    gold(a.db, DATA / "1. sqlite to pg" / f"{a.db}.sqlite", a.dsn, DATA / "questions.json", DATA / f"gold_pg_{a.db}.json")
