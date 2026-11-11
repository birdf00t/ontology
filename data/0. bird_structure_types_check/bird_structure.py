"""BIRD train 의 실제 SQLite 에 선언된 FK 로 스키마 그래프의 V, C, E, 순환 계수와 파일 크기를 구해 CSV 로 저장한다.

실행 (어느 폴더에서 실행해도 된다):
    python -I "data/0. bird_structure_types_check/bird_structure.py"
        → 이 파일이 있는 폴더의 train_databases 를 읽고, 결과를 이 폴더의 bird_structure_actual.csv 에 저장한다
    python -I "data/0. bird_structure_types_check/bird_structure.py" "<train.zip 또는 DB 폴더>" --out "<저장할 CSV>"

- zip 이면 압축을 통째로 풀지 않고 *.sqlite 를 하나씩 임시 폴더에 꺼내 읽은 뒤 바로 지운다 (안에 zip 이 또 있어도 따라 들어간다).
- 폴더이면 아래의 *.sqlite 를 모두 읽는다. 파일은 읽기 전용으로만 연다.
- FK 는 PRAGMA foreign_key_list 로 읽으므로 'references 테이블'처럼 참조 컬럼을 생략한 FK 도 포함된다.
- 컬럼이 둘 이상인 복합 FK 는 간선으로 세지 않는다 (RIGOR 의 스키마 형식이 컬럼 하나짜리 FK 만 표현하기 때문).

그래프: 테이블 = 정점, 컬럼 하나짜리 FK = 간선. 같은 두 테이블 사이의 FK 는 하나로 세고, 자기참조 FK 와 복합 FK 는 세지 않는다 (Diestel 의 단순 그래프).

열
  db_id    DB 이름
  V        정점 수 = 테이블 수
  C        연결 요소 수 (FK 를 따라가면 서로 닿는 테이블 묶음의 수; Diestel 1.4절)
  E        간선 수 (서로 다른 두 테이블 사이의 FK 쌍)
  mu       순환 계수 = E - V + C
           (Diestel 1.9절과 Bianchi et al., Discrete Applied Math. 184, 2015 는 연결 그래프에서 m - n + 1 로 정의한다.
            연결 요소가 여럿이면 요소별 값을 더하면 E - V + C 가 된다.)
  pk_comp  복합 PK 테이블 수 (PK 가 컬럼 둘 이상으로 이루어진 테이블; PRAGMA table_info 의 pk 값이 0 이 아닌 컬럼이 둘 이상)
  uq_comp  복합 UNIQUE 수 (컬럼 둘 이상으로 이루어진 UNIQUE 제약이나 UNIQUE 인덱스; PK 를 위해 자동으로 생긴 인덱스는 뺀다).
           FK 는 PK 나 UNIQUE 를 가리킬 수 있으므로 복합 UNIQUE 도 구조에 영향을 준다.
  fk_comp  복합 FK 수 (컬럼 둘 이상으로 이루어진 FK; PRAGMA foreign_key_list 에서 같은 id 로 여러 줄). 간선으로는 세지 않는다.
  size_mb  sqlite 파일 크기 (MB)
"""
import argparse
import csv
import shutil
import sqlite3
import tempfile
import zipfile
from collections import defaultdict, deque
from pathlib import Path


def read_graph(path):
    """(테이블 목록, FK 간선들, 복합 PK 테이블 수, 복합 FK 수, 복합 UNIQUE 수). 간선은 서로 다른 두 테이블 쌍의 집합이다.

    RIGOR 의 스키마 형식은 컬럼 하나로 이루어진 FK 만 표현할 수 있으므로, 컬럼이 둘 이상인 복합 FK 는 간선으로 세지 않는다."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        names = set(tables)
        edges = set()
        pk_comp = fk_comp = uq_comp = 0
        for t in tables:
            t_q = t.replace('"', '""')
            if sum(1 for r in con.execute(f'PRAGMA table_info("{t_q}")') if r[5] > 0) > 1:   # r[5] = pk 순번 (0 이면 PK 아님)
                pk_comp += 1
            for ix in con.execute(f'PRAGMA index_list("{t_q}")'):          # (seq, name, unique, origin, partial)
                if ix[2] == 1 and ix[3] in ("u", "c"):                     # u = UNIQUE 제약, c = CREATE UNIQUE INDEX (pk 로 생긴 인덱스는 제외)
                    ix_q = ix[1].replace('"', '""')
                    if len(list(con.execute(f'PRAGMA index_info("{ix_q}")'))) > 1:
                        uq_comp += 1
            groups = defaultdict(list)                                     # FK 번호 → 그 FK 의 컬럼 행들
            for r in con.execute(f'PRAGMA foreign_key_list("{t_q}")'):
                groups[r[0]].append(r)
            for rows in groups.values():
                parent = rows[0][2]
                if len(rows) > 1:
                    fk_comp += 1                                           # 복합 FK: 세기만 하고 간선으로는 쓰지 않는다
                elif parent != t and parent in names:                      # 컬럼 하나짜리 FK 만
                    edges.add(frozenset((t, parent)))
    finally:
        con.close()
    return tables, edges, pk_comp, fk_comp, uq_comp


def measure(tables, edges):
    """(V, C, E, mu)"""
    V, E = len(tables), len(edges)
    adj = defaultdict(set)
    for e in edges:
        a, b = tuple(e)
        adj[a].add(b)
        adj[b].add(a)
    seen, C = set(), 0
    for t in tables:
        if t in seen:
            continue
        C += 1
        q = deque([t])
        seen.add(t)
        while q:
            x = q.popleft()
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    q.append(y)
    return V, C, E, E - V + C


def iter_databases(source: Path):
    """(db 이름, 읽을 수 있는 sqlite 경로) 를 하나씩 돌려준다. zip 안에 zip 이 들어 있어도 따라 들어간다 (BIRD train.zip 이 그런 구조)."""
    if source.is_file() and source.suffix.lower() == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="bird_sqlite_"))              # 우리가 만든 빈 임시 폴더
        try:
            with zipfile.ZipFile(source) as z:
                files = [i for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename]
                members = sorted((i for i in files if i.filename.lower().endswith(".sqlite")), key=lambda i: i.filename)
                inner = [i for i in files if i.filename.lower().endswith(".zip")]
                if not members and inner:                                  # 안쪽 zip 을 임시 파일로 꺼내 다시 읽는다
                    for info in inner:
                        out = tmp / "inner.zip"
                        with z.open(info) as src, open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                        try:
                            yield from iter_databases(out)
                        finally:
                            out.unlink(missing_ok=True)
                    return
                for info in members:
                    out = tmp / "current.sqlite"                          # zip 안의 경로는 쓰지 않는다 (경로 조작 방지)
                    with z.open(info) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    try:
                        yield Path(info.filename).stem, out
                    finally:
                        out.unlink(missing_ok=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        for f in sorted(source.rglob("*.sqlite")):
            yield f.stem, f


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = Path(__file__).resolve().parent                                # 이 파일이 있는 폴더
    ap.add_argument("source", type=Path, nargs="?", default=here / "train_databases",
                    help="train.zip 또는 *.sqlite 가 들어 있는 폴더 (기본: 이 폴더의 train_databases)")
    ap.add_argument("--out", type=Path, default=here / "bird_structure_actual.csv",
                    help="결과 CSV (기본: 이 폴더의 bird_structure_actual.csv)")
    a = ap.parse_args()
    if not a.source.exists():
        raise SystemExit(f"{a.source} 가 없습니다.")

    rows, bad = [], []
    for db, path in iter_databases(a.source):
        try:
            tables, edges, pk_comp, fk_comp, uq_comp = read_graph(path)
            V, C, E, mu = measure(tables, edges)
            size_mb = round(Path(path).stat().st_size / 1e6, 1)
        except Exception as e:
            bad.append((db, str(e)[:80]))
            continue
        rows.append({"db_id": db, "V": V, "C": C, "E": E, "mu": mu, "pk_comp": pk_comp, "fk_comp": fk_comp, "uq_comp": uq_comp, "size_mb": size_mb})
        print(f"  {db:30} V={V:<3} C={C:<3} E={E:<3} mu={mu:<3} 복합PK={pk_comp:<2} 복합FK={fk_comp:<2} 복합UQ={uq_comp:<2} {size_mb} MB", flush=True)
    if not rows:
        raise SystemExit("읽은 DB 가 없습니다.")
    rows.sort(key=lambda r: r["db_id"])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8-sig", newline="") as fh:      # utf-8-sig: 엑셀에서 한글이 깨지지 않게
        w = csv.DictWriter(fh, fieldnames=["db_id", "V", "C", "E", "mu", "pk_comp", "fk_comp", "uq_comp", "size_mb"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nDB {len(rows)}개 → {a.out}")
    if bad:
        print("읽지 못한 DB:", bad)


if __name__ == "__main__":
    main()
