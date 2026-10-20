## DB
BIRD sqlite를 postgresql로 변환하여 사용 (train 데이터셋에서 synthea, movie_3)  
paper : https://arxiv.org/pdf/2305.03111  
code : https://bird-bench.github.io/

# DB -> OWL 변환
RIGOR 사용(claude-opus-5-5)
paper : https://arxiv.org/abs/2506.01232  
code : https://github.com/NadeenAhmad/RIGOR

## 실험 대상
클로드 LLM : claude-sonnet-5-5, claude-haiku-5-5
1. SQL 랭체인 에이전트(클로드) + DB
2. SPARQL 랭체인 에이전트(클로드) + OWL 검색 결과 -> SQL 랭체인 에이전트(클로드) + DB
3. SPARQL 랭체인 에이전트(클로드) + Ontop(OWL,R2RML)+ DB

### 실험 터미널
```
AGENT_KIND=sql PG_DB=movie_3 caffeinate -i python main.py
AGENT_KIND=sparql PG_DB=movie_3 caffeinate python main.py
AGENT_KIND=ontop  PG_DB=movie_3 caffeinate python main.py

AGENT_KIND=sql PG_DB=synthea caffeinate python main.py
AGENT_KIND=sparql PG_DB=synthea caffeinate python main.py
AGENT_KIND=ontop  PG_DB=synthea caffeinate python main.py
```