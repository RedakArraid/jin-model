from __future__ import annotations

import json

from scripts.reprocess_studio_documents import (
    _partition_replaceable,
    _replaceability_reason,
)


def _row(*, status="DRAFT", reviewer="jin-model-preannotation-v3", fields=None):
    return {
        "document_id": "jdoc-test",
        "filename": "commande.pdf",
        "status": status,
        "annotation_json": json.dumps(
            {
                "reviewed_by": reviewer,
                "field_annotations": fields
                if fields is not None
                else [{"status": "UNREVIEWED", "corrected_value": None}],
            }
        ),
    }


def test_machine_drafts_from_current_and_legacy_imports_are_replaceable():
    assert _replaceability_reason(_row()) == ""
    assert _replaceability_reason(_row(reviewer="codex-preannotation")) == ""
    assert _replaceability_reason(_row(reviewer="codex-preannotation-v3")) == ""


def test_human_status_reviewer_and_field_changes_are_never_superseded():
    assert "status=" in _replaceability_reason(_row(status="HUMAN_VALIDATED"))
    assert "reviewer=" in _replaceability_reason(_row(reviewer="khadara"))
    assert "human field review" in _replaceability_reason(
        _row(fields=[{"status": "CONFIRMED", "corrected_value": None}])
    )
    assert "human correction" in _replaceability_reason(
        _row(fields=[{"status": "UNREVIEWED", "corrected_value": "PO-42"}])
    )


def test_partition_keeps_safe_machine_drafts_and_reports_human_work():
    machine = _row()
    human = _row(reviewer="khadara")
    human["document_id"] = "jdoc-human"
    human["filename"] = "revue.pdf"

    selected, skipped = _partition_replaceable([machine, human])

    assert selected == [machine]
    assert skipped == [
        {
            "document_id": "jdoc-human",
            "filename": "revue.pdf",
            "reason": "reviewer=khadara",
        }
    ]
