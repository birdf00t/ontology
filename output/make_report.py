"""채점 결과(output/<DB>/evaluation.jsonl)로 논문의 표 1, 표 3, 그림 1 을 만든다 (LLM·DB 는 쓰지 않는다).

실행 (프로젝트 루트에서):
    python output/make_report.py

만드는 파일 (output/report/):
    table1_accuracy.csv          조건별 최종 정확도 — 전체, movie_3, synthea  (정확도 %, Wilson 95% 신뢰구간)
    table3_difficulty.csv        난이도별 최종 정확도 — easy, medium, hard, extra
    figure1_difficulty.png       난이도별 최종 정확도 묶음 막대 (오차 막대 = Wilson 95% 신뢰구간)

대상 질문: 세 조건(sql, sparql, ontop)이 모두 채점됐고 정답이 쓸 수 있는 질문.
  정답 SQL 이 불안정·변환 불가(gold_unstable / gold_error)이거나 결과가 빈(gold_empty) 질문은 뺀다.
최종 정확도: SQL 결과가 정답과 같거나(ex), 답변 글에 정답 값이 모두 있으면(ans) 정답 (evaluate.py 와 같은 기준).

그림에는 matplotlib 이 필요하다:  pip install matplotlib
"""
import csv
import json
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "report"
DBS = ["movie_3", "synthea"]
KINDS = ["sql", "sparql", "ontop"]
LEVELS = ["easy", "medium", "hard", "extra"]
UNUSABLE = {"gold_empty", "gold_unstable", "gold_error"}
MODEL = "claude-haiku-5-5"      # 논문에 쓰는 에이전트 모델 (다른 모델로 돌린 결과 줄은 쓰지 않는다)


def wilson(k, n, z=1.96):
    """정확도 k/n 의 Wilson 95% 신뢰구간 (%)."""
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / d * 100, (c + a) / d * 100


def load():
    """{(kind, id): 최종 정답 여부} 중 세 조건이 모두 있고 정답이 쓸 수 있는 질문만."""
    res, bad = {}, set()
    for db in DBS:
        path = ROOT / "output" / db / "evaluation.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["model"] != MODEL:
                continue
            if r["ex"] in UNUSABLE or r["ans"] in UNUSABLE:
                bad.add(r["id"])
                continue
            res[(r["kind"], r["id"])] = r["ex"] == "correct" or r["ans"] == "correct"
    ids = sorted({i for _, i in res if i not in bad and all((k, i) in res for k in KINDS)})
    return res, ids


def cell(res, kind, ids):
    k = sum(res[(kind, i)] for i in ids)
    lo, hi = wilson(k, len(ids))
    return k, len(ids), 100 * k / len(ids), lo, hi


def write_table(name, header, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{name}.csv", "w", encoding="utf-8-sig", newline="") as f:   # utf-8-sig: 엑셀에서 한글이 깨지지 않게
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def fmt(c):
    return f"{c[2]:.1f} ({c[3]:.1f}–{c[4]:.1f})"


def main():
    res, ids = load()
    diff = json.loads((ROOT / "data" / "difficulty.json").read_text(encoding="utf-8"))
    groups = {"전체": ids, **{db: [i for i in ids if i.startswith(db + "-")] for db in DBS}}
    print("대상 질문:", {g: len(v) for g, v in groups.items()})

    # 표 1: 조건 × (전체, DB별)
    rows = [[k] + [fmt(cell(res, k, v)) for v in groups.values()] for k in KINDS]
    rows.append(["질문 수"] + [len(v) for v in groups.values()])
    write_table("table1_accuracy", ["조건"] + list(groups), rows)

    # 표 3: 조건 × 난이도
    by_level = {lv: [i for i in ids if diff[i]["difficulty"] == lv] for lv in LEVELS}
    rows = [[k] + [fmt(cell(res, k, v)) for v in by_level.values()] for k in KINDS]
    rows.append(["질문 수"] + [len(v) for v in by_level.values()])
    write_table("table3_difficulty", ["조건"] + LEVELS, rows)

    # 그림 1: 난이도별 묶음 막대
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    installed = {f.name for f in font_manager.fontManager.ttflist}
    korean = next((n for n in ("AppleGothic", "Malgun Gothic", "NanumGothic") if n in installed), "sans-serif")   # 설치된 한글 글꼴 (맥, 윈도우, 리눅스)
    matplotlib.rcParams["font.family"] = korean
    matplotlib.rcParams["axes.unicode_minus"] = False
    colors = {"sql": "#9aa5b1", "sparql": "#2f6fb3", "ontop": "#d9822b"}
    w = 0.26
    fig, ax = plt.subplots(figsize=(7.5, 4))
    for j, k in enumerate(KINDS):
        cs = [cell(res, k, by_level[lv]) for lv in LEVELS]
        xs = [i + (j - 1) * w for i in range(len(LEVELS))]
        ys = [c[2] for c in cs]
        err = [[c[2] - c[3] for c in cs], [c[4] - c[2] for c in cs]]
        ax.bar(xs, ys, w, yerr=err, capsize=3, color=colors[k], label=k, error_kw={"linewidth": 1})
    ax.set_xticks(range(len(LEVELS)))
    ax.set_xticklabels([f"{lv}\n(n={len(by_level[lv])})" for lv in LEVELS])
    ax.set_ylabel("최종 정확도 (%)")
    ax.set_ylim(0, 100)
    ax.set_xlabel("질문 난이도")
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.12))
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(OUT / "figure1_difficulty.png", dpi=200)
    print("저장:", OUT)


if __name__ == "__main__":
    main()
