"""Fail the build unless FFmpeg came out LGPL-2.1-or-later only.

MDB's LGPL variant: no GPL / nonfree / version3 parts, external libraries exactly
dav1d + zlib, no encoders, no network, threads on. Reads FFmpeg's own generated
config.h / config_components.h and the library lists in its configure script, so
the answer does not depend on one string.
"""

import os
import re
import subprocess
import sys

LICENSE = "LGPL version 2.1 or later"
EXTERNAL_ALLOWED = {"libdav1d", "zlib"}
# configure's lists of external and hardware-acceleration libraries
LIST_NAMES = (
    "EXTERNAL_AUTODETECT_LIBRARY_LIST",
    "EXTERNAL_LIBRARY_LIST",
    "HWACCEL_AUTODETECT_LIBRARY_LIST",
    "HWACCEL_LIBRARY_LIST",
    "HWACCEL_LIBRARY_NONFREE_LIST",
)


def _shell_lists(configure: str) -> dict[str, list[str]]:
    """The NAME="..." word lists of configure, with $OTHER references expanded."""
    raw = {}
    for m in re.finditer(r'^([A-Z0-9_]+_LIST)="([^"]*)"', configure, re.M):
        raw[m.group(1)] = m.group(2).split()

    def expand(name: str, seen: frozenset = frozenset()) -> list[str]:
        out = []
        for word in raw.get(name, []):
            if word.startswith("$"):
                ref = word[1:]
                if ref not in seen:
                    out += expand(ref, seen | {name})
            else:
                out.append(word)
        return out

    return {name: expand(name) for name in raw}


def _defines(path: str) -> dict[str, str]:
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fp:
        for line in fp:
            m = re.match(r"#define (\w+) (.*)$", line.rstrip("\n"))
            if m:
                out[m.group(1)] = m.group(2).strip()
    return out


def problems(build_dir: str, configure_path: str) -> tuple[list[str], dict[str, str]]:
    found = []
    config = _defines(os.path.join(build_dir, "config.h"))
    components_h = os.path.join(build_dir, "config_components.h")
    if os.path.exists(components_h):
        config.update(_defines(components_h))
    with open(configure_path, encoding="utf-8", errors="replace") as fp:
        lists = _shell_lists(fp.read())

    license_ = config.get("FFMPEG_LICENSE", "").strip('"')
    if license_ != LICENSE:
        found.append(f"licence is {license_!r}, not {LICENSE!r}")
    configuration = config.get("FFMPEG_CONFIGURATION", "").strip('"')
    if not configuration:
        found.append("FFMPEG_CONFIGURATION is missing")
    for flag in ("--enable-gpl", "--enable-nonfree", "--enable-version3"):
        if flag in configuration.split():
            found.append(f"configured with {flag}")
    enable_lib = {w for w in configuration.split() if w.startswith("--enable-lib")}
    if enable_lib != {"--enable-libdav1d"}:
        found.append(f"--enable-lib* flags are {sorted(enable_lib)}, not ['--enable-libdav1d']")

    names = set()
    for list_name in LIST_NAMES:
        if list_name not in lists:
            found.append(f"configure has no {list_name}")
        names.update(lists.get(list_name, []))
    external = sorted(n for n in names if config.get("CONFIG_" + n.upper()) == "1")
    if set(external) != EXTERNAL_ALLOWED:
        found.append(f"external libraries are {external}, not {sorted(EXTERNAL_ALLOWED)}")

    encoders = sorted(k for k, v in config.items() if k.endswith("_ENCODER") and v == "1")
    if encoders:
        found.append(f"{len(encoders)} encoders enabled: {encoders[:10]}")
    if config.get("CONFIG_NETWORK") != "0":
        found.append("network is enabled")
    if config.get("HAVE_THREADS") != "1":
        found.append("threading support is off")

    summary = {
        "license": license_,
        "configuration": configuration,
        "external_libraries": " ".join(external),
        "encoders": str(len(encoders)),
        "threads": "pthreads" if config.get("HAVE_PTHREADS") == "1"
        else "w32threads" if config.get("HAVE_W32THREADS") == "1" else "none",
        "network": config.get("CONFIG_NETWORK", "?"),
    }
    return found, summary


def check_build(build_dir: str, report_path: str) -> None:
    """Called by build-ffmpeg.py after FFmpeg is built; exits non-zero on any problem."""
    configure_path = os.path.join(os.path.dirname(build_dir), "configure")
    found, summary = problems(build_dir, configure_path)
    lines = [f"{k}: {v}" for k, v in summary.items()]
    print("LGPL check:\n  " + "\n  ".join(lines), flush=True)
    if found:
        print("LGPL check FAILED:\n  " + "\n  ".join(found), flush=True)
        sys.exit(1)
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")


# the shared libraries the vendor folder may hold (FFmpeg's seven + dav1d + zlib on Windows)
OWN_LIBRARY = re.compile(
    r"^(lib)?(avcodec|avdevice|avfilter|avformat|avutil|swresample|swscale|dav1d)[-.]"
    r"|^zlib1\.dll$",
    re.I,
)
LINUX_SYSTEM = re.compile(r"^(libc|libm|libpthread|libdl|librt|libz)\.so\.\d+$|^ld-linux")


def _imports(path: str) -> list[str]:
    if sys.platform == "darwin":
        out = subprocess.run(["otool", "-L", path], check=True, capture_output=True, text=True).stdout
        return [line.split(" (")[0].strip() for line in out.splitlines()[1:] if line.strip()]
    if sys.platform.startswith("linux"):
        out = subprocess.run(["readelf", "-d", path], check=True, capture_output=True, text=True).stdout
        return re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", out)
    out = subprocess.run(["objdump", "-p", path], check=True, capture_output=True, text=True).stdout
    return re.findall(r"DLL Name: (\S+)", out)


def _system(name: str) -> bool:
    if sys.platform == "darwin":
        return name.startswith(("/usr/lib/", "/System/Library/"))
    if sys.platform.startswith("linux"):
        return bool(LINUX_SYSTEM.match(name))
    lower = name.lower()
    if lower.startswith(("api-ms-win-", "ext-ms-")):
        return True
    return os.path.exists(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", name))


def check_libraries(libraries: list[str]) -> None:
    """Every vendored library is ours, and imports only ours or the system's."""
    found = []
    for path in sorted(libraries):
        base = os.path.basename(path)
        if not OWN_LIBRARY.search(base):
            found.append(f"{base}: not an allowed library in the vendor folder")
        imports = _imports(path)
        print(f"{base} imports: {' '.join(os.path.basename(i) for i in imports)}", flush=True)
        for name in imports:
            if _system(name) or OWN_LIBRARY.search(os.path.basename(name)):
                continue
            found.append(f"{base} imports {name}")
    if found:
        print("LGPL library check FAILED:\n  " + "\n  ".join(found), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    build_dir = sys.argv[1] if len(sys.argv) > 1 else "build/ffmpeg/build"
    found, summary = problems(build_dir, os.path.join(os.path.dirname(build_dir), "configure"))
    for k, v in summary.items():
        print(f"{k}: {v}")
    for p in found:
        print("PROBLEM:", p)
    sys.exit(1 if found else 0)
