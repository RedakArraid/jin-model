import tempfile
import unittest
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.linear_model import SGDClassifier

from jin_runtime.document_router import CorpusDocumentRouter


class RouterTests(unittest.TestCase):
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
