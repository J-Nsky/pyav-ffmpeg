"""Write SOURCE.txt for a release: what was built, from which sources, and how.

Usage: python scripts/source_txt.py <artifacts dir> <source dir> <repo url> <commit> <ref>
"""

import glob
import hashlib
import os
import sys

from pkg import all_packages, ffmpeg_package


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    artifacts, source, repo, commit, ref = sys.argv[1:6]
    out = [
        f"FFmpeg {ffmpeg_package.source_url.rsplit('/', 1)[-1][len('ffmpeg-'):-len('.tar.xz')]} built "
        "LGPL-2.1-or-later only, for PyAV (MyDancingBook)",
        "=" * 70,
        "",
        "These binaries are FFmpeg's shared libraries (avcodec, avdevice, avfilter, avformat,",
        "avutil, swresample, swscale) and dav1d's, built from the UNMODIFIED sources below. No GPL,",
        "nonfree or version3 part is enabled; no encoders, no network protocols, no capture devices.",
        "",
        "Build recipe",
        f"  repository : {repo}",
        f"  commit     : {commit} ({ref})",
        "  script     : scripts/build-ffmpeg.py (configure flags), scripts/lgpl_check.py (the gate)",
        "  workflow   : .github/workflows/build-ffmpeg.yml",
        "",
        "Sources in this release (unmodified upstream tarballs)",
    ]
    for package in sorted(all_packages, key=lambda p: p.name):
        name = package.source_filename or package.source_url.split("/")[-1]
        path = os.path.join(source, name)
        if not os.path.exists(path):
            continue
        digest = sha256(path)
        if digest != package.sha256:
            sys.exit(f"{name}: sha256 {digest} does not match {package.sha256}")
        role = {
            "ffmpeg": "LGPL-2.1-or-later (COPYING.LGPLv2.1 inside)",
            "dav1d": "BSD-2-Clause (COPYING inside)",
            "nasm": "build tool only (assembles dav1d / FFmpeg on x86_64); not part of the binaries",
        }.get(package.name, "")
        out += [f"  {name}", f"    from   : {package.source_url}", f"    sha256 : {digest}", f"    note   : {role}"]
    out += [
        "",
        "Build tools (not part of the binaries, not linked; from the system's signed archive):",
        "  Linux x86_64 (lgpl2): Ubuntu 26.04 nasm 3.01-1, meson 1.10.1-1ubuntu2, ninja-build, pkgconf,",
        "  gcc 15; no tool is downloaded by the recipe.",
        "",
        "zlib: the system's on macOS (/usr/lib/libz.1.dylib) and Linux (libz.so.1, not bundled",
        "into the PyAV wheel); on Windows MSYS2's mingw-w64-x86_64-zlib (zlib1.dll). zlib licence.",
        "",
        "Windows only: MinGW-w64's winpthreads (MIT licence) and the GCC runtime (GPL-3.0 with",
        "the GCC Runtime Library Exception) are linked statically into the libraries; their",
        "sources: MSYS2's mingw-w64-x86_64-winpthreads and mingw-w64-x86_64-gcc packages.",
        "",
        "Configuration per platform (FFmpeg's own FFMPEG_CONFIGURATION and licence string)",
    ]
    for report in sorted(glob.glob(os.path.join(artifacts, "*.configure.txt"))):
        platform = os.path.basename(report)[: -len(".configure.txt")]
        out += ["", f"  [{platform}]"]
        with open(report, encoding="utf-8") as fp:
            out += ["    " + line.rstrip() for line in fp if line.strip()]
    out += [
        "",
        "Rebuilding",
        "  1. Check out the commit above; put the source tarballs into source/.",
        "  2. macOS arm64 : MACOSX_DEPLOYMENT_TARGET=14.0 ARCHFLAGS='-arch arm64'",
        "                   python scripts/build-ffmpeg.py /tmp/vendor",
        "     Linux x86_64: inside the manylinux x86_64 container cibuildwheel uses (see the workflow)",
        "                   python scripts/build-ffmpeg.py /tmp/vendor",
        "     Windows x64 : MSYS2 MINGW64 with gcc, nasm, pkgconf, zlib;",
        "                   python scripts/build-ffmpeg.py C:\\cibw\\vendor",
        "  3. The ffmpeg-<platform>.tar.gz is written to output/.",
        "",
        "Replacing the libraries in an application that ships them: build the same FFmpeg version",
        "(the same major library versions) with your changes and put your shared libraries in place",
        "of the shipped ones, keeping the file names as shipped.",
    ]
    print("\n".join(out))


if __name__ == "__main__":
    main()
