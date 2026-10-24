import argparse
import glob
import gzip
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile

import lgpl_check
from cibuildpkg import Builder, Package, fetch, log_group, run
from pkg import *

plat = platform.system()


def make_archive_deterministic(path: str) -> None:
    """Zero mtime/uid/gid in every ar member header to make the archive reproducible.

    Static archives (.a files) embed the file modification time (and uid/gid) of
    each member in a 60-byte header.  Tools like ar(1), libtool -static, and ar -M
    do not always produce deterministic timestamps even when SOURCE_DATE_EPOCH is set
    or the -D flag is requested, especially in custom CMake commands (e.g. x265's
    multi-lib merge).  Zeroing these fields in-place is the safest cross-platform fix.
    """
    MAGIC = b"!<arch>\n"
    HEADER_SIZE = 60
    with open(path, "r+b") as f:
        if f.read(len(MAGIC)) != MAGIC:
            return
        while True:
            pos = f.tell()
            header = f.read(HEADER_SIZE)
            if len(header) < HEADER_SIZE:
                break
            if header[58:60] != b"`\n":
                break  # not a valid ar member header
            try:
                size = int(header[48:58].decode().strip())
            except ValueError:
                break
            # ar member header layout (60 bytes):
            #   [0:16]  name          16 bytes
            #   [16:28] mtime         12 bytes  ← zero this
            #   [28:34] uid            6 bytes  ← zero this
            #   [34:40] gid            6 bytes  ← zero this
            #   [40:48] mode           8 bytes
            #   [48:58] size          10 bytes
            #   [58:60] end magic      2 bytes  (`\n)
            patched = (
                header[:16]
                + b"0           "  # mtime: 12 bytes
                + b"0     "        # uid:    6 bytes
                + b"0     "        # gid:    6 bytes
                + header[40:]      # mode + size + end magic unchanged
            )
            f.seek(pos)
            f.write(patched)
            # advance past member data; ar pads to even offset
            f.seek(size + (size % 2), 1)


def make_tarball_name() -> str:
    machine = platform.machine().lower()
    isArm64 = machine in {"arm64", "aarch64"}

    if sys.platform.startswith("win"):
        return "ffmpeg-windows-aarch64" if isArm64 else "ffmpeg-windows-x86_64"

    elif sys.platform.startswith("darwin"):
        return "ffmpeg-macos-arm64" if isArm64 else "ffmpeg-macos-x86_64"

    elif sys.platform.startswith("linux"):
        prefix = "ffmpeg-musllinux-" if is_musllinux else "ffmpeg-manylinux-"
        # Inside the manylinux/musllinux container AUDITWHEEL_ARCH is the
        # canonical wheel arch. uname (platform.machine) is unreliable when
        # cross-building 32-bit ARM under an aarch64 kernel, where it reports
        # "armv8l" rather than "armv7l".
        return prefix + os.environ.get("AUDITWHEEL_ARCH", machine)

    else:
        return "ffmpeg-unknown"

def require_tools(names: list[str]) -> None:
    """MDB lgpl2: the build never downloads its tools (no `pip install cmake meson ninja`).
    They come from the system (Ubuntu 26.04 here: apt nasm 3.01-1, meson 1.10.1-1ubuntu2,
    ninja-build, pkgconf); a missing one stops the build here, loudly."""
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        sys.exit("build-ffmpeg: missing build tools: " + ", ".join(missing)
                 + " (install them from the system's package manager; this build never downloads them)")


def read_configure_flags(path: str | None) -> list[str]:
    """MDB lgpl2: the allow-list's ./configure flags, one per line (MyDancingBook
    tools/ffmpeg/ffmpeg_allowlist.py --flags). They go FIRST (they start with
    --disable-everything), the LGPL flags below follow."""
    if not path:
        return []
    with open(path, encoding="utf-8") as fp:
        flags = [line.strip() for line in fp if line.strip() and not line.startswith("#")]
    bad = [f for f in flags if not f.startswith("--") or " " in f]
    if bad:
        sys.exit(f"build-ffmpeg: not a configure flag in {path}: {bad[:3]}")
    if not flags or flags[0] != "--disable-everything":
        sys.exit(f"build-ffmpeg: {path} must start with --disable-everything")
    return flags


def main():
    parser = argparse.ArgumentParser("build-ffmpeg")
    parser.add_argument("destination")
    parser.add_argument("--configure-flags", metavar="FILE",
                        help="extra ./configure flags, one per line, put first (the allow-list)")

    args = parser.parse_args()
    dest_dir = os.path.abspath(args.destination)
    allowlist_flags = read_configure_flags(args.configure_flags)

    machine = platform.machine().lower()
    is_arm32 = machine in {"armv7l", "armv8l", "arm"}
    is_arm = machine in {"arm64", "aarch64"} or is_arm32
    is_riscv = machine in {"riscv64"}

    output_dir = os.path.abspath("output")
    if plat == "Linux" and os.environ.get("CIBUILDWHEEL") == "1":
        output_dir = "/output"

    output_tarball = os.path.join(output_dir, make_tarball_name() + ".tar.gz")
    if os.path.exists(output_tarball):
        return

    builder = Builder(dest_dir=dest_dir)
    builder.create_directories()

    # install packages
    available_tools = set()
    if plat == "Windows":
        if not is_arm:
            available_tools.update(["nasm"])

        # print tool locations
        print("PATH", os.environ["PATH"])
        if is_arm:
            tools = ["clang", "clang++", "curl", "ld", "pkg-config"]
        else:
            tools = ["gcc", "g++", "curl", "ld", "nasm", "pkg-config"]
        for tool in tools:
            run(["where", tool])

    elif shutil.which("nasm"):
        # MDB lgpl2: a system nasm (Ubuntu 26.04: nasm 3.01-1) is used, never the tarball
        available_tools.add("nasm")

    # MDB lgpl2: no `pip install cmake meson ninja` (unpinned PyPI) — the system's tools
    needed = ["meson", "ninja", "pkg-config" if shutil.which("pkg-config") else "pkgconf", "make", "patch"]
    if plat != "Darwin" and machine in {"x86_64", "amd64", "i686", "i386"}:
        needed.append("nasm")
    if plat != "Windows":
        require_tools(needed)

    # MDB LGPL-only variant: no GPL / version3 libraries, no encoders, no network, no devices.
    ffmpeg_package.build_arguments = allowlist_flags + [
        "--disable-programs",
        "--disable-doc",
        "--disable-autodetect",
        "--disable-network",
        "--disable-encoders",
        "--disable-indevs",
        "--disable-outdevs",
        "--disable-libxml2",
        "--disable-lzma",
        "--disable-iconv",  # glibc's iconv is found even with --disable-autodetect
        "--enable-libdav1d",
        "--enable-zlib",
        "--disable-filter=gfxcapture",
        # threading made explicit (w32threads are only autodetected)
        "--enable-w32threads" if plat == "Windows" else "--enable-pthreads",
    ]
    if plat == "Windows" and not is_arm:
        # link the GCC runtime statically, so no libgcc_s_seh-1.dll ships
        os.environ["LDFLAGS"] = ("-static-libgcc " + os.environ.get("LDFLAGS", "")).strip()
        # and winpthreads (avutil's clock functions), so no libwinpthread-1.dll ships
        ffmpeg_package.build_arguments.append("--extra-libs=-Wl,-Bstatic -lwinpthread -Wl,-Bdynamic")
    if plat == "Darwin":
        ffmpeg_package.build_arguments.append("--extra-ldflags=-Wl,-ld_classic")
    if plat == "Windows" and is_arm:
        ffmpeg_package.build_arguments.extend(["--cc=clang", "--cxx=clang++", "--arch=aarch64"])

    packages = []
    if plat != "Darwin" and "nasm" not in available_tools and machine in {"x86_64", "amd64", "i686", "i386"}:
        packages.append(nasm_package)
    packages += [p for p in codec_group if p.name == "dav1d"]
    packages += [ffmpeg_package]

    for package in packages:
        builder.build(package, for_builder=package.name == "nasm")

    # stop here unless FFmpeg came out LGPL-only (scripts/lgpl_check.py)
    lgpl_check.check_build(
        os.path.join(builder.build_dir, "ffmpeg", ffmpeg_package.build_dir),
        os.path.join(output_dir, make_tarball_name() + ".configure.txt"),
    )

    if plat == "Windows":
        # fix .lib files being installed in the wrong directory
        for name in (
            "avcodec",
            "avdevice",
            "avfilter",
            "avformat",
            "avutil",
            "postproc",
            "swresample",
            "swscale",
        ):
            if os.path.exists(os.path.join(dest_dir, "bin", name + ".lib")):
                shutil.move(
                    os.path.join(dest_dir, "bin", name + ".lib"),
                    os.path.join(dest_dir, "lib"),
                )

        # copy some libraries provided by mingw
        is_arm64 = machine in {"arm64", "aarch64"}
        compiler = "clang" if is_arm64 else "gcc"
        mingw_bindir = os.path.dirname(
            subprocess.run(["where", compiler], check=True, stdout=subprocess.PIPE)
            .stdout.decode()
            .splitlines()[0]
            .strip()
        )
        if is_arm64:
            # CLANGARM64 uses clang/libc++ instead of gcc/libstdc++
            dll_names = (
                "libc++.dll",
                "libiconv-2.dll",
                "libunwind.dll",
                "libwinpthread-1.dll",
                "zlib1.dll",
            )
        else:
            dll_names = (
                "zlib1.dll",
            )
        for name in dll_names:
            shutil.copy(os.path.join(mingw_bindir, name), os.path.join(dest_dir, "bin"))

    # find libraries
    if plat == "Darwin":
        libraries = glob.glob(os.path.join(dest_dir, "lib", "*.dylib"))
    elif plat == "Linux":
        libraries = glob.glob(os.path.join(dest_dir, "lib", "*.so"))
    elif plat == "Windows":
        libraries = glob.glob(os.path.join(dest_dir, "bin", "*.dll"))

    lgpl_check.check_libraries(libraries)

    if plat == "Darwin":
        run(["strip", "-x", "-S"] + libraries)
    else:
        run(["strip", "-s"] + libraries)

    for lib in glob.glob(os.path.join(dest_dir, "lib", "*.a")):
        make_archive_deterministic(lib)

    # build output tarball (reproducible: fixed timestamps, sorted entries)
    os.makedirs(output_dir, exist_ok=True)
    subdirs = ["include", "lib"]
    if plat == "Windows":
        subdirs.append("bin")
    with gzip.GzipFile(output_tarball, "wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as tar:
            for subdir in subdirs:
                subdir_path = os.path.join(dest_dir, subdir)
                if not os.path.exists(subdir_path):
                    continue
                for root, dirs, files in os.walk(subdir_path):
                    dirs.sort()
                    for name in sorted(files):
                        if subdir == "bin" and not name.endswith(".dll"):
                            continue
                        filepath = os.path.join(root, name)
                        arcname = os.path.relpath(filepath, dest_dir)
                        info = tar.gettarinfo(filepath, arcname=arcname)
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        if info.issym() or info.islnk():
                            tar.addfile(info)
                        else:
                            with open(filepath, "rb") as f:
                                tar.addfile(info, f)


if __name__ == "__main__":
    main()
