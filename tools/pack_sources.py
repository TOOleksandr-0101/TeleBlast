"""Pack pinned sources for checking hunks and compiling patched modules without executing them."""

import argparse
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
SOURCE_DIGEST = "ca2730211c0cbf11d226c46b02ff4f2e33b7406cf3864372f7da6a0404a2d21d"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "maps/sources.zip")
    args = parser.parse_args()
    paths = [args.checkout / "LICENSE"]
    for directory in ("sqlglot", "tests"):
        paths.extend((args.checkout / directory).rglob("*.py"))
    files = {p.relative_to(args.checkout).as_posix(): p.read_bytes().replace(b"\r\n", b"\n") for p in paths}
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    if digest.hexdigest() != SOURCE_DIGEST:
        parser.error(f"Python sources and LICENSE differ from sqlglot {REVISION}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w") as archive:
        for name, data in sorted(files.items()):
            entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=9)


if __name__ == "__main__":
    main()
