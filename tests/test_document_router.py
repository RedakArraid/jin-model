import tempfile
import unittest
from pathlib import Path

import joblib
import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.linear_model import SGDClassifier

from jin_runtime.document_router import CorpusDocumentRouter


class RouterTests(unittest.TestCase):
    def test_compact_router_matches_sklearn_predictions(self):
        with tempfile.TemporaryDirectory() as td:
            vec = HashingVectorizer(
                analyzer="char_wb", ngram_range=(3, 5), n_features=1024,
                alternate_sign=False, norm="l2", lowercase=True,
            )
            samples = [
                "commande fournisseur paris", "commande fournisseur lyon",
                "facture client paris", "facture client lyon",
                "conditions generales vente", "conditions generales achat",
            ]
            model = SGDClassifier(
                loss="log_loss", random_state=42, max_iter=1000,
            ).fit(vec.transform(samples), ["CM", "CM", "CF", "CF", "Autre", "Autre"])
            legacy_path = Path(td) / "legacy.joblib"
            compact_path = Path(td) / "compact.joblib"
            joblib.dump({
                "version": "test", "n_features": 1024,
                "family_model": model, "decision_model": model,
            }, legacy_path)
            compact_model = {
                "classes": model.classes_,
                "coef": model.coef_.astype(np.float32),
                "intercept": model.intercept_.astype(np.float32),
            }
            joblib.dump({
                "version": "test", "n_features": 1024,
                "format": "dense-ovr-v1",
                "family": compact_model, "decision": compact_model,
            }, compact_path)
            legacy = CorpusDocumentRouter(legacy_path)
            compact = CorpusDocumentRouter(compact_path)
            self.assertTrue(compact.status()["loaded"])
            for sample in samples:
                with self.subTest(sample=sample):
                    expected = legacy.predict(text=sample)
                    actual = compact.predict(text=sample)
                    for target in ("family", "decision"):
                        self.assertEqual(actual[target], expected[target])
                        self.assertAlmostEqual(
                            actual[target + "_confidence"],
                            expected[target + "_confidence"], places=5,
                        )

    def test_router_loads_and_predicts(self):
        with tempfile.TemporaryDirectory() as td:
            vec = HashingVectorizer(
                analyzer="char_wb",
                ngram_range=(3, 5),
                n_features=1024,
                alternate_sign=False,
                norm="l2",
                lowercase=True,
            )
            x = [
                "CM commande izi paris",
                "CM commande izi lyon",
                "CF commande verney",
                "CF commande fournisseur verney",
            ]
            fam = ["CM", "CM", "CF", "CF"]
            dec = ["keep", "keep", "review", "review"]
            fm = SGDClassifier(
                loss="log_loss", random_state=42, max_iter=1000
            ).fit(vec.transform(x), fam)
            dm = SGDClassifier(
                loss="log_loss", random_state=42, max_iter=1000
            ).fit(vec.transform(x), dec)
            path = Path(td) / "m.joblib"
            joblib.dump(
                {
                    "version": "test",
                    "n_features": 1024,
                    "family_model": fm,
                    "decision_model": dm,
                    "metrics": {},
                },
                path,
            )
            router = CorpusDocumentRouter(path)
            pred = router.predict(
                filename="CM-123.pdf",
                subject="commande IZI",
                text="paris",
            )
            self.assertIsNotNone(pred)
            self.assertIn("family", pred)
            self.assertIn("decision", pred)


if __name__ == "__main__":
    unittest.main()
