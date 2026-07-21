from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from source_scope_classifier import classify_source_scope, load_kosis_org_catalog


class SourceScopeClassifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_kosis_org_catalog(ROOT / "data" / "kosis_org_names.json")

    def test_exact_catalog_name_is_kosis(self) -> None:
        decision = classify_source_scope("농림축산식품부", self.catalog)
        self.assertEqual(decision.scope, "KOSIS등재")
        self.assertEqual(decision.matched_org_name, "농림축산식품부")

    def test_verified_alias_is_kosis(self) -> None:
        decision = classify_source_scope("통계청", self.catalog)
        self.assertEqual(decision.scope, "KOSIS등재")
        self.assertEqual(decision.matched_org_name, "국가데이터처")

    def test_unknown_name_cannot_be_promoted_to_kosis(self) -> None:
        decision = classify_source_scope("KOSIS에서 조회 가능한 멋진 기관", self.catalog)
        self.assertNotEqual(decision.scope, "KOSIS등재")
        self.assertEqual(decision.matched_org_id, "")

    def test_missing_or_noise_source_is_unknown(self) -> None:
        for value in (None, "", "불명", "관련 기관 보도자료"):
            with self.subTest(value=value):
                self.assertEqual(classify_source_scope(value, self.catalog).scope, "불명")

    def test_non_kosis_categories_are_deterministic(self) -> None:
        self.assertEqual(classify_source_scope("미국 에너지부", self.catalog).scope, "해외기관")
        self.assertEqual(classify_source_scope("금융감독원", self.catalog).scope, "공식기관_비KOSIS")
        self.assertEqual(classify_source_scope("부동산R114", self.catalog).scope, "민간기관")


if __name__ == "__main__":
    unittest.main()
