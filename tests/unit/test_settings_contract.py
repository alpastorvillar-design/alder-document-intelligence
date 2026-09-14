"""Settings expose only configuration the application actually consumes."""

from iep.config import Settings
from iep.validation.rules import MIN_MEAN_OCR_CONFIDENCE


def test_ocr_mean_confidence_has_one_source_of_truth() -> None:
    assert "ocr_min_word_confidence" not in Settings.model_fields
    assert MIN_MEAN_OCR_CONFIDENCE == 78.0
