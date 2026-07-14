"""
table_index_experiment.py의 TF-IDF 버전을 실제 HCX 임베딩으로 재실행.

TF-IDF는 "색인 단위(표 단위 vs 분류값 단위)" 효과만 보려고 쓴 대체재였는데,
이제 HCX_API_KEY가 생겨서 실제 임베딩 모델로 같은 실험을 재현해 결론이
바뀌는지 확인한다.

사용 예 (레포 루트에서):
    venv/Scripts/python.exe src/table_index_experiment_hcx.py
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kosis_client import get_meta  # noqa: E402
from hcx_embedding_client import embed  # noqa: E402

TREE_PATH = Path(__file__).resolve().parent.parent / "data" / "kosis_table_tree.json"

QUERY = "비정규직 근로자는 856만8000명으로 전년 동월 대비 11만 명 증가했다"
CORRECT_TBL_ID = "DT_1DE7110_11"


def load_candidate_tables() -> list[dict]:
    tree = json.loads(TREE_PATH.read_text(encoding="utf-8"))
    leaves = tree["D"]["leaves"]
    cands = [
        leaf for leaf in leaves
        if "비정규직" in (leaf["tbl_nm"] or "") and leaf["org_id"] == "101"
    ]
    seen = set()
    unique = []
    for c in cands:
        if c["tbl_id"] not in seen:
            seen.add(c["tbl_id"])
            unique.append(c)
    return unique


def build_documents(tables: list[dict]) -> tuple[list[str], list[str]]:
    name_only_docs = []
    with_classes_docs = []
    for t in tables:
        name_only_docs.append(t["tbl_nm"])
        try:
            itm = get_meta(t["org_id"], t["tbl_id"], "ITM")
            class_names = " ".join(
                sorted({row.get("ITM_NM", "") or row.get("OBJ_NM", "") for row in itm})
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] {t['tbl_id']} 메타 조회 실패: {e}")
            class_names = ""
        with_classes_docs.append(f"{t['tbl_nm']} {class_names}")
    return name_only_docs, with_classes_docs


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def embed_all(texts: list[str]) -> list[np.ndarray]:
    vecs = []
    for t in texts:
        vecs.append(np.array(embed(t)))
        time.sleep(0.1)  # API 호출 간격
    return vecs


def rank_tables(docs: list[str], query: str, tables: list[dict]) -> list[tuple[str, str, float]]:
    doc_vecs = embed_all(docs)
    query_vec = np.array(embed(query))
    sims = [cosine_sim(query_vec, dv) for dv in doc_vecs]
    ranked = sorted(zip(tables, sims), key=lambda x: -x[1])
    return [(t["tbl_id"], t["tbl_nm"], round(s, 4)) for t, s in ranked]


def main():
    tables = load_candidate_tables()
    print(f"후보 표 {len(tables)}개 (D 노동 대분류, '비정규직' 포함, orgId=101)\n")

    name_only_docs, with_classes_docs = build_documents(tables)

    print("=== 실험 A (HCX 임베딩): 표 이름만 색인 ===")
    ranked_a = rank_tables(name_only_docs, QUERY, tables)
    for i, (tbl_id, tbl_nm, score) in enumerate(ranked_a, 1):
        mark = " <== 정답" if tbl_id == CORRECT_TBL_ID else ""
        print(f"  {i}. [{score}] {tbl_id} {tbl_nm}{mark}")
    rank_a = next(i for i, (tbl_id, _, _) in enumerate(ranked_a, 1) if tbl_id == CORRECT_TBL_ID)

    print("\n=== 실험 B (HCX 임베딩): 표 이름 + 분류값(itmId) 코드명까지 색인 ===")
    ranked_b = rank_tables(with_classes_docs, QUERY, tables)
    for i, (tbl_id, tbl_nm, score) in enumerate(ranked_b, 1):
        mark = " <== 정답" if tbl_id == CORRECT_TBL_ID else ""
        print(f"  {i}. [{score}] {tbl_id} {tbl_nm}{mark}")
    rank_b = next(i for i, (tbl_id, _, _) in enumerate(ranked_b, 1) if tbl_id == CORRECT_TBL_ID)

    print(f"\n=== 결론 (HCX 임베딩) ===")
    print(f"정답 표({CORRECT_TBL_ID}) 순위 — 표 이름만: {rank_a}위 / 분류값 포함: {rank_b}위 (총 {len(tables)}개 후보)")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
