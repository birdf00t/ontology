"""RIGOR 결과 점검: DB 의 모든 테이블·컬럼·FK 가 OWL 에 들어갔는지, R2RML 이 Ontop 에서 DB 와 같은 개수로 나오는지 본다.

  PG_DB=movie_3 python "data/3. RIGOR_edit/check_ontology.py"        (프로젝트 루트에서. synthea 도 같은 방법)

A. OWL 포함 여부 (DB 가 기준)
   - 테이블마다 클래스가 있는가 (prov:wasDerivedFrom = .../provenance/<테이블>)
   - FK 가 아닌 컬럼마다 데이터 속성, FK 컬럼마다 객체 속성이 있는가 (prov = .../<테이블>/<컬럼>)
   - 속성의 도메인이 그 테이블의 클래스인가, 범위 자료형이 DB 컬럼 타입과 어긋나지 않는가
B. R2RML 검증 (Ontop 으로 실제 질의)
   - OWL 의 클래스·속성이 매핑에 모두 있는가
   - 클래스 인스턴스 수 == 테이블 행 수
   - 속성 트리플 수 == DB 에서 센 값 수
       데이터 속성: NULL 이 아닌 값의 행 수 / FK 객체 속성: FK 가 NULL 이 아닌 행 수
       연결 테이블 속성: 연결 테이블의 (도메인 FK, 범위 FK) 쌍의 수
"""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import os  # noqa: E402

os.chdir(ROOT)   # db.py 의 Ontop 경로(tools/...)가 프로젝트 루트 기준이다

from rdflib import Graph, OWL, RDF, RDFS, URIRef  # noqa: E402
from rdflib.namespace import PROV  # noqa: E402

from db import DB_NAME, R2RML, connect, ontology_file, run_ontop_sparql  # noqa: E402

RR = "http://www.w3.org/ns/r2rml#"
OK_XSD = {   # DB 타입 → 허용하는 xsd 범위 (RIGOR 가 값 모양을 보고 날짜로 올리는 것까지 허용)
    "integer": {"integer", "int", "long", "short", "decimal"}, "bigint": {"integer", "int", "long", "decimal"},
    "smallint": {"integer", "int", "short", "decimal"},
    "numeric": {"decimal", "double", "float"}, "real": {"float", "double", "decimal"},
    "double precision": {"double", "float", "decimal"},
    "text": {"string", "date", "dateTime"}, "character varying": {"string", "date", "dateTime"},
    "character": {"string"}, "date": {"date"}, "boolean": {"boolean"},
    "timestamp without time zone": {"dateTime"}, "timestamp with time zone": {"dateTime"},
}


def db_meta():
    con = connect()
    cols = {}
    for t, c, dt in con.execute("select table_name, column_name, data_type from information_schema.columns "
                                "where table_schema='public' order by table_name, ordinal_position"):
        cols.setdefault(t, {})[c] = dt
    fks = []
    for t, c, rt, rc in con.execute("""select kcu.table_name, kcu.column_name, ccu.table_name, ccu.column_name
        from information_schema.table_constraints tc
        join information_schema.key_column_usage kcu on tc.constraint_name=kcu.constraint_name and tc.table_schema=kcu.table_schema
        join information_schema.constraint_column_usage ccu on tc.constraint_name=ccu.constraint_name and tc.table_schema=ccu.table_schema
        where tc.constraint_type='FOREIGN KEY' and tc.table_schema='public'"""):
        fks.append((t, c, rt))
    return con, cols, fks


def prov_of(g, e):
    out = []
    for o in g.objects(e, PROV.wasDerivedFrom):
        if "/provenance/" in str(o):
            out.append(tuple(unquote(x) for x in str(o).split("/provenance/", 1)[1].split("/")))
    return out


def count_sparql(pattern):
    text, _ = run_ontop_sparql(f"SELECT (COUNT(*) AS ?n) WHERE {{ {pattern} }}")
    m = re.findall(r"\d+", text.splitlines()[-1]) if text and not text.startswith("ERROR") else None
    return int(m[0]) if m else text


def main():
    con, cols, fks = db_meta()
    g = Graph()
    g.parse(ontology_file(), format="xml" if ontology_file().suffix == ".owl" else "turtle")
    fk_cols = {(t, c) for t, c, _ in fks}
    fk_target = {(t, c): rt for t, c, rt in fks}
    problems, notes = [], []
    iri = lambda e: str(e).split("#")[-1]

    classes = {e: prov_of(g, e) for e in g.subjects(RDF.type, OWL.Class) if str(e).startswith("http")}
    dprops = {e: prov_of(g, e) for e in g.subjects(RDF.type, OWL.DatatypeProperty)}
    oprops = {e: prov_of(g, e) for e in g.subjects(RDF.type, OWL.ObjectProperty)}
    table_class = {p[0][0]: e for e, p in classes.items() if len(p) == 1 and len(p[0]) == 1 and p[0][0] in cols}

    # ---------------- A. OWL 포함 여부
    n_cols = sum(len(c) for c in cols.values())
    miss_t = [t for t in cols if t not in table_class]
    miss_c = []
    for t, cs in cols.items():
        for c, dt in cs.items():
            kind = oprops if (t, c) in fk_cols else dprops
            hit = [e for e, p in kind.items() if (t, c) in {x[:2] for x in p if len(x) == 2}]
            if not hit:
                miss_c.append(f"{t}.{c} ({'FK→객체 속성' if (t, c) in fk_cols else '데이터 속성'})")
                continue
            if (t, c) not in fk_cols:       # 도메인·범위 점검
                for e in hit:
                    dom = [d for d in g.objects(e, RDFS.domain)]
                    if table_class.get(t) not in dom:
                        problems.append(f"도메인 불일치: {iri(e)} 의 도메인이 {t} 클래스가 아님 {[iri(d) for d in dom]}")
                    rng = {iri(r) for r in g.objects(e, RDFS.range)}
                    allowed = OK_XSD.get(dt)
                    if allowed and rng and not (rng & allowed):
                        notes.append(f"범위 확인: {t}.{c} DB 타입 {dt} ↔ 온톨로지 xsd:{','.join(rng)}")
    print(f"[A] OWL 포함 여부 ({DB_NAME}): 테이블 {len(cols)}개, 컬럼 {n_cols}개, FK {len(fks)}개")
    print(f"    클래스 {len(classes)}개, 데이터 속성 {len(dprops)}개, 객체 속성 {len(oprops)}개")
    print(f"    테이블→클래스 빠짐: {miss_t or '없음'}")
    print(f"    컬럼→속성 빠짐: {miss_c or '없음'}")
    problems += [f"OWL 에 테이블 없음: {t}" for t in miss_t] + [f"OWL 에 컬럼 없음: {c}" for c in miss_c]
    covered = {x[:2] for p in list(dprops.values()) + list(oprops.values()) for x in p if len(x) == 2}
    extra = [iri(e) for e, p in {**dprops, **oprops}.items()
             if not any(len(x) == 2 and (x[0], x[1]) in {(t, c) for t, cs in cols.items() for c in cs} for x in p)]
    print(f"    DB 컬럼에서 직접 나오지 않은 속성(LLM 이 추가): {extra or '없음'}")
    extra_cls = [iri(e) for e in classes if e not in table_class.values()]
    print(f"    테이블이 아닌 클래스(LLM 이 추가): {extra_cls or '없음'}")
    for n in notes[:10]:
        print("    참고:", n)

    # ---------------- B. R2RML
    m = Graph()
    m.parse(R2RML, format="turtle")
    mapped = {o for o in m.objects(None, URIRef(RR + "predicate"))} | {o for o in m.objects(None, URIRef(RR + "class"))}
    unmapped = [iri(e) for e in list(classes) + list(dprops) + list(oprops) if e not in mapped]
    print(f"\n[B] R2RML ({R2RML.name}): TriplesMap {len(list(m.subjects(RDF.type, URIRef(RR + 'TriplesMap'))))}개")
    print(f"    매핑에 없는 OWL 요소: {unmapped or '없음'}")
    problems += [f"R2RML 에 없음: {u}" for u in unmapped]

    jobs = []   # (설명, SPARQL 패턴, 기대값)
    for t, e in table_class.items():
        jobs.append((f"클래스 {iri(e)}", f"?x a <{e}>", con.execute(f'select count(*) from "{t}"').fetchone()[0]))
    for e, p in dprops.items():
        two = [x for x in p if len(x) == 2 and x[1] in cols.get(x[0], {})]
        if e in mapped and two:
            t, c = two[0]
            jobs.append((f"속성 {iri(e)}", f"?s <{e}> ?o", con.execute(f'select count(*) from "{t}" where "{c}" is not null').fetchone()[0]))
    for e, p in oprops.items():
        if e not in mapped or len(p) != 1:
            continue
        x = p[0]
        if len(x) == 2:
            t, c = x
            jobs.append((f"속성 {iri(e)}", f"?s <{e}> ?o", con.execute(f'select count(*) from "{t}" where "{c}" is not null').fetchone()[0]))
        elif len(x) == 1:                      # 연결 테이블: 도메인·범위 쪽 FK 쌍
            dom = next((t for t, c in table_class.items() if c in set(g.objects(e, RDFS.domain))), None)
            rng = next((t for t, c in table_class.items() if c in set(g.objects(e, RDFS.range))), None)
            t = x[0]
            fd = [c for tt, c, rt in fks if tt == t and rt == dom]
            fr = [c for tt, c, rt in fks if tt == t and rt == rng]
            if len(fd) == 1 and len(fr) == 1:
                q = f'select count(*) from (select distinct "{fd[0]}", "{fr[0]}" from "{t}" where "{fd[0]}" is not null and "{fr[0]}" is not null) z'
                jobs.append((f"속성 {iri(e)}", f"?s <{e}> ?o", con.execute(q).fetchone()[0]))

    print(f"    Ontop 으로 {len(jobs)}개 개수 비교 중...", flush=True)
    with ThreadPoolExecutor(4) as ex:
        got = list(ex.map(lambda j: count_sparql(j[1]), jobs))
    bad = 0
    for (name, _, want), have in zip(jobs, got):
        if have != want:
            bad += 1
            print(f"    ✗ {name}: Ontop {have} / DB {want}")
            problems.append(f"개수 불일치 {name}: Ontop {have} / DB {want}")
    print(f"    개수 일치 {len(jobs) - bad}/{len(jobs)}")
    print("\n" + ("문제 없음" if not problems else f"문제 {len(problems)}건"))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
