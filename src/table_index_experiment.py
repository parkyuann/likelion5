"""
"표 단위 색인 vs 분류값까지 포함한 색인" 중 어느 쪽이 나은지 확인하는 실험.

실제 임베딩 모델(HCX 계열)의 API 키가 이 환경에 없어서, 대신 문자 단위
TF-IDF(analyzer='char', 2~4gram — 한국어 형태소 분석기 없이도 어느 정도
어휘 유사도를 잡아내는 방식)로 "인덱싱 단위" 자체의 효과만 먼저 검증한다.
실제 서비스에 쓸 건 아니고, 벡터DB 설계(표 단위 vs 분류값 단위) 결정을
위한 프로토타입.

시나리오: "비정규직" 키워드로 후보가 10개 이상 나온다고 kosis_table_summary.md에
적어뒀던 문제를 실제 데이터(kosis_table_tree.json의 D 노동 대분류)로 재현해서,
표 이름만 넣은 색인과 분류값 이름까지 넣은 색인 중 어느 쪽이 정답 표를 더 잘
1위로 올리는지 비교한다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/table_index_experiment.py
"""
import json
import sys
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kosis_client import get_meta  # noqa: E402

TREE_PATH = Path(__file__).resolve().parent.parent / "data" / "kosis_table_tree.json"

# 실제 검증에 쓴 claim(idx=2646)과 정답 표
QUERY = "비정규직 근로자는 856만8000명으로 전년 동월 대비 11만 명 증가했다"
CORRECT_TBL_ID = "DT_1DE7110_11"


def load_candidate_tables() -> list[dict]:
    """D(노동) 대분류에서 "비정규직" 키워드가 들어간 현재 유효 표(orgId=101)만 추린다."""
    tree = json.loads(TREE_PATH.read_text(encoding="utf-8"))
    leaves = tree["D"]["leaves"]
    cands = [
        leaf for leaf in leaves
        if "비정규직" in (leaf["tbl_nm"] or "") and leaf["org_id"] == "101"
    ]
    # 구버전 중복 제거 대신 그대로 둔다 — "이름은 비슷한데 다른 표가 여러 개 나오는" 실제 상황을 재현하려는 것.
    seen = set()
    unique = []
    for c in cands:
        if c["tbl_id"] not in seen:
            seen.add(c["tbl_id"])
            unique.append(c)
    return unique


def build_documents(tables: list[dict]) -> tuple[list[str], list[str]]:
    """표 단위 문서(이름만) vs 분류값 포함 문서, 두 세트를 만든다."""
    name_only_docs = []
    with_classes_docs = []
    for t in tables:
        name_only_docs.append(t["tbl_nm"])
        try:
            itm = get_meta(t["org_id"], t["tbl_id"], "ITM")
            class_names = " ".join(
                sorted({row.get("ITM_NM", "") or row.get("OBJ_NM", "") for row in itm})
            )
        except Exception as e:  # noqa: BLE001 - 메타 조회 실패한 표는 이름만으로 대체
            print(f"  [WARN] {t['tbl_id']} 메타 조회 실패: {e}")
            class_names = ""
        with_classes_docs.append(f"{t['tbl_nm']} {class_names}")
    return name_only_docs, with_classes_docs


def rank_tables(docs: list[str], query: str, tables: list[dict]) -> list[tuple[str, str, float]]:
    vec = TfidfVectorizer(analyzer="char", ngram_range=(2, 4))
    doc_matrix = vec.fit_transform(docs)
    query_vec = vec.transform([query])
    sims = cosine_similarity(query_vec, doc_matrix)[0]
    ranked = sorted(zip(tables, sims), key=lambda x: -x[1])
    return [(t["tbl_id"], t["tbl_nm"], round(float(s), 4)) for t, s in ranked]


def main():
    tables = load_candidate_tables()
    print(f"후보 표 {len(tables)}개 (D 노동 대분류, '비정규직' 포함, orgId=101):")
    for t in tables:
        print(f"  {t['tbl_id']}: {t['tbl_nm']}")
    print()

    name_only_docs, with_classes_docs = build_documents(tables)

    print("=== 실험 A: 표 이름만 색인 ===")
    ranked_a = rank_tables(name_only_docs, QUERY, tables)
    for i, (tbl_id, tbl_nm, score) in enumerate(ranked_a, 1):
        mark = " <== 정답" if tbl_id == CORRECT_TBL_ID else ""
        print(f"  {i}. [{score}] {tbl_id} {tbl_nm}{mark}")
    rank_a = next(i for i, (tbl_id, _, _) in enumerate(ranked_a, 1) if tbl_id == CORRECT_TBL_ID)

    print("\n=== 실험 B: 표 이름 + 분류값(itmId) 코드명까지 색인 ===")
    ranked_b = rank_tables(with_classes_docs, QUERY, tables)
    for i, (tbl_id, tbl_nm, score) in enumerate(ranked_b, 1):
        mark = " <== 정답" if tbl_id == CORRECT_TBL_ID else ""
        print(f"  {i}. [{score}] {tbl_id} {tbl_nm}{mark}")
    rank_b = next(i for i, (tbl_id, _, _) in enumerate(ranked_b, 1) if tbl_id == CORRECT_TBL_ID)

    print(f"\n=== 결론 ===")
    print(f"정답 표({CORRECT_TBL_ID}) 순위 — 표 이름만: {rank_a}위 / 분류값 포함: {rank_b}위 (총 {len(tables)}개 후보)")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
