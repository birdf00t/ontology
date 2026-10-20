import getpass
import os
import subprocess
import tempfile
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, PROV, RDF, RDFS, XSD

load_dotenv()  # .env 의 PG_* 값 (없으면 아래 기본값)

DB_NAME = os.getenv("PG_DB", "movie_3")  # "synthea" 또는 "movie_3"
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", getpass.getuser())
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

DATA_DIR = Path("data")
MAX_ROWS = 50
TIMEOUT_SEC = 120

# RIGOR 온톨로지의 기본 주소. 에이전트가 ont:Film 처럼 짧게 쓸 수 있게 접두어로 등록한다.
ONT_NS = "http://example.org/ontology#"
PREFIXES = {"ont": ONT_NS, "prov": str(PROV), "owl": str(OWL), "rdfs": str(RDFS),
            "rdf": str(RDF), "xsd": str(XSD)}


# ---------------------------------------------------------------- SQL (PostgreSQL)

def connect():
    # 읽기 전용 + 쿼리 시간 제한: 에이전트가 쓴 SQL이 DB를 바꾸거나 오래 붙잡지 못하게
    return psycopg.connect(
        host=PG_HOST, port=PG_PORT, dbname=DB_NAME, user=PG_USER, password=PG_PASSWORD or None,
        autocommit=True,
        options=f"-c default_transaction_read_only=on -c statement_timeout={TIMEOUT_SEC * 1000}",
    )


def run_sql(query: str) -> str:
    try:
        with connect() as con:
            res = con.execute(query)
            if res.description is None:
                return "ERROR: 결과 행이 없는 문장입니다. SELECT 문만 사용하세요."
            cols = [d.name for d in res.description]
            rows = res.fetchmany(MAX_ROWS)
            return "\t".join(cols) + "\n" + "\n".join("\t".join(map(str, r)) for r in rows)
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------- 온톨로지 (OWL, rdflib)

def ontology_file() -> Path:
    """data/<DB>.owl 또는 data/<DB>/<DB>.owl (.ttl 도 허용) 중 있는 것을 쓴다."""
    for folder in (DATA_DIR, DATA_DIR / DB_NAME):
        for ext in (".owl", ".ttl"):
            p = folder / f"{DB_NAME}{ext}"
            if p.exists():
                return p
    return DATA_DIR / f"{DB_NAME}.owl"


_graph = None


def load_ontology() -> Graph:
    """OWL 을 한 번만 읽어 둔다 (파일 형식은 확장자로 판단: .ttl 이면 turtle, 아니면 RDF/XML)."""
    global _graph
    if _graph is None:
        path = ontology_file()
        g = Graph()
        g.parse(path, format="turtle" if path.suffix == ".ttl" else "xml")
        _graph = g
    return _graph


def short(term) -> str:
    """에이전트에게 줄 때 주소를 짧게 줄인다 (토큰 절약).

    ont:Film → 'Film', prov 값(.../provenance/film/title) → 'film/title'
    """
    if isinstance(term, URIRef):
        s = str(term)
        if "/provenance/" in s:
            return s.split("/provenance/", 1)[1]
        if s.startswith(ONT_NS):
            return s[len(ONT_NS):]
        return s
    if isinstance(term, BNode):
        return "_:blank"
    if isinstance(term, Literal):
        return str(term)
    return "" if term is None else str(term)


# RIGOR 출력에는 제약(exactly/max/min)이 이름 있는 가짜 클래스로 들어 있는 경우가 있다.
# 스키마 탐색에 방해가 되므로 결과에서 뺀다.
NOISE = ("_exactly_", "_max_", "_min_", "Unnamed")


def run_ontology_sparql(query: str) -> str:
    """OWL 을 SPARQL SELECT 로 검색한다. 접두어 ont:, prov:, owl:, rdfs:, rdf:, xsd: 는 미리 선언돼 있다."""
    try:
        g = load_ontology()
    except Exception as e:
        return f"ERROR: 온톨로지를 읽지 못했습니다 ({ontology_file()}): {e}"
    try:
        res = g.query(query, initNs=PREFIXES)
        if res.type != "SELECT":
            return "ERROR: SELECT 쿼리만 사용하세요."
        cols = [str(v) for v in res.vars]
        lines = []
        for row in res:
            cells = [short(c) for c in row]
            if any(n in c for c in cells for n in NOISE):
                continue
            lines.append("\t".join(cells))
            if len(lines) >= MAX_ROWS:
                break
        if not lines:
            return "(no results)"
        return "\t".join(cols) + "\n" + "\n".join(lines)
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------- Ontop (SPARQL → SQL → PostgreSQL)

ONTOP = "tools/ontop-cli-5.5.0/ontop"
JDBC_DIR = Path("tools/ontop-cli-5.5.0/jdbc")
JAVA_HOME = "tools/jdk-21.0.12.1+1/Contents/Home"
R2RML = DATA_DIR / f"{DB_NAME}.r2rml"
SPARQL_PREFIXES = "".join(f"PREFIX {k}: <{v}>\n" for k, v in PREFIXES.items() if k != "prov")

NATIVE_SQL_MARK = "Resulting native query:"
NATIVE_SQL_END = "@@END_OF_LOG@@"


def ontop_properties() -> str:
    return "\n".join([
        f"jdbc.url=jdbc:postgresql://{PG_HOST}:{PG_PORT}/{DB_NAME}",
        f"jdbc.user={PG_USER}",
        f"jdbc.password={PG_PASSWORD}",
        "jdbc.driver=org.postgresql.Driver",
    ]) + "\n"


def ontop_logback(log_file: Path) -> str:
    # Ontop 이 SPARQL 을 번역한 SQL 은 QuestQueryProcessor 의 DEBUG 로그로만 나온다.
    # 그 로거만 파일로 받고, 나머지는 WARN 이상만 화면(오류 메시지용)으로 보낸다.
    return f"""<configuration>
  <appender name="SQL" class="ch.qos.logback.core.FileAppender">
    <file>{log_file}</file>
    <encoder><pattern>%msg%n{NATIVE_SQL_END}%n</pattern></encoder>
  </appender>
  <appender name="STDERR" class="ch.qos.logback.core.ConsoleAppender">
    <target>System.err</target>
    <encoder><pattern>%-5level %logger{{36}} - %msg%n</pattern></encoder>
  </appender>
  <logger name="it.unibz.inf.ontop.answering.reformulation.impl.QuestQueryProcessor" level="DEBUG" additivity="false">
    <appender-ref ref="SQL" />
  </logger>
  <root level="ERROR"><appender-ref ref="STDERR" /></root>
</configuration>
"""


def native_sql(log_file: Path):
    """Ontop 로그에서 번역된 SQL 을 꺼낸다 (없으면 None)."""
    if not log_file.exists():
        return None
    sqls = []
    for rec in log_file.read_text(encoding="utf-8").split(NATIVE_SQL_END):
        if NATIVE_SQL_MARK not in rec:
            continue
        lines = rec.split(NATIVE_SQL_MARK, 1)[1].strip().splitlines()
        # 앞부분은 Ontop 내부 표현(ans1, CONSTRUCT, NATIVE [...]), 그 뒤가 실제 SQL
        native = [i for i, l in enumerate(lines) if l.strip().startswith("NATIVE [")]
        sqls.append("\n".join(lines[native[0] + 1:] if native else lines).strip())
    return "\n\n".join(s for s in sqls if s) or None


def ontop_error(p) -> str:
    # Java 스택 프레임("at ...")은 빼고 오류 메시지만 남긴다
    text = p.stderr or p.stdout
    msg = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("at ")
           and not l.lstrip().startswith("...")]
    return "ERROR: " + "\n".join(msg)[-1500:]


def run_ontop_sparql(query: str):
    """SPARQL 을 Ontop 으로 실행한다. (에이전트에게 줄 결과, Ontop 이 번역한 SQL) 을 돌려준다."""
    if not any(JDBC_DIR.glob("postgresql-*.jar")):
        return f"ERROR: PostgreSQL JDBC 드라이버(postgresql-*.jar)가 {JDBC_DIR}에 없습니다.", None
    for f in (R2RML, ontology_file()):
        if not Path(f).exists():
            return f"ERROR: 파일이 없습니다: {f}", None
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        q, out, props = tmp / "q.rq", tmp / "out.csv", tmp / "db.properties"
        logback, log_file = tmp / "logback.xml", tmp / "native_sql.log"
        q.write_text(SPARQL_PREFIXES + query, encoding="utf-8")   # 접두어를 미리 붙여 준다
        props.write_text(ontop_properties(), encoding="utf-8")
        logback.write_text(ontop_logback(log_file), encoding="utf-8")
        env = {**os.environ, "JAVA_HOME": str(Path(JAVA_HOME).resolve()), "ONTOP_LOG_CONFIG": str(logback)}
        env.pop("ONTOP_LOG_LEVEL", None)  # 설정 파일의 로그 수준을 덮어쓰지 않게
        try:
            p = subprocess.run(
                [ONTOP, "query", "-m", str(R2RML), "-t", str(ontology_file()), "-p", str(props),
                 "-q", str(q), "-o", str(out)],
                capture_output=True, text=True, env=env, timeout=TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: Ontop 실행이 {TIMEOUT_SEC}초를 넘었습니다.", None
        sql = native_sql(log_file)
        if p.returncode != 0 or not out.exists():
            return ontop_error(p), sql
        lines = out.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[: MAX_ROWS + 1]), sql


def ontop_summary() -> str:
    """Ontop 에이전트에게 줄 온톨로지 요약: 매핑(R2RML)에 실제로 있는 클래스와 속성만, 도메인/범위와 함께."""
    RR = "http://www.w3.org/ns/r2rml#"
    m = Graph()
    m.parse(R2RML, format="turtle")
    g = load_ontology()
    nm = lambda x: str(x).split("#")[-1]
    classes = sorted({nm(o) for o in m.objects(None, URIRef(RR + "class"))})
    preds = {o for o in m.objects(None, URIRef(RR + "predicate"))}
    dps, ops = [], []
    for p in sorted(preds, key=nm):
        dom = ", ".join(nm(o) for o in g.objects(p, RDFS.domain)) or "?"
        rng = [nm(o) for o in g.objects(p, RDFS.range)]
        if (p, RDF.type, OWL.ObjectProperty) in g:
            ops.append(f"  {nm(p)}: {dom} -> {', '.join(rng) or '?'}")
        else:
            dps.append(f"  {nm(p)}: {dom} -> xsd:{', '.join(rng) or '?'}")
    return ("Knowledge graph vocabulary (prefix ont: <" + ONT_NS + ">; use ont:Name):\n"
            "Classes: " + ", ".join(classes) + "\n"
            "Data properties (property: class -> datatype):\n" + "\n".join(dps) + "\n"
            "Object properties (property: from class -> to class):\n" + "\n".join(ops))
