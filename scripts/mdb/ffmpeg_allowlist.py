"""The FFmpeg allow-list: flags out, checks in (E2, session 198).

``tools/ffmpeg/allowlist.json`` is the one place that says which decoders,
demuxers, parsers, muxers and protocols MyDancingBook's LGPL PyAV FFmpeg
build may contain (research 2026-10-23 §E2; his answer: before the RC).
This module

- turns it into ``./configure`` flags (``--flags``) for the pyav-ffmpeg
  fork's build script (``build-ffmpeg-allowlist.sh``);
- parses ``ffmpeg -decoders`` / ``-demuxers`` / ``-muxers`` / ``-protocols``
  output (``--check-cli <ffmpeg>`` or ``--decoders-file`` …) and lists what a
  build has that the allow-list does not;
- asks the PyAV in THIS interpreter what its FFmpeg knows (``--check-pyav``)
  — which is what the frozen app will carry, so ``build.py``'s gate calls it.

No download, no build, no network. Pure functions take their inputs; the
``installed_*`` functions import ``av`` lazily.

    python tools/ffmpeg/ffmpeg_allowlist.py --flags
    python tools/ffmpeg/ffmpeg_allowlist.py --check-pyav
    python tools/ffmpeg/ffmpeg_allowlist.py --check-cli /path/to/ffmpeg
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWLIST_FILE = HERE / "allowlist.json"
# ``ffmpeg -decoders`` etc. print a legend, a dashed line, then one entry per
# line: flags, the name, a description. Every list has the same shape.
# filters (3.228.1): the four audio filters PyAV's AudioResampler builds its graph from.
KINDS = ("decoders", "demuxers", "parsers", "muxers", "protocols", "filters")
# ``ffmpeg`` has no ``-parsers`` listing (parsers are configure-time only), so a
# CLI check covers these four; parsers appear in the flags and nowhere else.
CLI_KINDS = ("decoders", "demuxers", "muxers", "protocols")


def load(path: Path = ALLOWLIST_FILE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def allowed(data: dict, kind: str) -> set:
    """The allowed names of one ``kind`` (decoders = video + audio)."""
    if kind == "decoders":
        return set(data["decoders"]["video"]) | set(data["decoders"]["audio"])
    return set(data.get(kind, ()))


def implied(data: dict, kind: str) -> dict:
    """``{name: why}`` of one ``kind`` that FFmpeg's configure (or libavfilter itself)
    turns on because an allowed name needs it (3.228.3, the first allow-list build):
    never a flag of ours, but present in the build, so named here instead of being
    silently allowed."""
    return {k: v for k, v in data.get("implied", {}).get(kind, {}).items() if not k.startswith("_")}


def configure_flags(data: dict) -> list:
    """The ``./configure`` arguments: everything off, then exactly the
    allow-list on. Order: stable (sorted), so two runs give one diff."""
    flags = [
        "--disable-everything",
        "--disable-programs", "--disable-doc",
        "--disable-autodetect", "--disable-network",
        "--disable-encoders", "--disable-hwaccels",
        "--disable-indevs", "--disable-outdevs",
        "--disable-libxml2", "--disable-lzma",
    ]
    singular = {"decoders": "decoder", "demuxers": "demuxer", "parsers": "parser",
                "muxers": "muxer", "protocols": "protocol", "filters": "filter"}
    for kind in KINDS:
        for name in sorted(allowed(data, kind)):
            flags.append(f"--enable-{singular[kind]}={name}")
    for lib in data.get("external_libraries", []):
        flags.append(f"--enable-{lib}" if lib.startswith("lib") else f"--enable-{lib}")
    return flags


def parse_listing(text: str) -> set:
    """Names from ``ffmpeg -decoders`` (or -demuxers / -muxers / -protocols)
    output. The protocols listing has ``Input:`` / ``Output:`` headings and
    bare names; the others have a flags column. Legend lines (``V..... =``),
    the dashed line and headings are skipped."""
    names = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.endswith(":") or set(line) <= {"-"} or " = " in line:
            continue
        parts = line.split()
        if len(parts) >= 2 and _is_flags(parts[0]):
            # "mov,mp4,m4a,3gp,3g2,mj2" is ONE demuxer whose configure name is
            # the first component (FFmpeg's .name field lists its aliases).
            names.add(parts[1].split(",")[0])
        elif len(parts) == 1 and parts[0].replace("_", "").replace(".", "").isalnum():
            names.add(parts[0])
    return names


def _is_flags(token: str) -> bool:
    """A flags column like ``V....D`` / ``D  `` / ``.E.``: letters and dots only."""
    return all(c.isalpha() or c == "." for c in token) and len(token) <= 6


def problems(found: dict, data: dict) -> list:
    """``found`` = ``{kind: set(names)}`` for the kinds a build reports.
    Every name a build has beyond the allow-list is one line; a protocol
    other than ``file`` is named as such. Empty = the build IS the allow-list
    (missing allowed names are reported separately by ``missing``)."""
    out = []
    for kind in KINDS:
        if kind not in found:
            continue
        extra = sorted(set(found[kind]) - allowed(data, kind) - set(implied(data, kind)))
        if extra:
            out.append(f"{kind}: {len(extra)} not in the allow-list: "
                       + ", ".join(extra[:12]) + (" …" if len(extra) > 12 else ""))
    return out


def missing(found: dict, data: dict) -> list:
    out = []
    for kind in KINDS:
        if kind not in found:
            continue
        lost = sorted(allowed(data, kind) - set(found[kind]))
        if lost:
            out.append(f"{kind}: allowed but not in this build: " + ", ".join(lost))
    return out


def installed_pyav() -> dict:
    """What the ``av`` of this interpreter can do: decoders, input formats
    (demuxers), output formats (muxers), and its libavcodec version."""
    import av
    from av.codec import Codec
    from av.format import ContainerFormat
    decoders = set()
    for name in av.codecs_available:
        try:
            if Codec(name, "r").is_decoder:
                decoders.add(name)
        except Exception:        # an encoder-only name, or a name PyAV lists but cannot open
            pass
    # ``formats_available`` mixes demuxer names ("mov,mp4,m4a,3gp,3g2,mj2") and muxer
    # names ("mp4"); a muxer name also finds the demuxer that lists it as an alias. So
    # each side is read from its OWN format: demuxer = the input format's first name
    # (the configure name), muxer = the output format's name (3.228.3).
    demuxers, muxers = set(), set()
    for name in av.formats_available:
        try:
            fmt = ContainerFormat(name)
        except Exception:
            continue
        if fmt.is_input:
            own = getattr(fmt, "input", None)
            demuxers.add(str(getattr(own, "name", None) or name).split(",")[0])
        if fmt.is_output:
            own = getattr(fmt, "output", None)
            muxers.add(str(getattr(own, "name", None) or name).split(",")[0])
    versions = dict(getattr(av, "library_versions", {}) or {})
    try:
        from av.filter import filters_available
        filters = set(filters_available)
    except Exception:            # a PyAV without av.filter: nothing to report
        filters = set()
    return {"decoders": decoders, "demuxers": demuxers, "muxers": muxers, "filters": filters,
            "libavcodec": tuple(versions.get("libavcodec", ())),
            "av_version": getattr(av, "__version__", "?")}


def version_problem(libavcodec: tuple, data: dict) -> str:
    """Empty when the allow-list has no decided minimum yet (it says so) or
    the bundled libavcodec is at least it."""
    floor = data.get("ffmpeg", {}).get("libavcodec_min")
    if not floor:
        return ""
    if tuple(libavcodec[:2]) < tuple(floor[:2]):
        return (f"libavcodec {'.'.join(map(str, libavcodec))} is below the allow-list's minimum "
                f"{'.'.join(map(str, floor))}")
    return ""


def cli_listing(ffmpeg: str, kind: str, run=subprocess.run) -> set:
    flag = {"decoders": "-decoders", "demuxers": "-demuxers",
            "muxers": "-muxers", "protocols": "-protocols"}[kind]
    result = run([ffmpeg, "-hide_banner", flag], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"{ffmpeg} {flag} failed (exit {result.returncode})")
    return parse_listing(result.stdout)


def report(found: dict, data: dict, title: str) -> bool:
    """Print the verdict; True = the build is within the allow-list."""
    print(title)
    for kind in KINDS:
        if kind in found:
            print(f"  {kind}: {len(found[kind])} (allow-list {len(allowed(data, kind))})")
    bad = problems(found, data)
    for kind in KINDS:
        for name, why in sorted(implied(data, kind).items()):
            if name in found.get(kind, ()):
                print(f"  implied — {kind}: {name} ({why})")
    for line in missing(found, data):
        print(f"  note — {line}")
    for line in bad:
        print(f"  PROBLEM — {line}")
    print("  OK: within the allow-list" if not bad else f"  {len(bad)} problem(s)")
    return not bad


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allowlist", type=Path, default=ALLOWLIST_FILE)
    parser.add_argument("--flags", action="store_true", help="print the ./configure flags, one per line")
    parser.add_argument("--check-pyav", action="store_true", help="check this interpreter's PyAV")
    parser.add_argument("--check-cli", metavar="FFMPEG", help="check an ffmpeg binary's listings")
    for kind in CLI_KINDS:
        parser.add_argument(f"--{kind}-file", type=Path, metavar="TXT",
                            help=f"a saved `ffmpeg -{kind}` listing to check")
    args = parser.parse_args(argv)
    data = load(args.allowlist)
    if args.flags:
        print("\n".join(configure_flags(data)))
        return 0
    found = {}
    for kind in CLI_KINDS:
        path = getattr(args, f"{kind}_file")
        if path:
            found[kind] = parse_listing(path.read_text(encoding="utf-8", errors="replace"))
    if args.check_cli:
        for kind in CLI_KINDS:
            found[kind] = cli_listing(args.check_cli, kind)
    ok = True
    if found:
        ok = report(found, data, f"FFmpeg listing(s) against {args.allowlist.name}:")
    if args.check_pyav:
        inst = installed_pyav()
        title = (f"PyAV {inst['av_version']} (libavcodec "
                 f"{'.'.join(map(str, inst['libavcodec'])) or '?'}) against {args.allowlist.name}:")
        ok = report({k: inst[k] for k in ("decoders", "demuxers", "muxers", "filters") if k in inst},
                    data, title) and ok
        vp = version_problem(inst["libavcodec"], data)
        if vp:
            print(f"  PROBLEM — {vp}")
            ok = False
    if not found and not args.check_pyav:
        parser.print_help()
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
