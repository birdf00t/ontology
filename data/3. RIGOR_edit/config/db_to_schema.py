"""
Live PostgreSQL database -> RIGOR schema JSON.

RIGOR never connects to a database: app.py only reads a schema JSON. So "running RIGOR on PostgreSQL" means
extracting tables / columns / types / PK / UNIQUE / FK from the server's catalog (pg_catalog), plus a value scan
for date-like text columns (see below).

    cd config
    python db_to_schema.py movie_3          # -> config/movie_3/schema_movie_3.json
    python db_to_schema.py synthea          # -> config/synthea/schema_synthea.json
    python db_to_schema.py DBNAME [--out FILE] [--host H] [--port P] [--user U] [--schema public]

Standalone: needs only psycopg2. The output defaults to <this script's folder>/<DBNAME>/schema_<DBNAME>.json
(the folder is created if missing), so it does not matter which directory the script is started from.

Connection: libpq defaults (local server, current user, socket/port 5432) unless --host/--port/--user are given;
PGPASSWORD is honoured.

Type normalisation ("raw_type" keeps the PostgreSQL type as reported, upper-cased):
  RIGOR's SQL_TO_XSD keys are single words and app.py takes the text before "(", so multi-word PostgreSQL
  types would fall back to xsd:string. They are mapped to the closest RIGOR key:
  double precision -> DOUBLE, character varying -> VARCHAR, timestamp with(out) time zone -> TIMESTAMP, ...

Date typing from the ACTUAL values:
  app.py fix_date_type() picks date types from the column NAME only, which gave wrong ranges
    movie_3  "last_update" (text, values "YYYY-MM-DD HH:MM:SS.0") -> xsd:date      (should be xsd:dateTime)
    synthea  "START"/"STOP" (text, values "YYYY-MM-DD")           -> xsd:string    (should be xsd:date)
  A non-key text/varchar/char column whose every non-NULL value matches one pattern (and has >=1 value) gets
    all values YYYY-MM-DD HH:MM:SS(.f) -> "type": "TIMESTAMP"  (xsd:dateTime)
    all values YYYY-MM-DD              -> "type": "DATE"       (xsd:date)
  "raw_type" stays TEXT, and the column gets "type_inferred": "<n> values" so the change is visible.
  Columns with mixed or no values are left alone. Every inference is printed.
"""

import argparse
import json
import re
from pathlib import Path

try:
    import psycopg2
except ImportError:   # [CHANGED] psycopg 3 (psycopg) 도 쓸 수 있게 한다
    import psycopg as psycopg2

HERE = Path(__file__).resolve().parent

PG_TO_RIGOR = {
    "bigint": "BIGINT", "integer": "INTEGER", "int": "INTEGER", "smallint": "SMALLINT",
    "double precision": "DOUBLE", "real": "FLOAT", "numeric": "NUMERIC", "decimal": "DECIMAL",
    "text": "TEXT", "character varying": "VARCHAR", "varchar": "VARCHAR", "character": "CHAR", "char": "CHAR",
    "boolean": "BOOLEAN", "date": "DATE", "time without time zone": "TIME", "time": "TIME",
    "timestamp without time zone": "TIMESTAMP", "timestamp with time zone": "TIMESTAMP", "timestamp": "TIMESTAMP",
    "bytea": "BLOB",
}
FK_ACTION = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
TEXT_TYPES = {"TEXT", "VARCHAR", "CHAR"}
DATE_RULES = [  # (RIGOR type, value pattern); first rule whose pattern matches every value wins
    ("TIMESTAMP", r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$"),
    ("DATE", r"^\d{4}-\d{2}-\d{2}$"),
]

COLUMNS_SQL = """
select c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
from pg_attribute a join pg_class c on c.oid = a.attrelid join pg_namespace n on n.oid = c.relnamespace
where n.nspname = %s and c.relkind in ('r', 'p') and a.attnum > 0 and not a.attisdropped
order by c.relname, a.attnum
"""
CONSTRAINTS_SQL = """
select c.relname, k.contype, k.conname,
       (select array_agg(a.attname order by u.ord) from unnest(k.conkey) with ordinality u(attnum, ord)
          join pg_attribute a on a.attrelid = k.conrelid and a.attnum = u.attnum),
       r.relname,
       (select array_agg(a.attname order by u.ord) from unnest(k.confkey) with ordinality u(attnum, ord)
          join pg_attribute a on a.attrelid = k.confrelid and a.attnum = u.attnum),
       k.confupdtype, k.confdeltype, k.convalidated
from pg_constraint k join pg_class c on c.oid = k.conrelid join pg_namespace n on n.oid = c.relnamespace
left join pg_class r on r.oid = k.confrelid
where n.nspname = %s and k.contype in ('p', 'u', 'f')
order by c.relname, k.conname
"""


def extract(conn, pg_schema: str, skip_composite: bool = False, skipped: list = None) -> dict:
    cur = conn.cursor()
    schema = {}
    cur.execute(COLUMNS_SQL, (pg_schema,))
    for table, _, col, typ, notnull in cur.fetchall():
        base = re.sub(r"\(.*\)", "", typ).strip().lower()
        schema.setdefault(table, {"columns": {}, "foreign_keys": []})["columns"][col] = {
            "type": PG_TO_RIGOR.get(base, typ.upper()), "raw_type": typ.upper(),
            "nullable": not notnull, "primary_key": False, "unique": False, "default": None}
    cur.execute(CONSTRAINTS_SQL, (pg_schema,))
    for table, kind, _, cols, ref_table, ref_cols, upd, dele, validated in cur.fetchall():
        if kind in ("p", "u"):
            for c in cols:
                # a multi-column UNIQUE does not make each column unique on its own
                if kind == "p" or len(cols) == 1:
                    schema[table]["columns"][c]["primary_key" if kind == "p" else "unique"] = True
                if kind == "p":
                    schema[table]["columns"][c]["nullable"] = False
        else:
            if len(cols) != 1:
                if skip_composite:   # RIGOR 스키마 형식은 컬럼 하나짜리 FK 만 표현한다: 건너뛰고 목록을 남긴다
                    if skipped is not None:
                        skipped.append(f"{table}{list(cols)} -> {ref_table}{list(ref_cols)}")
                    continue
                raise SystemExit(f"composite foreign key on {table}{cols} is not supported by the RIGOR schema format "
                                 "(use --skip-composite-fk to leave composite FKs out and list them)")
            fk = {"column": cols[0], "references_table": ref_table, "references_column": ref_cols[0],
                  "on_update": FK_ACTION[upd], "on_delete": FK_ACTION[dele]}
            if not validated:
                fk["not_valid"] = True   # PostgreSQL did not check existing rows against this FK
            schema[table]["foreign_keys"].append(fk)
    return schema


def infer_date_types(conn, schema: dict) -> list:
    cur = conn.cursor()
    done = []
    for t, tb in schema.items():
        fk_cols = {fk["column"] for fk in tb["foreign_keys"]}
        for c, col in tb["columns"].items():
            if col["type"] not in TEXT_TYPES or col["primary_key"] or c in fk_cols:
                continue
            for new, pattern in DATE_RULES:
                cur.execute(f'select count(*) filter (where "{c}" is not null and "{c}" !~ %s), count("{c}") from "{t}"',
                            (pattern,))
                bad, n = cur.fetchone()
                if n and not bad:
                    col["type"], col["type_inferred"] = new, f"{n} values"
                    done.append(f"{t}.{c}: {col['raw_type']} -> {new} ({n} values)")
                    break
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dbname")
    ap.add_argument("--out", type=Path, help="output JSON (default: <script dir>/<dbname>/schema_<dbname>.json)")
    ap.add_argument("--host"); ap.add_argument("--port"); ap.add_argument("--user")
    ap.add_argument("--schema", default="public", help="PostgreSQL schema to read (default public)")
    ap.add_argument("--skip-composite-fk", action="store_true",
                    help="leave out composite (multi-column) foreign keys, which the RIGOR schema format cannot express, and print them")
    a = ap.parse_args()
    out = a.out or HERE / a.dbname / f"schema_{a.dbname}.json"
    conn = psycopg2.connect(dbname=a.dbname, host=a.host, port=a.port, user=a.user)
    skipped = []
    s = extract(conn, a.schema, a.skip_composite_fk, skipped)
    inferred = infer_date_types(conn, s)
    conn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(s, indent=2), encoding="utf-8")
    n_col = sum(len(t["columns"]) for t in s.values())
    n_pk = sum(c["primary_key"] for t in s.values() for c in t["columns"].values())
    n_fk = sum(len(t["foreign_keys"]) for t in s.values())
    print(f"{len(s)} tables, {n_col} columns, {n_pk} PK columns, {n_fk} FKs -> {out}")
    if skipped:
        print(f"{len(skipped)} composite FKs left out (not expressible in the RIGOR schema format):")
        for line in skipped:
            print("  " + line)
    types = {}
    for t in s.values():
        for c in t["columns"].values():
            types[(c["raw_type"], c["type"])] = types.get((c["raw_type"], c["type"]), 0) + 1
    for (raw, norm), n in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {n:3}  {raw:20} -> {norm}")
    print(f"{len(inferred)} text columns typed from their values:")
    for line in inferred:
        print("  " + line)


if __name__ == "__main__":
    main()
