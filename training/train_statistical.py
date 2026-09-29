#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jin_runtime.learning import StatisticalAddressLearner


def main() -> int:
    parser = argparse.ArgumentParser(description="Train JIN statistical memory from corrected historical extractions.")
    parser.add_argument("--feedback", default="feedback/learning_feedback.jsonl")
    parser.add_argument("--model-dir", default="data/learning")
    args = parser.parse_args()

    learner = StatisticalAddressLearner(feedback_path=Path(args.feedback), model_dir=Path(args.model_dir))
    status = learner.train()
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0 if status.get("enabled") else 2


if __name__ == "__main__":
    raise SystemExit(main())
