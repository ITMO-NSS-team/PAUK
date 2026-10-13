import os
import pickle
import tempfile
import unittest
from pathlib import Path

from pauk.graph.person_resolution import MODEL_FEATURES, PairEvidence, logistic_probability
from pauk.graph.person_resolution_model import (
    DEFAULT_MODEL_PATH,
    ModelArtifactError,
    load_logistic_model,
)


def artifact(feature_names=MODEL_FEATURES, *, intercept=0.0):
    size = len(feature_names)
    return {
        "format_version": 1,
        "model_type": "standardized_logistic_regression",
        "feature_names": tuple(feature_names),
        "means": (0.0,) * size,
        "scales": (1.0,) * size,
        "coefficients": (0.0,) * size,
        "intercept": intercept,
    }


class PersonResolutionModelTest(unittest.TestCase):
    def write_artifact(self, payload) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "model.pkl"
        path.write_bytes(pickle.dumps(payload, protocol=4))
        return path

    def test_default_artifact_preserves_the_fitted_probability(self):
        model = load_logistic_model(DEFAULT_MODEL_PATH, MODEL_FEATURES)
        evidence = PairEvidence(
            person_a="A1",
            name_a="Alexander Petrov",
            person_b="A2",
            name_b="Alexander Petrov",
            shared_coauthors=2,
            shared_departments=1,
            shared_fields=2,
            works_a=8,
            works_b=3,
            surname_occurrences_a=5,
            surname_occurrences_b=5,
        )

        self.assertAlmostEqual(logistic_probability(evidence, model), 0.9006393934, places=9)

    def test_model_can_be_replaced_without_changing_code(self):
        model = load_logistic_model(self.write_artifact(artifact(intercept=0.0)), MODEL_FEATURES)
        features = dict.fromkeys(MODEL_FEATURES, 0.0)

        self.assertEqual(model.probability(features), 0.5)

    def test_replacing_an_artifact_at_the_same_path_invalidates_the_cache(self):
        path = self.write_artifact(artifact(intercept=0.0))
        features = dict.fromkeys(MODEL_FEATURES, 0.0)
        first = load_logistic_model(path, MODEL_FEATURES)
        original = path.stat()

        path.write_bytes(pickle.dumps(artifact(intercept=2.0), protocol=4))
        os.utime(
            path,
            ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000),
        )
        second = load_logistic_model(path, MODEL_FEATURES)

        self.assertEqual(first.probability(features), 0.5)
        self.assertGreater(second.probability(features), 0.8)

    def test_feature_schema_mismatch_fails_before_inference(self):
        path = self.write_artifact(artifact(MODEL_FEATURES[:-1]))

        with self.assertRaisesRegex(ModelArtifactError, "feature schema"):
            load_logistic_model(path, MODEL_FEATURES)

    def test_pickle_globals_are_rejected(self):
        path = self.write_artifact(str)

        with self.assertRaisesRegex(ModelArtifactError, "unsafe"):
            load_logistic_model(path, MODEL_FEATURES)


if __name__ == "__main__":
    unittest.main()
