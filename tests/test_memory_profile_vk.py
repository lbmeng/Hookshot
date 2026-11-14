"""Compile the real Vulkan wrappers against driver spies, without a Vulkan device."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

RUN_ALL_ARGS = ["--cxx", "{cxx}", "--headers", "{vulkan_headers}"]

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="g++")
    parser.add_argument("--headers", type=Path, required=True)
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="memory-vk-", dir=os.environ.get("TMPDIR")))
    exe = work / ("fixture.exe" if os.name == "nt" else "fixture")
    includes = ["-I" + str(args.headers), "-I" + str(ROOT / "gpu/vk")]
    if os.name != "nt":
        includes.append("-I" + str(ROOT / "host/posix"))
    source = (ROOT / "gpu/vk/vk_memory_profile.cpp").read_text()
    assert '#include "vk_work.h"' in source
    spy = '''void memory_profile_boundary_spy();
#define vkFreeMemory(...) (memory_profile_boundary_spy(), ::vkx::vkFreeMemory(__VA_ARGS__))'''
    fixture = work / "memory_profile.cpp"
    fixture.write_text(source.replace('#include "vk_work.h"', spy), encoding="utf-8")
    subprocess.run([args.cxx, "-std=c++20", "-D_Static_assert=static_assert", "-DVK_NO_PROTOTYPES"] + includes +
                   [str(fixture), str(ROOT / "tests/memory_profile_vk_test.cpp"), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
