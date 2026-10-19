import os
import subprocess
import tempfile
from pathlib import Path

import duckdb

DATA_PATH = "data/ACME_Insurance/"

DUCKDB_PATH = DATA_PATH + "*.duckdb" 
ONTOP = "tools/ontop-cli-5.5.0/ontop"
MAPPING = DATA_PATH+"*.r2rml"
ONTOLOGY = DATA_PATH+"*.insurance.ttl"
PROPERTIES = DATA_PATH+"acme.properties"
JAVA_HOME = "tools/jdk-21.0.12.1+1/Contents/Home"
MAX_ROWS = 50


def run_sql(query: str) -> str:
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        res = con.execute(query)
        cols = [d[0] for d in res.description]
        rows = res.fetchmany(MAX_ROWS)
        return "\t".join(cols) + "\n" + "\n".join("\t".join(map(str, r)) for r in rows)
    except Exception as e:
        return f"ERROR: {e}"
    finally:
        con.close()


def run_sparql(query: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        q, out = Path(tmp) / "q.rq", Path(tmp) / "out.csv"
        q.write_text(query, encoding="utf-8")
        env = {**os.environ, "JAVA_HOME": str(Path(JAVA_HOME).resolve())}
        p = subprocess.run(
            [ONTOP, "query", "-m", MAPPING, "-t", ONTOLOGY, "-p", PROPERTIES,
             "-q", str(q), "-o", str(out)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        if p.returncode != 0 or not out.exists():
            return "ERROR: " + (p.stderr or p.stdout)[-1500:]
        lines = out.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[: MAX_ROWS + 1])