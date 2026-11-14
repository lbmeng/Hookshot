"""Compile the actual telemetry collector and exercise the runner with a synthetic child."""
import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

RUN_ALL_ARGS = ["--cc", "gcc"]

ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    spec = importlib.util.spec_from_file_location("memory_experiment", ROOT / "scripts/memory_experiment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default="gcc")
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="memory-experiment-", dir=os.environ.get("TMPDIR")))
    exe = work / ("fixture.exe" if os.name == "nt" else "fixture")
    subprocess.run([args.cc, "-std=c11", "-Wall", "-Wextra", "-Werror"] + ([] if os.name == "nt" else ["-pthread"]) +
                   [str(ROOT / "host/memory_profile.c"), str(ROOT / "tests/memory_profile_fixture.c"), "-o", str(exe)], check=True)
    env = dict(os.environ, HOS_MEMORY_PROFILE=str(work / "memory.csv"), HOS_MEMORY_SAMPLE_MS="1000")
    subprocess.run([str(exe)], env=env, check=True)
    with (work / "memory.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    last = rows[-1]
    assert int(last["guest_address_union_bytes"]) == 6 * 4096
    assert int(last["guest_explicit_union_bytes"]) == 4 * 4096
    assert int(last["guest_pages_bytes"]) == 0
    assert int(last["nvn_pool_registered_bytes"]) == 4 * 4096
    assert int(last["nvn_pool_unique_address_bytes"]) == 3 * 4096
    assert max(int(row["guest_address_union_interval_peak_bytes"]) for row in rows) == 10 * 4096
    assert max(int(row["guest_explicit_union_interval_peak_bytes"]) for row in rows) == 8 * 4096
    assert int(last["vk_local_blocks_bytes"]) == 0
    assert max(int(row["vk_local_blocks_interval_peak_bytes"]) for row in rows) == 48 << 20
    assert int(last["vk_buffer_allocations_bytes"]) == 0
    assert int(last["allocation_failures"]) == 1
    assert int(last["telemetry_errors"]) == 0
    assert int(last["presents"]) == 30
    disabled = dict(os.environ)
    disabled.pop("HOS_MEMORY_PROFILE", None)
    subprocess.run([str(exe)], env=disabled, check=True)
    runner = load_runner()
    game, romfs, user = (work / name for name in ("game", "romfs", "baseline-user"))
    for folder in (game, romfs, user):
        folder.mkdir()
    target = game / exe.name
    shutil.copy2(exe, target)
    (game / ("hookshot_vk.dll" if os.name == "nt" else "hookshot_vk.so")).write_text("synthetic backend placeholder\n")
    (game / "hookshot_options.ini").write_text("frame_generation=4\n")
    (user / "save.txt").write_text("baseline\n")
    image = work / "image.bin"
    image.write_text("synthetic fixture input\n")
    config = {"game_dir": str(game), "executable": str(target), "image": str(image), "romfs": str(romfs),
              "user_template": str(user), "budgets_mib": [9809, 4096, 3285], "repeats": 1,
              "seconds": 2, "sample_ms": 20, "environment": {"FIXTURE_EXIT_BELOW": "4096"},
              "required_phases": ["forest-steady"],
              "scheduled_phases": [{"at_seconds": 0.01, "phase": "scheduled-transition"}]}
    config_path = work / "experiment.json"
    runner.write_json(config_path, config)
    runner.load_config(config_path)
    out = work / "sweep"
    out.mkdir()
    assert runner.sweep(config, out) == 2
    results = json.loads((out / "summary.json").read_text())["runs"]
    assert len(results) == 3
    by_budget = {row["budget_mib"]: row for row in results}
    assert by_budget[9809]["measurement_status"] == "captured"
    assert by_budget[4096]["measurement_status"] == "captured"
    assert by_budget[3285]["measurement_status"] == "game_failed"
    assert "scheduled-transition" in by_budget[9809]["phases"]
    assert by_budget[9809]["scene_coverage"] == "scheduled_only"
    assert by_budget[9809]["missing_observed_phases"] == ["forest-steady"]
    assert (user / "save.txt").read_text() == "baseline\n"
    assert not (user / "fixture-output.txt").exists()
    assert (game / "hookshot_options.ini").read_text() == "frame_generation=4\n"
    assert "frame_generation=0" in (out / "budget-9809-run-1/runtime/hookshot_options.ini").read_text()
    missing = out / "budget-1-run-1"
    missing.mkdir()
    runner.write_json(missing / "result.json", {"run": missing.name, "budget_mib": 1, "exit_code": 0, "stop_reason": "exited"})
    assert runner.summarize_run(missing)["measurement_status"] == "telemetry_missing"
    protected = work / "already.csv"
    protected.write_text("existing data\n")
    subprocess.run([str(exe)], env=dict(os.environ, HOS_MEMORY_PROFILE=str(protected)), check=True)
    assert protected.read_text() == "existing data\n"
    config["environment"]["RX_VK_NO_MESH"] = "1"
    runner.write_json(config_path, config)
    try:
        runner.load_config(config_path)
    except ValueError:
        pass
    else:
        raise AssertionError("Skipped geometry must not be benchmarked")
    print(f"PASS: overlap accounting, split/unmap, concurrent updates, transient peaks, isolation, budget sweep and failure reporting ({work})")


if __name__ == "__main__":
    main()
