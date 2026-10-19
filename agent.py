from langchain.agents import create_agent
from langchain.tools import tool

from db import run_sparql, run_sql
from llm import get_llm

PROMPTS = {
    "sql": (
        "You answer questions about a database by writing SQL (DuckDB). "
        "Use the sql_query tool. Only SELECT statements. "
        "If a query fails, read the error and fix it. Give a concise final answer."
    ),
    "sparql": (
        "You answer questions by writing SPARQL SELECT queries over a knowledge graph. "
        "Use the sparql_query tool and only the classes and properties in the ontology below. "
        "If a query fails, read the error and fix it. Give a concise final answer."
    ),
}


@tool
def sql_query(query: str) -> str:
    """Run a read-only SQL SELECT query and return the result rows."""
    return run_sql(query)

@tool
def sparql_query(query: str) -> str:
    """Run a SPARQL SELECT query and return the result as CSV."""
    return run_sparql(query)


def build_agent(kind: str, context: str = ""):
    if kind == "sql":
        tools = [sql_query]
    elif kind == "sparql":
        tools = [sparql_query]
    else:
        raise ValueError("kind must be 'sql' or 'sparql'")
    prompt = PROMPTS[kind] + ("\n\n" + context if context else "")
    return create_agent(get_llm(), tools, system_prompt=prompt)