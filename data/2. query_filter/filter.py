"""질문 준비 전체: ① 질문 거르기 → ② 정답 SQL 을 PostgreSQL 용으로 변환 → ③ 난이도 라벨.

먼저 1. sqlite to pg 로 DB 를 PostgreSQL 에 옮겨 둬야 한다 (②가 그 DB 에서 정답 SQL 을 실행해 본다).

  python "data/2. query_filter/filter.py"                      # ①②③ 전부
  python "data/2. query_filter/filter.py" --user someone       # PostgreSQL 사용자 (기본: 지금 로그인한 사용자)
  python "data/2. query_filter/filter.py" --no-gold            # ② 건너뜀 (DB 가 아직 없을 때)

결과: data/questions.json, data/gold_pg_<DB>.json, data/difficulty.json
"""
import argparse
import getpass
import json
from pathlib import Path

import sys

HERE = Path(__file__).resolve().parent  # 어디서 실행해도 이 파일 위치 기준
DATA = HERE.parent
sys.path.insert(0, str(HERE / "steps"))   # 하위 폴더: gold_to_pg.py, question_difficulty.py
import gold_to_pg  # noqa: E402
import question_difficulty  # noqa: E402

KEEP = ["movie_3", "synthea", "talkingdata"]
SRC = HERE / "train.json"
DST = DATA / "questions.json"


def filter_questions():
    with open(SRC, encoding="utf-8") as f:
        questions = json.load(f)
    kept = [q for q in questions if q["db_id"] in KEEP]
    with open(DST, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=4)
    with open(HERE / "filter.log", "w", encoding="utf-8") as f:
        f.write(f"{len(questions)}개 중 {len(kept)}개 질문 저장 → {DST}\n")
        for db in sorted(KEEP):
            f.write(f"  {db}: 질문 {sum(q['db_id'] == db for q in kept)}개\n")
    print(f"[1/3] 질문 거르기: {len(questions)}개 중 {len(kept)}개 → {DST}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=getpass.getuser(), help="PostgreSQL 사용자")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", default="5432")
    ap.add_argument("--no-gold", action="store_true", help="정답 SQL 변환을 건너뛴다")
    ap.add_argument("--dbs", nargs="+", choices=KEEP, default=KEEP,
                    help="정답 SQL 을 변환할 DB (기본: 전부). 아직 PostgreSQL 에 옮기지 않은 DB 가 있으면 옮긴 것만 고른다")
    a = ap.parse_args()

    filter_questions()
    if not a.no_gold:
        for db in a.dbs:
            print(f"\n[2/3] 정답 SQL 변환: {db}")
            gold_to_pg.gold(db, gold_to_pg.BIRD_DIR / db / f"{db}.sqlite",
                            f"postgresql://{a.user}@{a.host}:{a.port}/{db}", DST, DATA / f"gold_pg_{db}.json")
    print("\n[3/3] 난이도 라벨")
    question_difficulty.main()


if __name__ == "__main__":
    main()
