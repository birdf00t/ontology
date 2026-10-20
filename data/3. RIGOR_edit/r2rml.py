"""
RIGOR ontology + schema JSON -> W3C R2RML mapping (Turtle), and execution / validation of that mapping on PostgreSQL.
RIGOR itself has no R2RML export; this is new code. Works for both databases: --db movie_3 | synthea.

    python r2rml.py generate --db movie_3 ONTOLOGY_OWL OUT_TTL [--schema SCHEMA_JSON]
    python r2rml.py validate --db movie_3 ONTOLOGY_OWL MAPPING_TTL OUT_NT [--schema SCHEMA_JSON] [--db-url URL]
    python r2rml.py all      --db synthea ONTOLOGY_OWL OUT_TTL OUT_NT      (generate, then validate that mapping)

--schema defaults to config/<db>/schema_<db>.json. --db-url defaults to the local server
(postgresql+psycopg2://@/<db>?host=/tmp&port=5432) and is used both by morph-kgc and for the row counts.

generate - Rules
  1. One rr:TriplesMap per table: rr:tableName, subject IRI template <BASE><table>/{pk1}/{pk2}..., rr:class = the
     table's class. Class / property names come from app.py's own naming functions (to_class_name,
     data_property_name, object_property_name_for_fk), i.e. the Direct Mapping IRIs that the current app.py merges
     into the core ontology before any LLM step - so they exist in the ontology (checked; missing ones are reported).
  2. Non-FK column -> rr:predicate <DM data property>, rr:column, rr:datatype = the ontology's rdfs:range
     (taken from the ontology as published, even when it does not fit the stored values - see the validate step below).
  3. FK column -> rr:predicate <DM object property>, object = IRI template of the referenced table's row
     (every FK in this schema references the parent's primary key; a NULL FK produces no triple).
  5. Text column whose ontology range is xsd:dateTime (set by db_to_schema.py value typing): mapped by its OWN TriplesMap
     whose rr:sqlQuery selects only the key columns and replace(col, ' ', 'T') AS col__iso WHERE col IS NOT NULL,
     so "2005-05-24 22:53:30.0" becomes a valid xsd:dateTime lexical form; the subject template is the table's,
     so the triples attach to the same instances. The table's main TriplesMap keeps rr:tableName: morph-kgc turns
     NULLs into "None" and NULL-able integers into "9450.0" for rr:sqlQuery sources, so the query is kept minimal.
     The database is not modified. With the unmodified RIGOR ontology no column qualifies (identical to rule 2).
  4. Properties added by the LLM whose prov:wasDerivedFrom points to a column <BASE>/provenance/<table>/<column>:
       DatatypeProperty                         -> extra predicate on the same column (alias)
       ObjectProperty, same direction as the FK  -> extra predicate on the same FK (alias)
       ObjectProperty, reversed (domain = parent) -> separate TriplesMap: subject = parent row, object = child row
     anything else is listed as "not mapped".

validate - executes the mapping with morph-kgc and checks the result
Checks
  rows      instances per class == rows per table (composite keys included)
  vocab     every predicate / class used is declared in the ontology
  links     every object IRI produced by an FK points to an existing subject (referential integrity in RDF)
  datatype  literals that are ill-typed for their rr:datatype (rdflib Literal.ill_typed), per predicate + example
  domain    subjects of a DM property are instances of the property's rdfs:domain
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote  # [CHANGED] decode percent-encoded column names in provenance IRIs

from rdflib import Graph, Literal, Namespace, OWL, RDF, RDFS, URIRef, XSD

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import app  # noqa: E402  (naming functions only, unmodified)

DBS = ("movie_3", "synthea")
PROV = Namespace("http://www.w3.org/ns/prov#")
ONT = app.ONTOLOGY_IRI + "#"
RES = "http://example.org/{db}/resource/"
MAP = "http://example.org/{db}/mapping#"


def default_schema(db: str) -> Path:
    return HERE / "config" / db / f"schema_{db}.json"


def default_db_url(db: str) -> str:
    return f"postgresql+psycopg2://@/{db}?host=/tmp&port=5432"


# ---------------------------------------------------------------------------------------------------- generate

def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# [CHANGED for Ontop] PostgreSQL identifiers with capitals / spaces (synthea: "PATIENT") must be double-quoted inside rr:column and {templates};
# tables without a primary key get a row id = md5 of all values (Ontop does not know ctid), served through an rr:sqlQuery.
def q(name: str) -> str:
    return name if name.islower() and name.replace("_", "").isalnum() else f'"{name}"'


def rowid_expr(cols) -> str:
    h = "md5(concat_ws('|', " + ", ".join(f"COALESCE(\"{c}\"::text, '')" for c in cols) + "))"
    return f"{h} || '_' || (row_number() OVER (PARTITION BY {h}))::text"   # + counter: identical rows must stay separate individuals


def build(schema_path: Path, onto_path: Path, db: str) -> tuple[str, dict]:
    res, mapns = RES.format(db=db), MAP.format(db=db)
    sch = app.load_schema_from_json(schema_path)
    raw = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    g = Graph()
    g.parse(str(onto_path), format="xml")
    props = {str(p) for p in g.subjects(RDF.type, OWL.DatatypeProperty)} | {str(p) for p in g.subjects(RDF.type, OWL.ObjectProperty)}
    classes = {str(c) for c in g.subjects(RDF.type, OWL.Class)}
    rng = lambda p: next((str(o) for o in g.objects(URIRef(p), RDFS.range)), None)
    pk = {t: [c.name for c in tb.columns.values() if c.primary_key] for t, tb in sch.items()}
    keyless = {t for t in sch if not pk[t]}                       # [CHANGED for Ontop] no PK -> row id column "_rowid"
    keys = lambda t: pk[t] or ["_rowid"]
    tmpl = lambda t, cols=None: res + t + "/" + "/".join("{%s}" % q(c) for c in (cols or keys(t)))

    def logical(t: str) -> str:
        if t not in keyless:
            return f'[ rr:tableName "{esc(t)}" ]'
        return f'[ rr:sqlQuery "SELECT *, {esc(rowid_expr(sch[t].columns))} AS \\"_rowid\\" FROM \\"{esc(t)}\\"" ]'

    report = {"triples_maps": 0, "dm_predicates": 0, "missing_in_ontology": [], "llm_alias": [], "llm_inverse": [],
              "llm_not_mapped": [], "datatypes": {}, "iso_datetime_columns": []}
    out = ["@prefix rr: <http://www.w3.org/ns/r2rml#> .", "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
           f"@prefix : <{ONT}> .", f"@prefix map: <{mapns}> .", ""]

    # provenance index: column -> LLM-added properties derived from it
    prov_cols: dict = {}
    for p in props:
        for o in g.objects(URIRef(p), PROV.wasDerivedFrom):
            m = re.search(r"/provenance/([^/#]+)/([^/#]+)$", str(o))
            # [CHANGED] app.py now percent-encodes the column in provenance IRIs (space -> %20): decode it back
            col_name = unquote(m.group(2)) if m else None
            if m and m.group(1) in sch and col_name in sch[m.group(1)].columns:
                prov_cols.setdefault((m.group(1), col_name), set()).add(p)

    for t, tb in sch.items():
        cls = str(app.uri(app.to_class_name(t)))
        if cls not in classes:
            report["missing_in_ontology"].append(cls)
        fks = {fk.column: fk for fk in tb.foreign_keys}
        poms, extra_maps, iso = [], [], []
        for col in tb.columns.values():
            c = col.name
            if c in fks:
                fk = fks[c]
                dm = str(app.uri(app.object_property_name_for_fk(fk, t)))
                obj = f'[ rr:template "{esc(tmpl(fk.references_table, [c]))}" ; rr:termType rr:IRI ]'
            else:
                dm = str(app.uri(app.data_property_name(t, c)))
                dt = rng(dm) or str(col.xsd_type)
                report["datatypes"][f"{t}.{c}"] = dt.split("#")[-1]
                if dt == str(XSD.dateTime) and raw[t]["columns"][c]["raw_type"] == "TEXT":
                    iso.append((c, dm, dt))
                    report["iso_datetime_columns"].append(f"{t}.{c}")
                    if dm not in props:
                        report["missing_in_ontology"].append(dm)
                    report["dm_predicates"] += 1
                    continue
                obj = f'[ rr:column "{esc(q(c))}" ; rr:datatype <{dt}> ]'
            if dm not in props:
                report["missing_in_ontology"].append(dm)
            poms.append(f"    rr:predicateObjectMap [ rr:predicate <{dm}> ; rr:objectMap {obj} ]")
            report["dm_predicates"] += 1
            for p in sorted(prov_cols.get((t, c), set()) - {dm}):
                kind_op = (URIRef(p), RDF.type, OWL.ObjectProperty) in g
                dom = {str(x) for x in g.objects(URIRef(p), RDFS.domain)}
                rgs = {str(x) for x in g.objects(URIRef(p), RDFS.range)}
                if not kind_op and c not in fks:
                    dt = rng(p) or str(col.xsd_type)
                    poms.append(f'    rr:predicateObjectMap [ rr:predicate <{p}> ; rr:objectMap [ rr:column "{esc(q(c))}" ; rr:datatype <{dt}> ] ]')
                    report["llm_alias"].append(f"{t}.{c} -> {p.split('#')[-1]}")
                elif kind_op and c in fks:
                    parent_cls = str(app.uri(app.to_class_name(fks[c].references_table)))
                    if dom <= {cls} and rgs <= {parent_cls}:
                        poms.append(f"    rr:predicateObjectMap [ rr:predicate <{p}> ; rr:objectMap {obj} ]")
                        report["llm_alias"].append(f"{t}.{c} -> {p.split('#')[-1]}")
                    elif dom <= {parent_cls} and rgs <= {cls}:
                        name = f"map:TM_{t}__inv__{p.split('#')[-1]}"
                        extra_maps.append(
                            f"{name} a rr:TriplesMap ;\n"
                            f"    rr:logicalTable {logical(t)} ;\n"
                            f"    rr:subjectMap [ rr:template \"{esc(tmpl(fks[c].references_table, [c]))}\" ] ;\n"
                            f"    rr:predicateObjectMap [ rr:predicate <{p}> ; rr:objectMap [ rr:template \"{esc(tmpl(t))}\" ; rr:termType rr:IRI ] ] .\n")
                        report["llm_inverse"].append(f"{t}.{c} -> {p.split('#')[-1]} (parent -> child)")
                    else:
                        report["llm_not_mapped"].append(f"{t}.{c} -> {p.split('#')[-1]} (domain/range do not fit the FK)")
                else:
                    report["llm_not_mapped"].append(f"{t}.{c} -> {p.split('#')[-1]} ({'ObjectProperty on non-FK' if kind_op else 'DatatypeProperty on FK'})")
        for c, dm, dt in iso:
            sel = ", ".join(f'\\"{k}\\"' for k in pk[t]) if t not in keyless else f'{esc(rowid_expr(sch[t].columns))} AS \\"_rowid\\"'
            sql = f'SELECT {sel}, replace(\\"{c}\\", \' \', \'T\') AS \\"{c}__iso\\" FROM \\"{t}\\" WHERE \\"{c}\\" IS NOT NULL'
            extra_maps.append(
                f"map:TM_{t}__datetime__{c} a rr:TriplesMap ;\n"
                f"    rr:logicalTable [ rr:sqlQuery \"{sql}\" ] ;\n"
                f"    rr:subjectMap [ rr:template \"{esc(tmpl(t))}\" ] ;\n"
                f"    rr:predicateObjectMap [ rr:predicate <{dm}> ; rr:objectMap [ rr:column \"{c}__iso\" ; rr:datatype <{dt}> ] ] .\n")
        logical_t = logical(t)
        out.append(f"map:TM_{t} a rr:TriplesMap ;\n    rr:logicalTable {logical_t} ;\n"
                   f"    rr:subjectMap [ rr:template \"{esc(tmpl(t))}\" ; rr:class <{cls}> ] ;\n" + " ;\n".join(poms) + " .\n")
        out += extra_maps
        report["triples_maps"] += 1 + len(extra_maps)

    # [CHANGED for Ontop] LLM object property whose provenance is a whole table (e.g. film -> category via film_category):
    # the table is a link table with one FK to the domain's table and one to the range's table -> one TriplesMap from that table.
    cls_table = {str(app.uri(app.to_class_name(t))): t for t in sch}
    for p in sorted(p for p in props if (URIRef(p), RDF.type, OWL.ObjectProperty) in g):
        pv = [m.group(1) for o in g.objects(URIRef(p), PROV.wasDerivedFrom)
              for m in [re.search(r"/provenance/([^/#]+)$", str(o))] if m]
        dom = next((cls_table.get(str(x)) for x in g.objects(URIRef(p), RDFS.domain)), None)
        rgs = next((cls_table.get(str(x)) for x in g.objects(URIRef(p), RDFS.range)), None)
        if len(pv) != 1 or pv[0] not in sch or not dom or not rgs:
            continue
        t = pv[0]
        to_dom = [fk.column for fk in sch[t].foreign_keys if fk.references_table == dom]
        to_rng = [fk.column for fk in sch[t].foreign_keys if fk.references_table == rgs]
        name = p.split("#")[-1]
        if dom != rgs and len(to_dom) == 1 and len(to_rng) == 1:
            out.append(f"map:TM_{t}__link__{name} a rr:TriplesMap ;\n"
                       f"    rr:logicalTable {logical(t)} ;\n"
                       f"    rr:subjectMap [ rr:template \"{esc(tmpl(dom, [to_dom[0]]))}\" ] ;\n"
                       f"    rr:predicateObjectMap [ rr:predicate <{p}> ; rr:objectMap [ rr:template \"{esc(tmpl(rgs, [to_rng[0]]))}\" ; rr:termType rr:IRI ] ] .\n")
            report["llm_link_table"] = report.get("llm_link_table", []) + [f"{t} -> {name}"]
            report["triples_maps"] += 1
        else:
            report["llm_not_mapped"].append(f"{t} -> {name} (link table: cannot pick one FK per side)")
    return "\n".join(out), report


def generate(db: str, onto_path: Path, out_p: Path, schema_path: Path) -> None:
    ttl, rep = build(schema_path, onto_path, db)
    Graph().parse(data=ttl, format="turtle")  # syntax check
    out_p.write_text(ttl, encoding="utf-8")
    (out_p.with_suffix(".report.json")).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(f"-> {out_p}: {rep['triples_maps']} TriplesMaps, {rep['dm_predicates']} DM predicate maps, "
          f"{len(rep['llm_alias'])} LLM aliases, {len(rep['llm_inverse'])} LLM inverse maps, {len(rep.get('llm_link_table', []))} LLM link-table maps, "
          f"{len(rep['llm_not_mapped'])} LLM properties not mapped, {len(rep['missing_in_ontology'])} DM IRIs missing in ontology")


# ---------------------------------------------------------------------------------------------------- validate

def validate(onto_path: Path, mapping: Path, out_nt: Path, schema_path: Path, db_url: str) -> None:
    import morph_kgc  # imported here so that `generate` works without morph-kgc / SQLAlchemy (morph-kgc needs Python >= 3.10)
    import sqlalchemy
    cfg = f"[CONFIGURATION]\nnumber_of_processes=1\n\n[DataSource1]\nmappings={mapping}\ndb_url={db_url}\n"
    data = morph_kgc.materialize(cfg)
    data.serialize(out_nt, format="nt")

    onto = Graph(); onto.parse(str(onto_path), format="xml")
    o_props = set(onto.subjects(RDF.type, OWL.DatatypeProperty)) | set(onto.subjects(RDF.type, OWL.ObjectProperty))
    o_cls = set(onto.subjects(RDF.type, OWL.Class))
    schema = json.loads(Path(schema_path).read_text())

    engine = sqlalchemy.create_engine(db_url)
    with engine.connect() as conn:
        rows = {t: conn.execute(sqlalchemy.text(f'select count(*) from "{t}"')).scalar() for t in schema}
    engine.dispose()

    inst = Counter(o for o in data.objects(None, RDF.type))
    subjects = set(data.subjects(RDF.type, None))
    rep = {"triples": len(data), "rows_vs_instances": {}, "unknown_predicates": [], "unknown_classes": [],
           "dangling_links": {}, "ill_typed": {}, "domain_violations": {}}
    for t in schema:
        cls = app.uri(app.to_class_name(t))
        rep["rows_vs_instances"][t] = {"rows": rows[t], "instances": inst.get(cls, 0), "ok": rows[t] == inst.get(cls, 0)}
    preds = set(data.predicates()) - {RDF.type}
    rep["unknown_predicates"] = sorted(str(p) for p in preds if p not in o_props)
    rep["unknown_classes"] = sorted(str(c) for c in inst if c not in o_cls)

    dangling, ill, ill_ex, dom_bad = Counter(), Counter(), {}, Counter()
    domain_of = {p: set(onto.objects(p, RDFS.domain)) for p in o_props}
    types = defaultdict(set)
    for s, _, c in data.triples((None, RDF.type, None)):
        types[s].add(c)
    for s, p, o in data:
        if p == RDF.type:
            continue
        if isinstance(o, URIRef) and o not in subjects:
            dangling[str(p)] += 1
        if isinstance(o, Literal) and o.datatype is not None and o.ill_typed:
            ill[str(p)] += 1
            ill_ex.setdefault(str(p), f'"{o}"^^{o.datatype.split("#")[-1]}')
        dom = domain_of.get(p)
        if dom and not (types[s] & dom):
            dom_bad[str(p)] += 1
    rep["dangling_links"] = dict(dangling)
    rep["ill_typed"] = {p.split("#")[-1]: {"count": n, "example": ill_ex[p]} for p, n in ill.most_common()}
    rep["domain_violations"] = {p.split("#")[-1]: n for p, n in dom_bad.most_common()}
    Path(out_nt).with_suffix(".validation.json").write_text(json.dumps(rep, indent=2))

    ok_rows = all(v["ok"] for v in rep["rows_vs_instances"].values())
    print(f"triples {rep['triples']} -> {out_nt}")
    print(f"rows == instances for all tables: {ok_rows}  "
          + ("" if ok_rows else str({t: v for t, v in rep['rows_vs_instances'].items() if not v['ok']})))
    print(f"unknown predicates {len(rep['unknown_predicates'])}, unknown classes {len(rep['unknown_classes'])}")
    print(f"dangling object IRIs {sum(dangling.values())} {dict(dangling) if dangling else ''}")
    print(f"ill-typed literals {sum(ill.values())} in {len(ill)} predicates")
    for p, v in list(rep["ill_typed"].items())[:12]:
        print(f"   {p:28} {v['count']:6}  e.g. {v['example']}")
    print(f"domain violations {sum(dom_bad.values())} {rep['domain_violations'] if dom_bad else ''}")


# ---------------------------------------------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description="RIGOR ontology -> R2RML mapping, and validation of the mapping on PostgreSQL")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--db", choices=DBS, required=True, help="which database / schema to use")
        p.add_argument("--schema", type=Path, help="schema JSON (default: config/<db>/schema_<db>.json)")

    g = sub.add_parser("generate", help="ontology + schema -> R2RML mapping (.ttl)")
    common(g); g.add_argument("ontology", type=Path); g.add_argument("out_ttl", type=Path)
    v = sub.add_parser("validate", help="run a mapping with morph-kgc on PostgreSQL and check the result")
    common(v); v.add_argument("ontology", type=Path); v.add_argument("mapping", type=Path); v.add_argument("out_nt", type=Path)
    v.add_argument("--db-url", help="SQLAlchemy URL (default: local server, database <db>)")
    al = sub.add_parser("all", help="generate, then validate that mapping")
    common(al); al.add_argument("ontology", type=Path); al.add_argument("out_ttl", type=Path); al.add_argument("out_nt", type=Path)
    al.add_argument("--db-url", help="SQLAlchemy URL (default: local server, database <db>)")
    a = ap.parse_args()

    schema = a.schema or default_schema(a.db)
    if a.cmd in ("generate", "all"):
        generate(a.db, a.ontology, a.out_ttl, schema)
    if a.cmd in ("validate", "all"):
        validate(a.ontology, a.mapping if a.cmd == "validate" else a.out_ttl, a.out_nt, schema,
                 a.db_url or default_db_url(a.db))


if __name__ == "__main__":
    main()
