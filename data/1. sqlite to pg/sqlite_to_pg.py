#!/usr/bin/env python3
"""SQLite → PostgreSQL 무손실 이전 + 검증 스크립트.

사용법 (프로젝트 루트에서)
  python "data/1. sqlite to pg/sqlite_to_pg.py" convert <DB 이름> --dsn "postgresql://<user>@localhost:5432/<DB>"
  <DB 이름>(예: movie_3)은 data/0. bird_structure_types_check/train_databases/<DB>/<DB>.sqlite 를 가리킨다.
  다른 위치의 파일을 쓰려면 .sqlite 파일 경로를 그대로 줘도 된다.
  변환이 끝나면 원본 SQLite 와 행 단위로 바로 대조(검증)한다. 화면에 나온 내용은 <DB>.log 에 남는다.

원칙
  - SQLite 원본은 읽기 전용으로 연다 (원본은 절대 수정하지 않음).
  - 모든 행·값을 그대로 옮긴다. 컬럼 타입은 선언 타입이 아니라 실제 저장된 값의 종류로 정한다
    (예: DATE로 선언됐지만 텍스트로 저장된 값 → text. 날짜 형식이 바뀌지 않음).
  - PK, 복합 PK, UNIQUE, NOT NULL, 기본값, 인덱스, AUTOINCREMENT(시퀀스), FK와 FK 동작
    (ON UPDATE / ON DELETE)을 옮긴다.
  - FK는 데이터를 다 넣은 뒤 추가한다 (서로 참조하는 FK도 가능). 기존 데이터가 FK를 어기면
    행을 버리지 않고 NOT VALID로 선언한다 (이후 새로 들어오는 데이터만 검사됨).
  - 옮길 수 없는 것이 있으면 조용히 넘어가지 않고 중단한다. 경고 사항과 원본 CREATE 문은
    로그(<DB>.log)에 모두 남긴다. DB 안에는 메타데이터 테이블을 만들지 않는다.
  - 변환 전체가 하나의 트랜잭션이라, 중간에 실패하면 아무것도 남지 않는다.
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg
from psycopg import sql

PG_NAME_MAX = 63
ACTIONS = {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}
PG_ACTION_CODE = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
SQLITE_NOW = {  # SQLite가 기본값으로 넣는 텍스트 형식을 그대로 재현 (UTC)
    "CURRENT_TIMESTAMP": "to_char(timezone('UTC', now()), 'YYYY-MM-DD HH24:MI:SS')",
    "CURRENT_DATE": "to_char(timezone('UTC', now()), 'YYYY-MM-DD')",
    "CURRENT_TIME": "to_char(timezone('UTC', now()), 'HH24:MI:SS')",
}
NUM_RE = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")


class MigrationError(Exception):
    pass


# BIRD train 데이터베이스가 풀려 있는 폴더 (<DB>/<DB>.sqlite)
BIRD_DIR = Path(__file__).resolve().parents[1] / "0. bird_structure_types_check" / "train_databases"


def resolve_sqlite(arg):
    """DB 이름이면 BIRD_DIR/<DB>/<DB>.sqlite, 파일 경로이면 그대로 돌려준다."""
    p = Path(arg)
    if p.is_file():
        return p
    name = p.name[:-7] if p.name.lower().endswith(".sqlite") else p.name
    for cand in (BIRD_DIR / name / f"{name}.sqlite", BIRD_DIR / f"{name}.sqlite"):
        if cand.is_file():
            return cand
    raise MigrationError(f"SQLite 를 찾지 못했습니다: {arg}  (찾은 위치: {BIRD_DIR / name / (name + '.sqlite')})")


def qident(name):
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------- SQLite 읽기
def open_sqlite(path):
    p = Path(path).resolve()
    if not p.is_file():
        raise MigrationError(f"SQLite 파일이 없습니다: {p}")
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def read_sqlite_schema(con, allow_skip):
    """테이블·컬럼·키·인덱스·FK 정보를 PRAGMA로 읽는다."""
    objs = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    skipped, tables = [], {}
    for typ, name, _tbl, ddl in objs:
        if name.startswith("sqlite_"):
            continue
        if typ in ("view", "trigger") or (typ == "table" and ddl and re.match(r"\s*CREATE\s+VIRTUAL", ddl, re.I)):
            skipped.append({"type": typ, "name": name, "sql": ddl})
    if skipped and not allow_skip:
        raise MigrationError("옮길 수 없는 객체가 있습니다 (뷰/트리거/가상 테이블). "
                             "--allow-skip을 주면 리포트에 기록하고 건너뜁니다: "
                             + ", ".join(f"{s['type']} {s['name']}" for s in skipped))
    skip_names = {s["name"] for s in skipped}
    seqs = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        seqs = dict(con.execute("SELECT name, seq FROM sqlite_sequence").fetchall())

    for typ, name, _tbl, ddl in objs:
        if typ != "table" or name.startswith("sqlite_") or name in skip_names:
            continue
        cols = []
        for cid, cname, ctype, notnull, dflt, pk, hidden in con.execute(f"PRAGMA table_xinfo({qident(name)})"):
            if hidden:
                raise MigrationError(f"{name}.{cname}: 생성 컬럼/숨김 컬럼은 지원하지 않습니다.")
            cols.append(dict(name=cname, decl=ctype or "", notnull=bool(notnull), default=dflt, pk=pk))
        pk_cols = [c["name"] for c in sorted((c for c in cols if c["pk"]), key=lambda c: c["pk"])]
        rowid_alias = (len(pk_cols) == 1 and next(c for c in cols if c["name"] == pk_cols[0])["decl"].upper() == "INTEGER"
                       and not re.search(r"WITHOUT\s+ROWID", ddl or "", re.I))
        uniques, indexes = [], []
        for _seq, iname, unique, origin, partial in con.execute(f"PRAGMA index_list({qident(name)})"):
            if origin == "pk":
                continue
            if partial:
                raise MigrationError(f"{name}: 부분 인덱스({iname})는 지원하지 않습니다.")
            icols = []
            for _seqno, icid, icname, desc, coll, key in con.execute(f"PRAGMA index_xinfo({qident(iname)})"):
                if not key:
                    continue
                if icid == -2:
                    raise MigrationError(f"{name}: 식(expression) 인덱스({iname})는 지원하지 않습니다.")
                if coll and coll.upper() != "BINARY":
                    raise MigrationError(f"{name}: 인덱스 {iname}의 COLLATE {coll}은 PostgreSQL과 비교 규칙이 달라 지원하지 않습니다.")
                icols.append((icname, bool(desc)))
            if origin == "u":
                uniques.append([c for c, _ in icols])
            else:
                indexes.append(dict(name=iname, unique=bool(unique), cols=icols))
        fks = defaultdict(list)
        for fid, _seq, parent, frm, to, on_upd, on_del, match in con.execute(f"PRAGMA foreign_key_list({qident(name)})"):
            fks[fid].append(dict(parent=parent, frm=frm, to=to, on_update=on_upd, on_delete=on_del, match=match))
        warnings = []
        if ddl and re.search(r"\bCHECK\s*\(", ddl, re.I):
            raise MigrationError(f"{name}: CHECK 제약은 자동 변환하지 않습니다. 원본 CREATE 문을 확인해 주세요.")
        if ddl and re.search(r"\bCOLLATE\b", ddl, re.I):
            raise MigrationError(f"{name}: COLLATE가 있는 컬럼은 PostgreSQL과 비교 규칙이 달라 지원하지 않습니다.")
        tables[name] = dict(name=name, sql=ddl, cols=cols, pk=pk_cols, rowid_alias=rowid_alias,
                            autoincrement=name in seqs, seq=seqs.get(name, 0), uniques=uniques,
                            indexes=indexes, fks=[fks[k] for k in sorted(fks)], warnings=warnings)
    # FK의 부모 컬럼 생략(= 부모 PK) 해석
    for t in tables.values():
        for fk in t["fks"]:
            parent = fk[0]["parent"]
            if parent not in tables:
                raise MigrationError(f"{t['name']}: FK가 존재하지 않는 테이블 {parent}을 참조합니다.")
            if any(p["to"] is None for p in fk):
                ppk = tables[parent]["pk"]
                if len(ppk) != len(fk):
                    raise MigrationError(f"{t['name']} → {parent}: 참조 컬럼이 생략됐는데 부모 PK와 컬럼 수가 다릅니다.")
                for p, col in zip(fk, ppk):
                    p["to"] = col
    return tables, skipped


def profile_table(con, t):
    """한 번 훑으며 각 컬럼에 실제로 저장된 값의 종류를 확인한다."""
    names = [c["name"] for c in t["cols"]]
    kinds = [set() for _ in names]
    max_int = [0] * len(names)
    pk_has_null = False
    pk_idx = [names.index(c) for c in t["pk"]]
    n = 0
    for row in con.execute(f"SELECT {', '.join(qident(c) for c in names)} FROM {qident(t['name'])}"):
        n += 1
        for i, v in enumerate(row):
            if v is None:
                continue
            k = type(v).__name__
            kinds[i].add(k)
            if k == "int":
                max_int[i] = max(max_int[i], abs(v))
            elif k == "str" and "\x00" in v:
                raise MigrationError(f"{t['name']}.{names[i]}: 텍스트에 NUL 문자(\\x00)가 있어 PostgreSQL text에 넣을 수 없습니다.")
        if any(row[i] is None for i in pk_idx):
            pk_has_null = True
    return n, kinds, max_int, pk_has_null


def decide_type(table, col, kinds, max_int, mixed_as_text):
    if not kinds:  # 값이 전부 NULL → 선언 타입의 affinity로 결정
        d = col["decl"].upper()
        if "INT" in d:
            return "bigint"
        if any(x in d for x in ("REAL", "FLOA", "DOUB")):
            return "double precision"
        if "BLOB" in d:
            return "bytea"
        return "text"
    if kinds == {"int"}:
        return "bigint"
    if kinds <= {"int", "float"}:
        if max_int > 2 ** 53:
            raise MigrationError(f"{table}.{col['name']}: 정수와 실수가 섞여 있고 정수가 2^53을 넘어 double로 옮기면 값이 바뀝니다.")
        return "double precision"
    if kinds == {"str"}:
        return "text"
    if kinds == {"bytes"}:
        return "bytea"
    if mixed_as_text and "bytes" not in kinds:
        return "text"
    raise MigrationError(f"{table}.{col['name']}: 한 컬럼에 여러 종류의 값이 섞여 있습니다 ({sorted(kinds)}). "
                         "--mixed-as-text를 주면 text로 옮깁니다 (값은 보존, 숫자 타입 정보는 사라짐).")


def convert_default(table, col, pgtype):
    d = col["default"]
    if d is None:
        return None
    u = d.strip()
    if u.upper() == "NULL":
        return "NULL"
    if u.upper() in SQLITE_NOW:
        if pgtype != "text":
            raise MigrationError(f"{table}.{col['name']}: {u} 기본값이 text가 아닌 컬럼({pgtype})에 있습니다.")
        return SQLITE_NOW[u.upper()]
    if NUM_RE.match(u):
        return u if pgtype in ("bigint", "double precision") else "'" + u + "'"
    if len(u) >= 2 and u[0] == "'" and u[-1] == "'":
        if pgtype == "bytea":
            raise MigrationError(f"{table}.{col['name']}: bytea 컬럼의 문자열 기본값은 지원하지 않습니다.")
        return u
    raise MigrationError(f"{table}.{col['name']}: 기본값 {u!r}은 자동 변환하지 않습니다.")


# ---------------------------------------------------------------- 변환
def build_name_map(tables, lowercase):
    tmap, cmap = {}, {}
    for t in tables.values():
        tn = t["name"].lower() if lowercase else t["name"]
        tmap[t["name"]] = tn
        cmap[t["name"]] = {}
        for c in t["cols"]:
            cmap[t["name"]][c["name"]] = c["name"].lower() if lowercase else c["name"]
        if len(set(cmap[t["name"]].values())) != len(t["cols"]):
            raise MigrationError(f"{t['name']}: 소문자로 바꾸면 이름이 겹치는 컬럼이 있습니다.")
    if len(set(tmap.values())) != len(tmap):
        raise MigrationError("소문자로 바꾸면 이름이 겹치는 테이블이 있습니다.")
    for name in list(tmap.values()) + [c for m in cmap.values() for c in m.values()]:
        if len(name.encode()) > PG_NAME_MAX:
            raise MigrationError(f"이름이 PostgreSQL 최대 길이(63바이트)를 넘어 잘립니다: {name}")
    return tmap, cmap


def convert(args):
    con = open_sqlite(args.sqlite)
    tables, skipped = read_sqlite_schema(con, args.allow_skip)
    tmap, cmap = build_name_map(tables, args.lowercase_identifiers)
    schema = args.schema
    T = lambda t: sql.Identifier(schema, tmap[t])
    C = lambda t, c: sql.Identifier(cmap[t][c])

    print(f"[1/6] 데이터 확인: {len(tables)}개 테이블")
    for t in tables.values():
        n, kinds, max_int, pk_has_null = profile_table(con, t)
        t["rows"] = n
        for i, c in enumerate(t["cols"]):
            c["pgtype"] = decide_type(t["name"], c, kinds[i], max_int[i], args.mixed_as_text)
            c["kinds"] = sorted(kinds[i])
            c["pg_default"] = convert_default(t["name"], c, c["pgtype"])
        t["pk_as_unique"] = bool(t["pk"]) and pk_has_null
        if t["pk_as_unique"]:
            t["warnings"].append("PK 컬럼에 NULL 값이 있어 PRIMARY KEY 대신 UNIQUE로 옮겼습니다 (SQLite는 허용, PostgreSQL PK는 불가).")
        if any(len(c["kinds"]) > 1 for c in t["cols"]):
            t["warnings"].append("한 컬럼에 정수/실수 등 여러 종류의 값이 있어 넓은 타입으로 옮긴 컬럼이 있습니다: "
                                 + ", ".join(f"{c['name']}({'/'.join(c['kinds'])}→{c['pgtype']})" for c in t["cols"] if len(c["kinds"]) > 1))

    fk_report = []
    with psycopg.connect(args.dsn, autocommit=True) as pg:
        with pg.transaction():
            cur = pg.cursor()
            if schema != "public":
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            existing = {r[0] for r in cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))}
            clash = [tmap[t] for t in tables if tmap[t] in existing]
            if clash and not args.replace:
                raise MigrationError(f"대상 스키마 {schema}에 같은 이름의 테이블이 이미 있습니다: {clash}. "
                                     "지우고 새로 만들려면 --replace를 주세요.")
            for name in clash:
                cur.execute(sql.SQL("DROP TABLE {} CASCADE").format(sql.Identifier(schema, name)))

            print("[2/6] 테이블 생성")
            for t in tables.values():
                coldefs = []
                for c in t["cols"]:
                    parts = [C(t["name"], c["name"]), sql.SQL(c["pgtype"])]
                    if c["notnull"]:
                        parts.append(sql.SQL("NOT NULL"))
                    if c["pg_default"] is not None:
                        parts.append(sql.SQL("DEFAULT " + c["pg_default"]))
                    coldefs.append(sql.SQL(" ").join(parts))
                cur.execute(sql.SQL("CREATE TABLE {} ({})").format(T(t["name"]), sql.SQL(", ").join(coldefs)))

            print("[3/6] 데이터 적재")
            for t in tables.values():
                names = [c["name"] for c in t["cols"]]
                fixers = [(float if c["pgtype"] == "double precision" else str if c["pgtype"] == "text" else None)
                          for c in t["cols"]]
                q = sql.SQL("COPY {} ({}) FROM STDIN").format(T(t["name"]), sql.SQL(", ").join(C(t["name"], n) for n in names))
                with cur.copy(q) as cp:
                    for row in con.execute(f"SELECT {', '.join(qident(n) for n in names)} FROM {qident(t['name'])}"):
                        cp.write_row([v if v is None or f is None else f(v) for v, f in zip(row, fixers)])
                got = cur.execute(sql.SQL("SELECT count(*) FROM {}").format(T(t["name"]))).fetchone()[0]
                if got != t["rows"]:
                    raise MigrationError(f"{t['name']}: 적재 행 수 불일치 (SQLite {t['rows']} / PostgreSQL {got})")
                print(f"    {t['name']}: {got:,}행")

            print("[4/6] PK·UNIQUE·인덱스·시퀀스")
            for t in tables.values():
                if t["pk"]:
                    kind = "UNIQUE" if t["pk_as_unique"] else "PRIMARY KEY"
                    cur.execute(sql.SQL("ALTER TABLE {} ADD " + kind + " ({})").format(
                        T(t["name"]), sql.SQL(", ").join(C(t["name"], c) for c in t["pk"])))
                for u in t["uniques"]:
                    cur.execute(sql.SQL("ALTER TABLE {} ADD UNIQUE ({})").format(
                        T(t["name"]), sql.SQL(", ").join(C(t["name"], c) for c in u)))
                for ix in t["indexes"]:
                    cur.execute(sql.SQL("CREATE " + ("UNIQUE " if ix["unique"] else "") + "INDEX {} ON {} ({})").format(
                        sql.Identifier(ix["name"]), T(t["name"]),
                        sql.SQL(", ").join(sql.SQL("{}{}").format(C(t["name"], c), sql.SQL(" DESC" if d else "")) for c, d in ix["cols"])))
                if t["rowid_alias"] and not t["pk_as_unique"]:
                    col = t["pk"][0]
                    cur.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} ADD GENERATED BY DEFAULT AS IDENTITY").format(T(t["name"]), C(t["name"], col)))
                    mx = cur.execute(sql.SQL("SELECT coalesce(max({}), 0) FROM {}").format(C(t["name"], col), T(t["name"]))).fetchone()[0]
                    last = max(mx, t["seq"])  # SQLite 다음 값 = max(sqlite_sequence, 최대 rowid) + 1
                    seqname = cur.execute("SELECT pg_get_serial_sequence(%s, %s)",
                                          (T(t["name"]).as_string(pg), cmap[t["name"]][col])).fetchone()[0]
                    if last > 0:
                        cur.execute("SELECT setval(%s, %s, true)", (seqname, last))
                    else:
                        cur.execute("SELECT setval(%s, 1, false)", (seqname,))
                    t["next_id"] = last + 1

            print("[5/6] FK")
            skipped_fks = []
            for t in tables.values():
                for i, fk in enumerate(t["fks"]):
                    parent = fk[0]["parent"]
                    frm = [p["frm"] for p in fk]
                    to = [p["to"] for p in fk]
                    on_upd, on_del = fk[0]["on_update"].upper(), fk[0]["on_delete"].upper()
                    if on_upd not in ACTIONS or on_del not in ACTIONS:
                        raise MigrationError(f"{t['name']}: 알 수 없는 FK 동작 {on_upd}/{on_del}")
                    if fk[0]["match"].upper() not in ("NONE", "SIMPLE"):
                        raise MigrationError(f"{t['name']}: FK MATCH {fk[0]['match']}는 지원하지 않습니다.")
                    rec = dict(table=t["name"], columns=frm, ref_table=parent, ref_columns=to,
                               on_update=on_upd, on_delete=on_del)
                    ptab = tables[parent]
                    keysets = ([ptab["pk"]] if ptab["pk"] else []) + ptab["uniques"] + [[c for c, _ in ix["cols"]] for ix in ptab["indexes"] if ix["unique"]]
                    if not any(set(k) == set(to) for k in keysets):
                        if args.skip_invalid_fk:
                            skipped_fks.append(dict(table=t["name"], columns=frm, ref_table=parent, ref_columns=to))
                            print(f"    ! {t['name']}({', '.join(frm)}) → {parent}({', '.join(to)}): 참조 대상에 PK/UNIQUE가 없어 "
                                  "(원본 SQLite 에서도 'foreign key mismatch') 건너뜀")
                            continue
                        raise MigrationError(f"{t['name']} → {parent}({', '.join(to)}): 참조 대상 컬럼에 PK/UNIQUE가 없어 "
                                             "PostgreSQL에서 FK를 만들 수 없습니다 (SQLite에서도 'foreign key mismatch' 상태).")
                    ctypes = {c["name"]: c["pgtype"] for c in t["cols"]}
                    ptypes = {c["name"]: c["pgtype"] for c in ptab["cols"]}
                    for a, b in zip(frm, to):
                        if ctypes[a] != ptypes[b]:
                            raise MigrationError(f"{t['name']}.{a}({ctypes[a]}) → {parent}.{b}({ptypes[b]}): "
                                                 "FK 양쪽 타입이 달라 PostgreSQL FK를 만들 수 없습니다.")
                    cond = sql.SQL(" AND ").join(sql.SQL("c.{} IS NOT NULL").format(C(t["name"], a)) for a in frm)
                    join = sql.SQL(" AND ").join(sql.SQL("p.{} = c.{}").format(C(parent, b), C(t["name"], a)) for a, b in zip(frm, to))
                    viol = cur.execute(sql.SQL("SELECT count(*) FROM {} c WHERE {} AND NOT EXISTS (SELECT 1 FROM {} p WHERE {})").format(
                        T(t["name"]), cond, T(parent), join)).fetchone()[0]
                    cname = f"{tmap[t['name']]}_fk{i}"[:PG_NAME_MAX]
                    cur.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}) ON UPDATE "
                                        + on_upd + " ON DELETE " + on_del + (" NOT VALID" if viol else "")).format(
                        T(t["name"]), sql.Identifier(cname), sql.SQL(", ").join(C(t["name"], a) for a in frm),
                        T(parent), sql.SQL(", ").join(C(parent, b) for b in to)))
                    rec.update(constraint=cname, violations=viol, status="NOT VALID" if viol else "VALID")
                    fk_report.append(rec)
                    if viol:
                        print(f"    ! {t['name']}({', '.join(frm)}) → {parent}: 기존 데이터 {viol:,}행이 FK를 어겨 NOT VALID로 선언")

            print("[6/6] 통계 갱신")
            for t in tables.values():
                cur.execute(sql.SQL("ANALYZE {}").format(T(t["name"])))

    report = dict(
        sqlite=str(Path(args.sqlite).resolve()), schema=schema, lowercase_identifiers=args.lowercase_identifiers,
        skipped_objects=skipped,
        tables=[dict(sqlite_name=t["name"], pg_name=tmap[t["name"]], rows=t["rows"], original_sql=t["sql"],
                     primary_key=t["pk"], primary_key_as_unique=t["pk_as_unique"], uniques=t["uniques"],
                     indexes=t["indexes"], identity_next_value=t.get("next_id"), warnings=t["warnings"],
                     columns=[dict(sqlite_name=c["name"], pg_name=cmap[t["name"]][c["name"]], declared_type=c["decl"],
                                   stored_kinds=c["kinds"], pg_type=c["pgtype"], not_null=c["notnull"],
                                   sqlite_default=c["default"], pg_default=c["pg_default"]) for c in t["cols"]])
                for t in tables.values()],
        foreign_keys=fk_report, skipped_foreign_keys=skipped_fks)
    nv = [f for f in fk_report if f["status"] != "VALID"]
    for sf in skipped_fks:
        print(f"  건너뛴 FK: {sf['table']}({', '.join(sf['columns'])}) → {sf['ref_table']}({', '.join(sf['ref_columns'])})")
    print(f"\n완료: 테이블 {len(tables)}개, 행 {sum(t['rows'] for t in tables.values()):,}개, FK {len(fk_report)}개"
          f" (NOT VALID {len(nv)}개), 건너뛴 객체 {len(skipped)}개")
    for t in tables.values():
        for w in t["warnings"]:
            print(f"  경고 [{t['name']}] {w}")
    for obj in skipped:
        print(f"  건너뜀: {obj}")
    print("\n원본 CREATE 문 (SQLite)")
    for t in tables.values():
        print(f"  [{t['name']}] {' '.join(str(t['sql']).split())}")
    print("\n행 단위 검증")
    verify(args, report)


# ---------------------------------------------------------------- 검증
def row_digest(row):
    return hashlib.blake2b(repr(row).encode(), digest_size=16).digest()


def verify(args, report):
    schema = report["schema"]
    con = open_sqlite(args.sqlite)
    tables, _ = read_sqlite_schema(con, allow_skip=True)
    problems = []

    def bad(msg):
        problems.append(msg)
        print("  ✗ " + msg)

    rep_tables = {t["sqlite_name"]: t for t in report["tables"]}
    if set(rep_tables) != set(tables):
        bad(f"테이블 목록 불일치: SQLite {sorted(tables)} / 리포트 {sorted(rep_tables)}")

    with psycopg.connect(args.dsn, autocommit=True) as pg, pg.transaction():
        cur = pg.cursor()
        pg_tables = {r[0] for r in cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'", (schema,))}
        expected = {t["pg_name"] for t in rep_tables.values()}
        if not expected <= pg_tables:
            bad(f"PostgreSQL에 없는 테이블: {sorted(expected - pg_tables)}")

        for name, t in tables.items():
            rt = rep_tables.get(name)
            if rt is None or rt["pg_name"] not in pg_tables:
                continue
            print(f"[{name}]")
            relid = cur.execute("SELECT %s::regclass::oid", (sql.Identifier(schema, rt["pg_name"]).as_string(pg),)).fetchone()[0]
            # 컬럼: 이름·순서·타입·NOT NULL·기본값
            pgcols = cur.execute("""SELECT attname, format_type(atttypid, atttypmod), attnotnull, pg_get_expr(d.adbin, d.adrelid), attidentity
                                    FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                                    WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped ORDER BY attnum""", (relid,)).fetchall()
            want = [(c["pg_name"], c["pg_type"]) for c in rt["columns"]]
            if [(r[0], r[1]) for r in pgcols] != want:
                bad(f"컬럼 이름/순서/타입 불일치: {[(r[0], r[1]) for r in pgcols]} / 기대 {want}")
            if [c["name"] for c in t["cols"]] != [c["sqlite_name"] for c in rt["columns"]]:
                bad("SQLite 원본 컬럼이 리포트와 다릅니다 (변환 이후 원본이 바뀌었을 수 있음).")
            pk_set = set(t["pk"]) if t["pk"] and not rt["primary_key_as_unique"] else set()
            for c, rc, r in zip(t["cols"], rt["columns"], pgcols):
                if r[2] != (c["notnull"] or c["name"] in pk_set):
                    bad(f"{c['name']}: NOT NULL 불일치 (SQLite {c['notnull']} / PostgreSQL {r[2]})")
                has_default = c["default"] is not None and c["default"].strip().upper() != "NULL"   # DEFAULT NULL 은 '기본값 없음'과 같다
                if r[4] == "" and has_default != (r[3] is not None):
                    bad(f"{c['name']}: 기본값 유무 불일치 (SQLite {c['default']!r} / PostgreSQL {r[3]!r})")

            # 키·인덱스
            cmap = {c["sqlite_name"]: c["pg_name"] for c in rt["columns"]}
            cons = cur.execute("""SELECT contype,
                                         array(SELECT attname FROM unnest(conkey) WITH ORDINALITY k(n, o)
                                               JOIN pg_attribute ON attrelid = conrelid AND attnum = k.n ORDER BY k.o),
                                         confrelid::oid,
                                         array(SELECT attname FROM unnest(confkey) WITH ORDINALITY k(n, o)
                                               JOIN pg_attribute ON attrelid = confrelid AND attnum = k.n ORDER BY k.o),
                                         confupdtype, confdeltype, convalidated
                                  FROM pg_constraint WHERE conrelid = %s""", (relid,)).fetchall()
            pks = [set(r[1]) for r in cons if r[0] == "p"]
            uqs = sorted(sorted(r[1]) for r in cons if r[0] == "u")
            want_uq = sorted(sorted(cmap[c] for c in u) for u in t["uniques"] + ([t["pk"]] if rt["primary_key_as_unique"] else []))
            if t["pk"] and not rt["primary_key_as_unique"] and pks != [{cmap[c] for c in t["pk"]}]:
                bad(f"PK 불일치: {pks} / 기대 {t['pk']}")
            if uqs != want_uq:
                bad(f"UNIQUE 불일치: {uqs} / 기대 {want_uq}")
            for ix in t["indexes"]:
                if not cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s", (schema, ix["name"])).fetchone():
                    bad(f"인덱스 {ix['name']} 없음")
            if rt.get("identity_next_value"):
                col = cmap[t["pk"][0]]
                seq = cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (sql.Identifier(schema, rt["pg_name"]).as_string(pg), col)).fetchone()[0]
                last, called = cur.execute(sql.SQL("SELECT last_value, is_called FROM {}").format(sql.SQL(seq))).fetchone()
                nxt = last + 1 if called else last
                if nxt < rt["identity_next_value"]:  # 변환 후 INSERT가 있었다면 더 클 수 있음 (정상)
                    bad(f"다음 자동 증가 값이 원본보다 작음: {nxt} / 기대 {rt['identity_next_value']} 이상 (기존 id와 충돌 위험)")

            # FK
            rep_fk = {(tuple(f["columns"]), f["ref_table"], tuple(f["ref_columns"])): f for f in report["foreign_keys"] if f["table"] == name}
            pg_fk = [r for r in cons if r[0] == "f"]
            skip_keys = {(tuple(sf["columns"]), sf["ref_table"], tuple(sf["ref_columns"]))
                         for sf in report.get("skipped_foreign_keys", []) if sf["table"] == name}      # 일부러 건너뛴 FK
            fks_eff = [fk for fk in t["fks"] if (tuple(p["frm"] for p in fk), fk[0]["parent"], tuple(p["to"] for p in fk)) not in skip_keys]
            if len(pg_fk) != len(fks_eff):
                bad(f"FK 개수 불일치: PostgreSQL {len(pg_fk)} / SQLite {len(fks_eff)}")
            ptmap = {x["sqlite_name"]: x for x in report["tables"]}
            for fk in fks_eff:
                key = (tuple(p["frm"] for p in fk), fk[0]["parent"], tuple(p["to"] for p in fk))
                f = rep_fk.get(key)
                pmap = {c["sqlite_name"]: c["pg_name"] for c in ptmap[fk[0]["parent"]]["columns"]}
                ref_oid = cur.execute("SELECT %s::regclass::oid", (sql.Identifier(schema, ptmap[fk[0]["parent"]]["pg_name"]).as_string(pg),)).fetchone()[0]
                match = [r for r in pg_fk if r[1] == [cmap[c] for c in key[0]] and r[3] == [pmap[c] for c in key[2]] and r[2] == ref_oid]
                if not match or f is None:
                    bad(f"FK {key} 없음")
                    continue
                r = match[0]
                if (PG_ACTION_CODE[r[4]], PG_ACTION_CODE[r[5]]) != (fk[0]["on_update"].upper(), fk[0]["on_delete"].upper()):
                    bad(f"FK {key} 동작 불일치: {PG_ACTION_CODE[r[4]]}/{PG_ACTION_CODE[r[5]]}")
                if r[6] != (f["status"] == "VALID"):
                    bad(f"FK {key} 검증 상태 불일치 (리포트 {f['status']})")

            # 데이터: 모든 행을 해시해서 다중집합으로 비교 (순서 무관, 중복 행 개수까지 비교)
            names = [c["name"] for c in t["cols"]]
            fixers = [(float if c["pg_type"] == "double precision" else str if c["pg_type"] == "text" else None) for c in rt["columns"]]
            src = Counter()
            for row in con.execute(f"SELECT {', '.join(qident(n) for n in names)} FROM {qident(name)}"):
                src[row_digest(tuple(v if v is None or fx is None else fx(v) for v, fx in zip(row, fixers)))] += 1
            dst = Counter()
            with pg.cursor(name=f"v_{abs(hash(name))}") as sc:
                sc.itersize = 20000
                sc.execute(sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(sql.Identifier(cmap[n]) for n in names),
                                                                sql.Identifier(schema, rt["pg_name"])))
                for row in sc:
                    dst[row_digest(tuple(bytes(v) if isinstance(v, memoryview) else v for v in row))] += 1
            ns, nd = sum(src.values()), sum(dst.values())
            missing, extra = sum((src - dst).values()), sum((dst - src).values())
            if missing or extra or ns != nd:
                bad(f"데이터 불일치: SQLite {ns:,}행 / PostgreSQL {nd:,}행, PostgreSQL에 없는 행 {missing:,}, 원본에 없는 행 {extra:,}")
            else:
                print(f"  ✓ {ns:,}행 전부 일치, 컬럼 {len(names)}개, FK {len(t['fks'])}개")

    print()
    if problems:
        print(f"검증 실패: 문제 {len(problems)}건")
        sys.exit(1)
    print("검증 통과: 모든 테이블의 스키마·제약·행 값이 원본과 일치합니다.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="SQLite를 PostgreSQL로 옮긴다")
    c.add_argument("sqlite", help="DB 이름(예: movie_3) 또는 .sqlite 파일 경로")
    c.add_argument("--dsn", required=True, help='예: "postgresql://user@localhost:5432/synthea"')
    c.add_argument("--schema", default="public")
    c.add_argument("--lowercase-identifiers", action="store_true",
                   help="테이블·컬럼 이름을 소문자로 옮긴다 (따옴표 없는 SQL에서 대문자 이름을 쓰기 위함)")
    c.add_argument("--replace", action="store_true", help="같은 이름의 테이블이 있으면 지우고 새로 만든다")
    c.add_argument("--skip-invalid-fk", action="store_true",
                   help="참조 대상 컬럼에 PK/UNIQUE 가 없어 원본 SQLite 에서도 깨진 FK 를 건너뛰고 로그에 기록한다 (기본은 중단)")
    c.add_argument("--allow-skip", action="store_true", help="뷰/트리거/가상 테이블을 로그에 기록하고 건너뛴다")
    c.add_argument("--mixed-as-text", action="store_true", help="여러 종류의 값이 섞인 컬럼을 text로 옮긴다")
    args = ap.parse_args()
    try:
        args.sqlite = resolve_sqlite(args.sqlite)
    except MigrationError as e:
        sys.exit(f"중단: {e}")
    log_path = Path(__file__).resolve().parent / f"{Path(args.sqlite).stem}.log"   # 화면에 나오는 내용을 그대로 이 폴더의 <DB>.log 에 남긴다 (매번 새로 씀)
    log_file = open(log_path, "w", encoding="utf-8")

    class Tee:
        def __init__(self, stream):
            self.stream = stream

        def write(self, text):
            self.stream.write(text)
            log_file.write(text)

        def flush(self):
            self.stream.flush()
            log_file.flush()

    sys.stdout, sys.stderr = Tee(sys.stdout), Tee(sys.stderr)
    try:
        convert(args)
    except MigrationError as e:
        print(f"\n중단: {e}\n(변환은 하나의 트랜잭션이라 PostgreSQL에는 아무것도 남지 않았습니다.)", file=sys.stderr)
        sys.exit(2)
    finally:
        print(f"로그: {log_path.resolve()}", file=sys.__stdout__)


if __name__ == "__main__":
    main()
