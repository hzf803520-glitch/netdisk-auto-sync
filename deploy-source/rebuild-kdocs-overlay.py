from __future__ import annotations

import base64
import binascii
import hashlib
import io
import sys
import tarfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: rebuild-kdocs-overlay.py <parts-dir> <output.tar.gz>")

    parts_dir = Path(sys.argv[1])
    output = Path(sys.argv[2])
    parts = [parts_dir / f"kdocs-override.part{index:02d}" for index in range(4)]

    missing = [str(path) for path in parts if not path.is_file()]
    if missing:
        raise SystemExit(f"missing KDocs overlay parts: {', '.join(missing)}")

    # part00 没有被污染，用它的文件大小作为每个分片的有效前缀长度。
    valid_prefix_size = parts[0].stat().st_size
    if valid_prefix_size <= 0:
        raise SystemExit("KDocs overlay part00 is empty")

    clean_parts: list[bytes] = []
    for path in parts:
        raw = path.read_bytes()
        if len(raw) < valid_prefix_size:
            raise SystemExit(
                f"{path.name} is too short: {len(raw)} bytes, expected at least {valid_prefix_size}"
            )
        prefix = raw[:valid_prefix_size]
        clean = b"".join(prefix.split())
        clean_parts.append(clean)
        print(
            f"{path.name}: stored={len(raw)} valid-prefix={valid_prefix_size} "
            f"base64-chars={len(clean)} sha256={hashlib.sha256(clean).hexdigest()}"
        )

    encoded = b"".join(clean_parts)
    print(f"combined base64 chars={len(encoded)} sha256={hashlib.sha256(encoded).hexdigest()}")

    try:
        archive = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise SystemExit(f"KDocs overlay base64 is invalid after cleanup: {exc}") from exc

    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = tar.getmembers()
            if not members:
                raise SystemExit("KDocs overlay archive is empty")
            print(
                f"archive bytes={len(archive)} sha256={hashlib.sha256(archive).hexdigest()} "
                f"members={len(members)}"
            )
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SystemExit(f"KDocs overlay archive is damaged: {exc}") from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive)


if __name__ == "__main__":
    main()
