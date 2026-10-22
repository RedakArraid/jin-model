"""Isolated worker executing only JIN Studio's reviewed training pipeline."""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from jin_runtime.studio_store import ALLOWED_BASE_MODELS, StudioStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_TRAINING_SCRIPTS = frozenset(
    {
        "training/vision/prepare_layout_dataset.py",
        "training/vision/train_layoutlmv3.py",
    }
)


def _within(path: Path, parent: Path) -> bool:
    resolved = path.resolve()
    root = parent.resolve()
    return resolved == root or root in resolved.parents


def validated_command(
    raw_command: Any,
    *,
    store: StudioStore,
    output_directory: str,
) -> list[str]:
    """Convert a stored step to a shell-free command from a strict allowlist."""

    if not isinstance(raw_command, (list, tuple)) or len(raw_command) < 2:
        raise ValueError("Training command must be a non-empty argument list")
    command = [str(item) for item in raw_command]
    if Path(command[0]).name.lower() not in {"python", "python3", "python.exe"}:
        raise ValueError("Training command must use the Python interpreter")
    script = command[1].replace("\\", "/").lstrip("./")
    if script not in ALLOWED_TRAINING_SCRIPTS:
        raise ValueError(f"Training script is not allowed: {script}")
    script_path = (PROJECT_ROOT / script).resolve()
    if not script_path.is_file() or not _within(script_path, PROJECT_ROOT / "training"):
        raise ValueError(f"Training script is unavailable: {script}")

    allowed_output = Path(output_directory).resolve()
    if not _within(allowed_output, store.root / "training"):
        raise ValueError("Training output directory is outside JIN Studio storage")

    path_options = {"--manifest", "--data", "--output"}
    indexed_arguments = command[2:]
    seen_options: set[str] = set()
    for offset, argument in enumerate(indexed_arguments):
        option, separator, inline_value = argument.partition("=")
        if option.startswith("--"):
            seen_options.add(option)
        if option not in path_options:
            continue
        if separator:
            raw_value = inline_value
        elif offset + 1 < len(indexed_arguments):
            raw_value = indexed_arguments[offset + 1]
        else:
            raise ValueError(f"Missing value for {option}")
        candidate = Path(raw_value).resolve()
        if option == "--manifest":
            if not _within(candidate, store.datasets_dir):
                raise ValueError("Dataset manifest is outside JIN Studio storage")
        elif not _within(candidate, allowed_output):
            raise ValueError(f"{option} path is outside the run output directory")

    required = (
        {"--manifest", "--output"}
        if script.endswith("prepare_layout_dataset.py")
        else {"--data", "--output", "--model"}
    )
    missing = sorted(required - seen_options)
    if missing:
        raise ValueError(f"Training command is missing required options: {', '.join(missing)}")
    if "--model" in seen_options:
        model_index = next(
            index
            for index, value in enumerate(indexed_arguments)
            if value == "--model" or value.startswith("--model=")
        )
        model_option = indexed_arguments[model_index]
        _, separator, inline_model = model_option.partition("=")
        model = (
            inline_model
            if separator
            else indexed_arguments[model_index + 1]
            if model_index + 1 < len(indexed_arguments)
            else ""
        )
        if model not in ALLOWED_BASE_MODELS:
            raise ValueError("Training base model is not allowed")

    return [sys.executable, str(script_path), *command[2:]]


def _positive_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name) or str(default))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def execute_training_run(store: StudioStore, run: dict[str, Any]) -> None:
    run_id = str(run["runId"])
    output_directory = Path(str(run["outputDirectory"])).resolve()
    expected_output = (store.root / "training" / run_id).resolve()
    if output_directory != expected_output:
        raise ValueError("Training output directory does not match the run identifier")
    output_directory.mkdir(parents=True, exist_ok=True)
    log_path = Path(str(run["logPath"])).resolve()
    if log_path != output_directory / "training.log":
        raise ValueError("Training log does not match the run directory")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_seconds = _positive_int(
        "JIN_STUDIO_TRAINING_HEARTBEAT_SECONDS", 10, minimum=2, maximum=300
    )
    timeout_seconds = _positive_int(
        "JIN_STUDIO_TRAINING_STEP_TIMEOUT_SECONDS",
        86400,
        minimum=60,
        maximum=604800,
    )
    completed: list[str] = list(run.get("completedSteps") or [])
    steps = run.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Training run does not contain any step")
    step_names = [str(step.get("name") or "") for step in steps if isinstance(step, dict)]
    if len(step_names) != len(steps) or len(step_names) != len(set(step_names)):
        raise ValueError("Training steps must be named JSON objects with unique names")

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        for step in steps:
            name = str(step.get("name") or "unnamed-step")[:160]
            if name in completed:
                continue
            command = validated_command(
                step.get("command"),
                store=store,
                output_directory=str(output_directory),
            )
            store.heartbeat_training_run(
                run_id, current_step=name, completed_steps=completed
            )
            log.write(f"\n[JIN Studio] Starting {name}\n")
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
            )
            started = time.monotonic()
            while process.poll() is None:
                if time.monotonic() - started > timeout_seconds:
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise TimeoutError(
                        f"Training step {name} exceeded {timeout_seconds} seconds"
                    )
                store.heartbeat_training_run(
                    run_id, current_step=name, completed_steps=completed
                )
                time.sleep(heartbeat_seconds)
            if process.returncode != 0:
                raise RuntimeError(
                    f"Training step {name} exited with code {process.returncode}"
                )
            completed.append(name)
            store.heartbeat_training_run(
                run_id, current_step=name, completed_steps=completed
            )
            log.write(f"[JIN Studio] Completed {name}\n")


def run_once(store: StudioStore, worker_id: str) -> bool:
    run = store.claim_training_run(worker_id)
    if run is None:
        return False
    try:
        execute_training_run(store, run)
    except Exception as exc:  # the failure is persisted for the Studio operator
        store.finish_training_run(run["runId"], success=False, error=str(exc))
    else:
        store.finish_training_run(run["runId"], success=True)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated JIN Studio trainer")
    parser.add_argument("--once", action="store_true", help="Process at most one run")
    args = parser.parse_args(argv)
    store = StudioStore()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stale_seconds = _positive_int(
        "JIN_STUDIO_TRAINING_STALE_SECONDS", 900, minimum=60, maximum=86400
    )
    store.recover_stale_training_runs(stale_seconds)
    if args.once:
        run_once(store, worker_id)
        return 0
    poll_seconds = _positive_int(
        "JIN_STUDIO_TRAINING_POLL_SECONDS", 10, minimum=2, maximum=300
    )
    while True:
        if not run_once(store, worker_id):
            time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOWED_TRAINING_SCRIPTS",
    "execute_training_run",
    "main",
    "run_once",
    "validated_command",
]
