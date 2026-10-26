"""Prove in the CI log that the FFmpeg just built IS MyDancingBook's allow-list build.

MDB lgpl2, Windows (RC item G4, 2026-10-26). Reads what FFmpeg's configure turned on
(<build dir>/config_components.h) and the INSTALLED libavcodec version headers, prints
every enabled decoder / demuxer / muxer / parser / protocol / filter with counts, and
checks them with MyDancingBook's own checker (scripts/mdb/ffmpeg_allowlist.py, a copy of
the app's tools/ffmpeg/ffmpeg_allowlist.py + allowlist.json; scripts/mdb/inputs.sha256
pins both copies). Exit 1 on any name outside the allow-list, any encoder / hwaccel /
device, a `never` name, or a libavcodec below the allow-list's minimum.

    python scripts/mdb/config_report.py <ffmpeg build dir> <vendor prefix>
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffmpeg_allowlist  # noqa: E402  (the app's checker, vendored next to this file)

# config_components.h suffix -> the allow-list's kind
KINDS = {"DECODER": "decoders", "DEMUXER": "demuxers", "MUXER": "muxers",
         "PARSER": "parsers", "PROTOCOL": "protocols", "FILTER": "filters"}
# kinds that must be empty in this build
MUST_BE_EMPTY = ("ENCODER", "HWACCEL", "INDEV", "OUTDEV")
ALWAYS_BUILT_FILTERS = {"abuffer", "abuffersink", "buffer", "buffersink"}


def enabled(components_h: str) -> dict:
    out: dict = {}
    pattern = re.compile(r"^#define CONFIG_([A-Z0-9_]+)_(" + "|".join(list(KINDS) + list(MUST_BE_EMPTY) + ["BSF"]) + r") 1$")
    with open(components_h, encoding="utf-8") as fp:
        for line in fp:
            m = pattern.match(line.strip())
            if m:
                out.setdefault(m.group(2), set()).add(m.group(1).lower())
    return out


def header_int(path: str, name: str) -> int:
    with open(path, encoding="utf-8") as fp:
        m = re.search(rf"^#define {name}\s+(\d+)", fp.read(), re.M)
    if not m:
        sys.exit(f"config_report: {name} not found in {path}")
    return int(m.group(1))


def main() -> int:
    build_dir, prefix = sys.argv[1], sys.argv[2]
    data = ffmpeg_allowlist.load()
    found = enabled(os.path.join(build_dir, "config_components.h"))
    bad = []

    inc = os.path.join(prefix, "include", "libavcodec")
    lavc = (header_int(os.path.join(inc, "version_major.h"), "LIBAVCODEC_VERSION_MAJOR"),
            header_int(os.path.join(inc, "version.h"), "LIBAVCODEC_VERSION_MINOR"),
            header_int(os.path.join(inc, "version.h"), "LIBAVCODEC_VERSION_MICRO"))
    print(f"[mdb] libavcodec {'.'.join(map(str, lavc))} (installed headers)")
    vp = ffmpeg_allowlist.version_problem(lavc, data)
    if vp:
        bad.append(vp)

    for suffix, kind in KINDS.items():
        names = sorted(found.get(suffix, ()))
        print(f"[mdb] {kind} enabled: {len(names)} (allow-list {len(ffmpeg_allowlist.allowed(data, kind))}): "
              + " ".join(names))
    print(f"[mdb] bitstream filters (chosen by configure for the allowed codecs): "
          + " ".join(sorted(found.get("BSF", ()))))
    for suffix in MUST_BE_EMPTY:
        names = sorted(found.get(suffix, ()))
        print(f"[mdb] {suffix.lower()}s enabled: {len(names)}")
        if names:
            bad.append(f"{suffix.lower()}s present: {names}")

    # abuffer / abuffersink (and buffer / buffersink) are always built: libavfilter/allfilters.c
    # declares them "extern  const" (two blanks), outside the sed that makes configure's
    # FILTER_LIST, so config_components.h has no CONFIG_ line for them and --enable-filter=abuffer
    # matches nothing. PyAV still lists them (the Linux wheel: 6 filters). A note, not a problem.
    always = sorted(ALWAYS_BUILT_FILTERS & ffmpeg_allowlist.allowed(data, "filters"))
    print("[mdb] filters always built by libavfilter (no CONFIG_ line): " + " ".join(always))

    by_kind = {kind: found.get(suffix, set()) for suffix, kind in KINDS.items()}
    if not ffmpeg_allowlist.report(by_kind, data, "[mdb] this build against allowlist.json:"):
        bad.append("components outside the allow-list (see PROBLEM lines)")
    never = data.get("never", {})
    hits = [f"{kind}: {n}" for kind in ("decoders", "demuxers", "protocols")
            for n in never.get(kind, ()) if n in by_kind.get(kind, ())]
    print("[mdb] never-list names present: " + (", ".join(hits) if hits else "none"))
    if hits:
        bad.append(f"never-list names present: {hits}")

    if bad:
        print("[mdb] ALLOW-LIST CHECK FAILED:\n  " + "\n  ".join(bad))
        return 1
    print("[mdb] ALLOW-LIST CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
