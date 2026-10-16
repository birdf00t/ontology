<!-- TREE_START -->
```text
.
├── .github
│   └── workflows
│       └── update-tree.yml
├── .gitignore
├── README.md
├── agent.py
├── data
│   ├── .gitkeep
│   └── ACME_Insurance
│       ├── DDL
│       │   └── ACME_small.ddl
│       ├── data
│       │   ├── Agreement.csv
│       │   ├── Agreement_Assessment.csv
│       │   ├── Agreement_Party_Role.csv
│       │   ├── Assesment_Party_Role.csv
│       │   ├── Assessment.csv
│       │   ├── Assessment_Result.csv
│       │   ├── Catastrophe.csv
│       │   ├── Claim.csv
│       │   ├── Claim_Amount.csv
│       │   ├── Claim_Coverage.csv
│       │   ├── Expense_Payment.csv
│       │   ├── Expense_Reserve.csv
│       │   ├── FireClaim.csv
│       │   ├── Geographic_Location.csv
│       │   ├── Insurable_Object.csv
│       │   ├── Location_Address.csv
│       │   ├── Loss_Payment.csv
│       │   ├── Loss_Reserve.csv
│       │   ├── Occurrence.csv
│       │   ├── Organization.csv
│       │   ├── Party.csv
│       │   ├── Party_Role.csv
│       │   ├── Person.csv
│       │   ├── Physical_Location.csv
│       │   ├── Policy.csv
│       │   ├── Policy_Amount.csv
│       │   ├── Policy_Coverage_Detail.csv
│       │   ├── Premium.csv
│       │   ├── Underwriting_Assessment.csv
│       │   └── data.world_P&C_Insurance_Ontology_V1.r2rml
│       ├── investigation
│       │   └── acme-benchmark.ttl
│       └── ontology
│           └── insurance.ttl
├── db.py
├── llm.py
└── main.py
```
<!-- TREE_END -->

## 실험 대상
1. SQL 에이전트(클로드) + DB
2. SPARQL 에이전트(클로드) + Ontop(OWL+R2RML)+ DB

## OWL
RIGOR 사용
paper : https://arxiv.org/abs/2506.01232  
code : https://github.com/NadeenAhmad/RIGOR

## DB
BIRD 사용  
paper : https://arxiv.org/pdf/2305.03111  
code : https://bird-bench.github.io/


