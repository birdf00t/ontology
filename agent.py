from langchain.tools import tool
from langgraph.prebuilt import create_react_agent

from db import ontop_summary, run_ontology_sparql, run_ontop_sparql, run_sql
from llm import get_llm

SQL_PROMPT = (
    "You answer questions about a database by writing SQL (PostgreSQL). "
    "Use the sql_query tool. Only SELECT statements. "
    "Table and column names are case-sensitive: wrap any name that has uppercase letters "
    "or spaces in double quotes. "
    "If a query fails, read the error and fix it. Give a concise final answer."
)

ONTOLOGY_PROMPT = (
    "\n\nYou have an ontology of the database, searchable with the ontology_query tool "
    "(SPARQL SELECT). Work in exactly this order:\n"
    "STEP 1 - Search the ontology. Use ONLY ontology_query. Do NOT write or run any SQL yet. "
    "Find the classes, properties and relationships relevant to the question.\n"
    "STEP 2 - Only after the ontology search has returned the schema you need, write the SQL "
    "and run it with sql_query. The sql_query tool refuses to run until the ontology has been "
    "searched at least once, so never start with SQL.\n"
    "STEP 3 - Read the SQL result (fix the SQL if it failed) and give a concise final answer.\n\n"
    "About the ontology: every class and property has a prov:wasDerivedFrom value, 'table' for a "
    "class and 'table/column' for a property (an object property points to the foreign-key column). "
    "That value is the source table/column to use in SQL. Prefixes ont:, prov:, owl:, rdfs:, rdf:, "
    "xsd: are predefined. Useful queries:\n"
    "- all elements derived from a table: "
    "SELECT ?e ?src WHERE { ?e prov:wasDerivedFrom ?src . FILTER(CONTAINS(STR(?src), \"/film\")) }\n"
    "- relationships between classes (foreign keys): "
    "SELECT ?p ?domain ?range WHERE { ?p a owl:ObjectProperty ; rdfs:domain ?domain ; rdfs:range ?range }\n"
    "- descriptions: SELECT ?e ?comment WHERE { ?e rdfs:comment ?comment } (add a FILTER on ?e).\n"
    "The ontology is only a guide to the schema; the data itself is in the database."
)

ONTOP_PROMPT = (
    "You answer questions about a database by writing SPARQL SELECT queries over a knowledge graph. "
    "The graph is a live view of the relational database (Ontop translates SPARQL to SQL), so a "
    "SPARQL query returns the real data. Use the sparql_query tool and only the classes and "
    "properties in the vocabulary below. Prefixes ont:, rdf:, rdfs:, xsd:, owl: are predefined. "
    "Every row is an individual of a class; read a value with its data property and follow a "
    "relationship with an object property. Only SELECT queries, and return only the variables "
    "the question asks for. If a query fails or returns nothing, read the error, check the "
    "vocabulary and fix the query. Give a concise final answer."
)

PROMPTS = {
    "sql": SQL_PROMPT,
    "sparql": SQL_PROMPT + ONTOLOGY_PROMPT,
    "ontop": ONTOP_PROMPT,
}

# sparql 에이전트에서 "온톨로지 검색 후에만 SQL" 순서를 코드로도 지키게 하는 장치.
# 프롬프트만으로는 모델이 바로 SQL부터 쓸 수 있어서, 검색 전의 sql_query 호출은 거절한다.
# (필요 없으면 아래 _gate 관련 줄을 지우면 프롬프트만으로 동작한다.)
_gate = {"enabled": False, "searched": False}


def reset_gate():
    """질문 하나를 시작할 때마다 호출한다 (이전 질문의 검색 기록이 넘어가지 않게)."""
    _gate["searched"] = False


@tool
def sql_query(query: str) -> str:
    """Run a read-only SQL SELECT query and return the result rows."""
    if _gate["enabled"] and not _gate["searched"]:
        return ("ERROR: Search the ontology first. Call ontology_query (SPARQL) to find the "
                "relevant classes, properties and source tables/columns, then write the SQL.")
    return run_sql(query)


@tool
def ontology_query(query: str) -> str:
    """Search the database ontology with a SPARQL SELECT query. Use it to find classes,
    properties, relationships and their source table/column (prov:wasDerivedFrom)."""
    result = run_ontology_sparql(query)
    if not result.startswith("ERROR") and result != "(no results)":
        _gate["searched"] = True  # 스키마가 실제로 검색됐을 때만 SQL 을 허용
    return result


@tool(response_format="content_and_artifact")
def sparql_query(query: str):
    """Run a SPARQL SELECT query over the knowledge graph (a live view of the database)
    and return the result as CSV."""
    # 에이전트에게는 결과만 준다. Ontop 이 번역한 SQL 은 artifact 로 기록에만 남긴다.
    result, sql = run_ontop_sparql(query)
    return result, {"sql": sql}


def build_agent(kind: str, context: str = ""):
    if kind == "ontop":
        tools = [sparql_query]
        context = (context + "\n\n" if context else "") + ontop_summary()  # 매핑에 있는 클래스·속성 목록
    elif kind == "sql":
        tools = [sql_query]
    elif kind == "sparql":
        # SPARQL 로 온톨로지를 검색해 스키마(테이블/컬럼)를 파악한 뒤, 그걸로 SQL 을 써서 DB 를 검색한다
        tools = [ontology_query, sql_query]
    else:
        raise ValueError("kind must be 'sql', 'sparql' or 'ontop'")
    _gate["enabled"] = kind == "sparql"
    reset_gate()
    prompt = PROMPTS[kind] + ("\n\n" + context if context else "")
    return create_react_agent(get_llm(), tools, prompt=prompt)
