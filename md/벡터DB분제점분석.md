1. org_name이 64% 비어 있음
- org_id 없음: 0(90%)
- org_name 없음: 170,902건(64%)




## 현재 Qdrant의 성격
현재 Qdrant는 sparse 벡터만 사용하고(claim dense, HyDE dense) BM25는 Qdrant 밖으로 뺌
=> 그래서 Qdrant native 하이브리드(prefetch + FusionQuery.RRF)를 쓸 수 없고, BM25를 매 검색 실행마다 파이썬 메모리에서 다시 빌드해야함

### 문제점
- 전체 색인(25만건) 돌리면 터진다 
