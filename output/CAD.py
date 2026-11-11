import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

BASE = Path(__file__).resolve().parent          # output 폴더
DBS = ["movie_3", "synthea"]                     # 각 폴더 안의 *.csv를 읽음
CONDS = ["sql", "sparql", "ontop"]
NEED = {"id", "condition", "question", "answer", "final"}
N_ITER, FRAC, SEED = 500, 0.8, 0


def vendi(emb):
    emb = emb / np.linalg.norm(emb, axis=-1, keepdims=True)
    K = np.maximum(cosine_similarity(emb), 0) / len(emb)
    lam = np.linalg.eigvalsh(K)
    lam = lam[lam > 1e-12]
    return float(np.exp(-(lam * np.log(lam)).sum()))


def load_db(db):
    files = sorted((BASE / db).glob("*.csv"))
    if not files:
        sys.exit(f"[오류] {BASE / db} 안에 csv가 없습니다.")
    frames = []
    for f in files:
        df = pd.read_csv(f, encoding="utf-8-sig")
        missing = NEED - set(df.columns)
        if missing:
            print(f"[건너뜀] {f.name}: 필요한 열 없음 {sorted(missing)}")
            continue
        print(f"[읽음] {db}/{f.name}: {len(df)}행")
        frames.append(df)
    if not frames:
        sys.exit(f"[오류] {db}: 필요한 열(id, condition, question, answer, final)이 있는 csv가 없습니다.")
    d = pd.concat(frames, ignore_index=True)
    d["cond"] = d.condition.str.split("_").str[0]
    before = len(d)
    d = d.drop_duplicates(["id", "cond"], keep="first")
    if len(d) < before:
        print(f"[주의] {db}: (id, 조건) 중복 {before - len(d)}행 제거 (첫 행 유지). 여러 run이 섞였는지 확인하세요.")
    return d


model = SentenceTransformer("BAAI/bge-large-en-v1.5")
embed = lambda t: model.encode(t, normalize_embeddings=True, batch_size=32, show_progress_bar=False)

rng = np.random.default_rng(SEED)
rows = []

for db in DBS:
    d = load_db(db)

    # 3조건 모두 정답인 질문만 (final은 여기서만 사용)
    ok = d.pivot_table(index="id", columns="cond", values="final", aggfunc="first")
    if not set(CONDS) <= set(ok.columns):
        sys.exit(f"[오류] {db}: 조건 {CONDS} 중 없는 것이 있습니다: {list(ok.columns)}")
    ids = sorted(ok[(ok[CONDS] == "correct").all(axis=1)].index)
    n = len(ids)
    print(f"\n[{db}] 3조건 모두 정답인 질문 수 N={n}  (전체 id {len(ok)})")
    if n < 10:
        sys.exit(f"[오류] {db}: 질문이 너무 적습니다.")
    d = d[d.id.isin(ids)].set_index(["cond", "id"]).sort_index()

    # 임베딩에는 question, answer만 사용
    E = {}
    for c in CONDS:
        g = d.loc[c].loc[ids]
        q, a = g.question.astype(str).tolist(), g.answer.astype(str).tolist()
        E[c] = dict(
            cad=embed([f"{x} {y}" for x, y in zip(q, a)]),   # 질문 + 답변
            vendi=embed(a),                                  # 답변만
        )

    # 같은 질문 부분집합을 세 방식에 동일하게 사용
    k = int(n * FRAC)
    S = {m: {c: [] for c in CONDS} for m in ("cad", "vendi")}
    for _ in range(N_ITER):
        idx = rng.choice(n, k, replace=False)
        for m in S:
            for c in CONDS:
                S[m][c].append(vendi(E[c][m][idx]))

    for m in S:
        for c in CONDS:
            v = np.array(S[m][c])
            lo, hi = np.percentile(v, [2.5, 97.5])
            rows.append(dict(db=db, metric=m, item=c, N=n,
                             mean=v.mean(), lo=lo, hi=hi, verdict=""))
        for a, b in combinations(CONDS, 2):
            v = np.array(S[m][a]) - np.array(S[m][b])
            lo, hi = np.percentile(v, [2.5, 97.5])
            verdict = "차이 있음" if (lo > 0 or hi < 0) else "구분 어려움"
            rows.append(dict(db=db, metric=m, item=f"{a}-{b}", N=n,
                             mean=v.mean(), lo=lo, hi=hi, verdict=verdict))

res = pd.DataFrame(rows)
for (db, m), g in res.groupby(["db", "metric"], sort=False):
    print(f"\n[{db}] {m.upper()}  (N={g.N.iloc[0]})")
    print(g[["item", "mean", "lo", "hi", "verdict"]].round(3).to_string(index=False))

out = BASE / "cad_results.csv"
res.to_csv(out, index=False, encoding="utf-8-sig")
print(f"\n저장: {out}")