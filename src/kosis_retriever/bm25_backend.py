# -*- coding: utf-8 -*-
"""BM25 색인 클래스 + 한국어 형태소 토크나이저 (검색단 자기완결용).

검색기(retriever.py)가 쓰는 최소 구성만 담는다:
  - KiwiRepresentations : Kiwi 형태소 토크나이저(질의 토큰화). all_morphemes / expanded_core 채널.
  - DocMetaBM25         : doc_meta_text에 대한 Okapi BM25 색인(build/search). bm25_*.pkl 언피클 대상.
  - Document / PathHit  : 색인 문서·검색 결과 컨테이너.

의존은 표준 라이브러리 + kiwipiepy 뿐. (BM25 pkl은 이 모듈의 DocMetaBM25/Document로 언피클된다.)
"""
from __future__ import annotations

import heapq
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


# ── 형태소 태그·불용 상수 ──────────────────────────────────────────────
KEEP_ALL = ("NN", "NR", "NP", "VV", "VA", "VX", "XR", "MM", "SL", "SN")
NOMINAL = ("NN", "NR", "NP", "SL", "SN")
SUBJECT_PARTICLES = {"JKS"}
OBJECT_PARTICLES = {"JKO"}
TOPIC_FORMS = {"은", "는"}
LOCATION_FORMS = {"에", "에서"}
STOP_NOUNS = {"것", "수", "등", "중", "때", "정도", "경우", "하나"}


class KiwiRepresentations:
    def __init__(self):
        try:
            from kiwipiepy import Kiwi
        except ImportError as exc:
            raise RuntimeError("python -m pip install kiwipiepy") from exc
        self.kiwi = Kiwi(num_workers=-1)

    @staticmethod
    def _term(form: str) -> str:
        return f"m:{form.lower()}"

    @staticmethod
    def _is_nominal(token: Any) -> bool:
        return token.tag.startswith(NOMINAL) or token.tag in {"XSN", "MM"}

    def _nominal_chunk_before(self, tokens: Sequence[Any], particle_index: int) -> list[str]:
        selected: list[str] = []
        index = particle_index - 1
        while index >= 0 and self._is_nominal(tokens[index]):
            token = tokens[index]
            if token.tag.startswith(NOMINAL) and clean(token.form):
                selected.append(self._term(token.form))
            index -= 1
        selected.reverse()
        return selected

    def all_morphemes_from_tokens(self, tokens: Sequence[Any]) -> list[str]:
        return [
            self._term(token.form)
            for token in tokens
            if token.tag.startswith(KEEP_ALL) and clean(token.form)
        ]

    def all_morphemes(self, text: str) -> list[str]:
        return self.all_morphemes_from_tokens(self.kiwi.tokenize(clean(text)))

    def tokenize_many(self, texts: Sequence[str]) -> list[list[str]]:
        normalized = [clean(text) for text in texts]
        return [self.all_morphemes_from_tokens(tokens) for tokens in self.kiwi.tokenize(normalized)]

    def sov(self, text: str) -> list[str]:
        tokens = self.kiwi.tokenize(clean(text))
        selected: list[str] = []
        for index, token in enumerate(tokens):
            if token.tag in SUBJECT_PARTICLES | OBJECT_PARTICLES:
                selected.extend(self._nominal_chunk_before(tokens, index))
            if token.tag.startswith(("VV", "VX")):
                selected.append(self._term(token.form))
        return list(dict.fromkeys(selected))

    def expanded_core(self, text: str) -> list[str]:
        tokens = self.kiwi.tokenize(clean(text))
        selected = self.sov(text)
        for index, token in enumerate(tokens):
            if token.tag == "JX" and token.form in TOPIC_FORMS:
                selected.extend(self._nominal_chunk_before(tokens, index))
            if token.tag == "JKB" and token.form in LOCATION_FORMS:
                selected.extend(self._nominal_chunk_before(tokens, index))
            is_content_noun = token.tag.startswith("NN") or token.tag == "SL"
            if is_content_noun and len(clean(token.form)) >= 2 and token.form not in STOP_NOUNS:
                selected.append(self._term(token.form))
            if token.tag.startswith(("VV", "VA", "VX")):
                selected.append(self._term(token.form))
        return list(dict.fromkeys(selected))


# ── 색인 문서·검색 결과 컨테이너 ───────────────────────────────────────
@dataclass(frozen=True)
class Document:
    table_key: str
    org_id: str
    tbl_name: str


@dataclass
class PathHit:
    table_key: str
    path: str
    rank: int
    raw_score: float
    tbl_name: str = ""
    payload: dict = field(default_factory=dict)


class DocMetaBM25:
    """Okapi BM25 whose document is exactly Catalog doc_meta_text."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents: list[Document] = []
        self.lengths: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.idf: dict[str, float] = {}
        self.avg_length = 0.0

    @classmethod
    def build(cls, catalog: Path, kiwi: KiwiRepresentations) -> "DocMetaBM25":
        index = cls()
        rows = [json.loads(line) for line in catalog.open(encoding="utf-8-sig") if line.strip()]
        texts = [str(row.get("doc_meta_text") or "").strip() for row in rows]
        if any(not text for text in texts):
            raise ValueError("catalog contains empty doc_meta_text")
        token_rows = kiwi.tokenize_many(texts)
        for row, tokens in zip(rows, token_rows):
            doc_id = len(index.documents)
            index.documents.append(Document(
                table_key=str(row["table_key"]),
                org_id=str(row.get("org_id") or ""),
                tbl_name=str(row.get("tbl_name") or ""),
            ))
            counts = Counter(tokens)
            index.lengths.append(sum(counts.values()))
            for term, frequency in counts.items():
                index.postings[term].append((doc_id, frequency))
        size = len(index.documents)
        index.avg_length = sum(index.lengths) / size if size else 0.0
        index.idf = {
            term: math.log(1.0 + (size - len(rows) + 0.5) / (len(rows) + 0.5))
            for term, rows in index.postings.items()
        }
        return index

    def search(
        self,
        query: str,
        tokenizer: Callable[[str], list[str]],
        limit: int,
        org_ids: set[str] | None = None,
    ) -> tuple[list[PathHit], list[str]]:
        terms = list(dict.fromkeys(tokenizer(query)))
        scores: defaultdict[int, float] = defaultdict(float)
        for term in terms:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for doc_id, frequency in self.postings[term]:
                document = self.documents[doc_id]
                if org_ids and document.org_id not in org_ids:
                    continue
                length = self.lengths[doc_id]
                norm = self.k1 * (
                    1.0 - self.b + self.b * length / self.avg_length
                ) if self.avg_length else self.k1
                scores[doc_id] += idf * frequency * (self.k1 + 1.0) / (frequency + norm)
        best = heapq.nsmallest(
            limit,
            scores.items(),
            key=lambda item: (-item[1], self.documents[item[0]].table_key),
        )
        return [
            PathHit(
                table_key=self.documents[doc_id].table_key,
                path="",
                rank=rank,
                raw_score=round(float(score), 8),
                tbl_name=self.documents[doc_id].tbl_name,
                payload={"org_id": self.documents[doc_id].org_id},
            )
            for rank, (doc_id, score) in enumerate(best, start=1)
            if score > 0
        ], terms
