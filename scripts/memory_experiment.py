"""Run an isolated memory-budget sweep and summarize runtime and OS telemetry.

Only the Python standard library is required, on Windows and Linux.
"""
import argparse
import csv
import ctypes
import hashlib
import json
import math
import mmap
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RENDERER_API_VERSION = 16
DEFAULT_BUDGETS = [9809, 8192, 6144, 4096, 3285]
PHASE = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
OPTIONS = """options_version=2
quality_preset=0
frame_generation=0
frame_pacing=0
resolution=4
upscaling=0
sharpness=0
handheld_profile=1
game_speed=1
ultrawide=0
"""
CACHE_FILES = {"hookshot_shaders.bin", "hookshot_programs.bin", "hookshot_programs.idx",
               "hookshot_pipelines.bin", "mkw_vk_pipelines.bin"}
ENVIRONMENT = {"RX_BACKEND": "vulkan", "HOS_HANDHELD": "1", "HOS_DYNRES": "0",
    "HOS_RES_SCALE": "0.5", "HOS_REALTIME_STEP": "1", "HOS_RES_LOG": "1",
    "HOOKSHOT_NO_UPDATE_CHECK": "1"}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def atomic_text(path, value):
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(value + "\n", encoding="utf-8")
    os.replace(temporary, path)


def scratch_base():
    value = os.environ.get("TMPDIR")
    if not value:
        raise ValueError("Set TMPDIR to a private scratch directory before running")
    path = Path(value).absolute()
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir():
        raise ValueError("TMPDIR must be a directory, not a symlink")
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("TMPDIR must be owned by this user and have mode 0700")
    return path.resolve()


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    for name in ("game_dir", "executable", "image", "romfs", "user_template"):
        p = Path(config[name])
        if not p.is_absolute() or not p.exists():
            raise ValueError(f"{name} must be an existing absolute path: {p}")
        if name in ("game_dir", "romfs", "user_template") and not p.is_dir():
            raise ValueError(f"{name} must be a directory")
        if name in ("executable", "image") and not p.is_file():
            raise ValueError(f"{name} must be a file")
        config[name] = str(p.resolve())
    exe = Path(config["executable"])
    if exe.parent not in (Path(config["game_dir"]), Path(config["game_dir"]) / "bin"):
        raise ValueError("The executable must be directly in game_dir or game_dir/bin")
    with exe.open("rb") as stream:
        if not exe.stat().st_size:
            raise ValueError("The executable is empty")
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as binary:
            if binary.find(b"HOS_MEMORY_PROFILE") < 0:
                raise ValueError("Rebuild the game executable and Vulkan backend with memory telemetry first")
    renderer = exe.parent / ("hookshot_vk.dll" if os.name == "nt" else "hookshot_vk.so")
    if not renderer.is_file():
        raise ValueError(f"The Vulkan backend must be beside the executable: {renderer}")
    config.setdefault("budgets_mib", DEFAULT_BUDGETS)
    config.setdefault("seconds", 300)
    config.setdefault("repeats", 1)
    config.setdefault("sample_ms", 100)
    config.setdefault("environment", {})
    config.setdefault("scheduled_phases", [])
    config.setdefault("required_phases", [])
    if not config["budgets_mib"] or any(type(n) is not int or n <= 0 for n in config["budgets_mib"]):
        raise ValueError("budgets_mib must contain positive integers")
    if len(set(config["budgets_mib"])) != len(config["budgets_mib"]):
        raise ValueError("budgets_mib must not contain duplicates")
    if (type(config["repeats"]) is not int or config["repeats"] < 1 or not math.isfinite(config["seconds"])
            or config["seconds"] <= 0 or type(config["sample_ms"]) is not int or not 10 <= config["sample_ms"] <= 1000):
        raise ValueError("Use repeats >= 1, seconds > 0, and sample_ms between 10 and 1000")
    forbidden = ("HOS_MEMORY_", "HOS_ENV_FILE", "RX_VK_NO_MESH", "RX_VK_FRAMEGEN_MULT")
    for key, value in config["environment"].items():
        if key.startswith(forbidden) or key in ("HOS_OPTIONS_FILE", "RX_VK_FORCE_MESH_FALLBACK"):
            raise ValueError(f"Reserved or incompatible experiment variable: {key}")
        if not isinstance(value, str) or "\n" in value or "\0" in value:
            raise ValueError(f"Environment values must be single-line strings: {key}")
    for event in config["scheduled_phases"]:
        if event["at_seconds"] < 0 or not PHASE.fullmatch(event["phase"]):
            raise ValueError("Scheduled phases need nonnegative times and simple phase names")
    if any(not PHASE.fullmatch(phase) for phase in config["required_phases"]):
        raise ValueError("required_phases must contain simple phase names")
    return config


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def stage_runtime(config, run):
    source = Path(config["game_dir"])
    destination = run / "runtime"
    destination.mkdir()
    for folder in (source, source / "bin"):
        if not folder.is_dir():
            continue
        target = destination / folder.relative_to(source)
        target.mkdir(exist_ok=True)
        for path in folder.iterdir():
            name = path.name.lower()
            if path.is_file() and (path.resolve() == Path(config["executable"]) or name.endswith((".dll", ".so"))
                                   or ".so." in name or name == "language.txt" or name in CACHE_FILES):
                shutil.copy2(path, target / path.name)
        (target / "hookshot_options.ini").write_text(OPTIONS, encoding="utf-8")
    shutil.copytree(config["user_template"], run / "user")
    shutil.copy2(config["image"], destination / "image.bin")
    return destination / Path(config["executable"]).relative_to(source)


class ProcessMemory:
    def __init__(self, pid):
        self.pid, self.handle = pid, None
        if os.name == "nt":
            from ctypes import wintypes as w
            class Counters(ctypes.Structure):
                _fields_ = [("cb", w.DWORD), ("faults", w.DWORD)] + [(name, ctypes.c_size_t) for name in (
                    "peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "commit", "peak_commit", "private")]
            self.counters = Counters
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            self.kernel.OpenProcess.restype = w.HANDLE
            self.kernel.CloseHandle.argtypes = [w.HANDLE]
            self.query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
            self.query.argtypes = [w.HANDLE, ctypes.POINTER(Counters), w.DWORD]
            self.query.restype = w.BOOL
            self.handle = self.kernel.OpenProcess(0x0400 | 0x0010, False, pid)

    def read(self):
        if os.name == "nt":
            c = self.counters()
            c.cb = ctypes.sizeof(c)
            if self.handle and self.query(self.handle, ctypes.byref(c), c.cb):
                return {"rss_bytes": c.rss, "global_peak_rss_bytes": c.peak_rss, "private_commit_bytes": c.private,
                        "swap_bytes": "", "measurement_error": ""}
            return {"measurement_error": f"GetProcessMemoryInfo failed: {ctypes.get_last_error()}"}
        try:
            values = {}
            for line in Path(f"/proc/{self.pid}/status").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in ("VmRSS", "VmHWM", "VmSwap"):
                    values[key] = int(value.split()[0]) * 1024
            if "VmRSS" not in values:
                raise ValueError("Process has no resident-memory counters")
            return {"rss_bytes": values["VmRSS"], "global_peak_rss_bytes": values.get("VmHWM", ""),
                    "private_commit_bytes": "", "swap_bytes": values.get("VmSwap", ""), "measurement_error": ""}
        except (OSError, ValueError) as error:
            return {"measurement_error": str(error)}

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)


def mark(out, phase, source="manual"):
    if not PHASE.fullmatch(phase):
        raise ValueError("A phase uses 1-80 ASCII letters, digits, dots, dashes or underscores")
    active = json.loads((out / "active.json").read_text())
    run = Path(active["run"])
    if not run.is_relative_to(out.resolve()) or (run / "result.json").exists():
        raise ValueError("There is no active run to mark")
    atomic_text(run / "phase.txt", phase)
    with (run / "markers.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"monotonic_ns": time.perf_counter_ns(), "phase": phase, "source": source}) + "\n")


def summarize_run(run):
    result = json.loads((run / "result.json").read_text())
    profile = run / "memory.csv"
    if not profile.exists():
        return dict(result, measurement_status="telemetry_missing", phases={})
    try:
        with profile.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        if not rows:
            raise ValueError("No telemetry samples")
        required = ("monotonic_ns", "budget_bytes", "guest_address_union_bytes", "presents", "renderer_api", "telemetry_errors")
        for row in rows:
            for field in required:
                int(row[field])
        if any(int(row["budget_bytes"]) not in (0, result["budget_mib"] << 20) for row in rows):
            raise ValueError("Game-reported budget differs from the requested budget")
        valid = [row for row in rows if int(row["budget_bytes"]) == result["budget_mib"] << 20]
        if not valid:
            raise ValueError("The game never reported its memory budget")
        status = "captured"
        if max(int(row["renderer_api"]) for row in rows) != RENDERER_API_VERSION:
            status = "renderer_telemetry_missing"
        elif not max(int(row["presents"]) for row in rows):
            status = "no_presents"
        elif max(int(row["telemetry_errors"]) for row in rows):
            status = "telemetry_error"
        elif result["stop_reason"] == "interrupted":
            status = "interrupted"
        elif result["exit_code"] and result["stop_reason"] == "exited":
            status = "game_failed"
        phases = {}
        for row in valid:
            phase = phases.setdefault(row["phase"], {"samples": 0, "peaks": {}, "sampled_rss_peak_bytes": None})
            phase["samples"] += 1
            for key, value in row.items():
                if key.endswith("_bytes") and value:
                    phase["peaks"][key] = max(phase["peaks"].get(key, 0), int(value))
        with (run / "process.csv").open(newline="") as stream:
            process = list(csv.DictReader(stream))
        for row in process:
            if row.get("rss_bytes") and row["phase"] in phases:
                phase = phases[row["phase"]]
                phase["sampled_rss_peak_bytes"] = max(phase["sampled_rss_peak_bytes"] or 0, int(row["rss_bytes"]))
        os_peaks = [int(row["global_peak_rss_bytes"]) for row in process if row.get("global_peak_rss_bytes")]
        if not os_peaks and status == "captured":
            status = "process_telemetry_missing"
        markers = []
        if (run / "markers.jsonl").exists():
            markers = [json.loads(line) for line in (run / "markers.jsonl").read_text().splitlines() if line]
        manual = {item["phase"] for item in markers if item["source"] == "manual"}
        timestamps = [int(row["monotonic_ns"]) for row in rows]
        return dict(result, measurement_status=status, phases=phases,
                    max_runtime_sample_gap_ms=max((b - a for a, b in zip(timestamps, timestamps[1:])), default=0) / 1e6,
                    manual_phases=sorted(manual), missing_observed_phases=sorted(set(result.get("required_phases", [])) - manual),
                    scene_coverage="manual_markers" if manual else "scheduled_only" if markers else "unmarked",
                    global_peak_rss_bytes=max(os_peaks) if os_peaks else None,
                    allocation_failures=max(int(row["allocation_failures"]) for row in rows),
                    observed_presents=max(int(row["presents"]) for row in rows))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return dict(result, measurement_status="invalid_telemetry", telemetry_error=str(error), phases={})


def report(out):
    runs = [summarize_run(path.parent) for path in sorted(out.glob("budget-*/result.json"))]
    write_json(out / "summary.json", {"runs": runs, "notes": [
        "Allocation interval peaks include allocations freed between samples",
        "Phase peaks span adjacent runtime samples; use actual timestamps/gaps, not the nominal sample_ms, at boundaries",
        "Guest ranges and NVN pools overlap; imported aliases and VMA suballocations must not be added to blocks",
        "RSS, private commit and device-memory blocks are different measurements, not an additive total",
        "Per-phase RSS peaks are sampled; OS high-water RSS applies to the entire process",
        "Forced termination can lose the final telemetry interval; no exact physical-memory minimum is inferred"]})
    lines = ["# Memory budget experiment", "", "| Budget MiB | Run | Status | RSS high-water MiB | Presents | Failures |",
             "|---|---|---|---|---|---|"]
    for run in runs:
        rss = run.get("global_peak_rss_bytes")
        lines.append(f"| {run['budget_mib']} | {run['run']} | {run['measurement_status']} | "
                     f"{rss / 1048576:.1f} | {run.get('observed_presents', '-')} | {run.get('allocation_failures', '-')} |"
                     if rss is not None else f"| {run['budget_mib']} | {run['run']} | {run['measurement_status']} | unavailable | - | - |")
        for phase, data in run["phases"].items():
            lines.extend(["", f"## {run['run']}: {phase}", "", "| Metric | Peak MiB |", "|---|---|"])
            for key, value in sorted(data["peaks"].items()):
                if "interval_peak" in key:
                    lines.append(f"| {key} | {value / 1048576:.2f} |")
        lines.extend(["", f"Scene coverage: {run.get('scene_coverage', 'unavailable')}; missing observed phases: "
                      + ", ".join(run.get("missing_observed_phases", []))])
    lines.extend(["", "See summary.json for scope and double-counting limitations.", ""])
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return runs


def terminate(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def sweep(config, out):
    fingerprint = {"config": config, "python": sys.version, "platform": platform.platform(),
                   "processor": platform.processor(),
                   "executable_sha256": digest(Path(config["executable"])), "image_sha256": digest(Path(config["image"]))}
    renderer = Path(config["executable"]).parent / ("hookshot_vk.dll" if os.name == "nt" else "hookshot_vk.so")
    fingerprint["renderer_sha256"] = digest(renderer)
    fingerprint["fixed_options"] = OPTIONS
    try:
        fingerprint["source_commit"] = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        fingerprint["source_commit"] = "unavailable"
    write_json(out / "manifest.json", fingerprint)
    any_failed = False
    for budget in config["budgets_mib"]:
        for repetition in range(1, config["repeats"] + 1):
            run = out / f"budget-{budget}-run-{repetition}"
            run.mkdir()
            executable = stage_runtime(config, run)
            (run / "phase.txt").write_text("unmarked\n", encoding="utf-8")
            (run / "environment.txt").write_text("\n", encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if not key.startswith(("HOS_", "RX_", "HOOKSHOT_"))}
            env.update(ENVIRONMENT)
            env.update(config["environment"])
            env.update(HOS_ENV_FILE=str(run / "environment.txt"), HOS_MEMORY_MB=str(budget),
                       HOS_MEMORY_PROFILE=str(run / "memory.csv"), HOS_MEMORY_PHASE_FILE=str(run / "phase.txt"),
                       HOS_MEMORY_SAMPLE_MS=str(config["sample_ms"]))
            write_json(run / "environment.json", {key: value for key, value in env.items() if key.startswith(("HOS_", "RX_", "HOOKSHOT_"))})
            write_json(out / "active.json", {"run": str(run), "budget_mib": budget})
            print(f"Running {run.name}; mark observed scene boundaries in another terminal", flush=True)
            print(f"  python -I {Path(__file__).resolve()} mark --out {out} --phase enter-house-start", flush=True)
            start = time.perf_counter_ns()
            stop_reason, process = "duration", None
            interrupted = False
            with (run / "stdout.log").open("w", encoding="utf-8") as stdout, \
                 (run / "stderr.log").open("w", encoding="utf-8") as stderr, \
                 (run / "process.csv").open("w", newline="", encoding="utf-8") as samples:
                command = [str(executable), str(run / "runtime/image.bin"), "--romfs", config["romfs"], "--user", str(run / "user")]
                process = subprocess.Popen(command, cwd=executable.parent, env=env, stdout=stdout, stderr=stderr,
                                           start_new_session=os.name != "nt")
                probe = None
                try:
                    probe = ProcessMemory(process.pid)
                    writer = csv.DictWriter(samples, fieldnames=["monotonic_ns", "phase", "rss_bytes", "global_peak_rss_bytes",
                                                                 "private_commit_bytes", "swap_bytes", "measurement_error"])
                    writer.writeheader()
                    scheduled = sorted(config["scheduled_phases"], key=lambda event: event["at_seconds"])
                    while process.poll() is None:
                        now = time.perf_counter_ns()
                        elapsed = (now - start) / 1e9
                        while scheduled and scheduled[0]["at_seconds"] <= elapsed:
                            mark(out, scheduled.pop(0)["phase"], "scheduled")
                        phase = (run / "phase.txt").read_text().strip()
                        writer.writerow(dict(monotonic_ns=now, phase=phase, **probe.read()))
                        samples.flush()
                        if elapsed >= config["seconds"]:
                            break
                        time.sleep(config["sample_ms"] / 1000)
                    if process.poll() is not None:
                        stop_reason = "exited"
                except KeyboardInterrupt:
                    stop_reason, interrupted = "interrupted", True
                finally:
                    terminate(process)
                    if probe:
                        probe.close()
            write_json(run / "result.json", {"run": run.name, "budget_mib": budget, "exit_code": process.returncode,
                "stop_reason": stop_reason, "elapsed_seconds": (time.perf_counter_ns() - start) / 1e9,
                "sample_ms": config["sample_ms"], "required_phases": config["required_phases"]})
            result = next(item for item in report(out) if item["run"] == run.name)
            print(f"{run.name}: {result['measurement_status']}", flush=True)
            if interrupted:
                return 130
            if result["measurement_status"] in ("telemetry_missing", "renderer_telemetry_missing", "invalid_telemetry", "telemetry_error"):
                return 2
            any_failed |= result["measurement_status"] != "captured"
    return 2 if any_failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "run"):
        p = commands.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        if name == "run":
            p.add_argument("--out", type=Path)
            p.add_argument("--budget", type=int, action="append")
    p = commands.add_parser("mark")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--phase", required=True)
    p = commands.add_parser("report")
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "mark":
            mark(args.out.resolve(), args.phase)
            return 0
        if args.command == "report":
            report(args.out.resolve())
            return 0
        config = load_config(args.config)
        if args.command == "check":
            scratch_base()
            print("Preflight passed; this does not validate GPU support or scene coverage")
            return 0
        if args.budget:
            if min(args.budget) <= 0 or len(set(args.budget)) != len(args.budget):
                raise ValueError("Budgets must be positive and distinct")
            config["budgets_mib"] = args.budget
        base = scratch_base()
        if args.out:
            out = args.out.absolute()
            if out.is_symlink() or not out.resolve().is_relative_to(base):
                raise ValueError("--out must be inside TMPDIR and must not be a symlink")
            out.mkdir(mode=0o700, parents=True, exist_ok=True)
            if any(out.iterdir()):
                raise ValueError("--out must be empty; previous measurements are never overwritten")
        else:
            out = Path(tempfile.mkdtemp(prefix="oot-memory-", dir=base))
        print(f"Experiment directory: {out}", flush=True)
        return sweep(config, out.resolve())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
