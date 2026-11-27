#!/bin/sh
# Build gitfurl's optional native decoder module (gitfurl._codecs: the cm and rolz stages) with maturin
# and put it beside the sources, so `PYTHONPATH=src` finds it. Needs cargo, rustc and maturin
# (`nix develop .#native`). Writes only native/gitfurl-codecs/target/ and
# src/gitfurl/_codecs.abi3.so (both ignored by git); cargo uses the network only when it has
# no copy of the crates pinned in Cargo.lock yet.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
crate="$root/native/gitfurl-codecs"
out="$crate/target/wheels"
rm -rf "$out"
maturin build --release --locked --manifest-path "$crate/Cargo.toml" --out "$out"
python3 - "$out" "$root/src/gitfurl" <<'PY'
import pathlib
import sys
import zipfile

wheels = sorted(pathlib.Path(sys.argv[1]).glob("*.whl"))
if len(wheels) != 1:
    sys.exit(f"expected one wheel, found {len(wheels)}")
with zipfile.ZipFile(wheels[0]) as z:
    names = [n for n in z.namelist() if n.startswith("gitfurl/_codecs") and n.endswith(".so")]
    if len(names) != 1:
        sys.exit("the wheel holds no gitfurl/_codecs module")
    target = pathlib.Path(sys.argv[2]) / pathlib.Path(names[0]).name
    target.write_bytes(z.read(names[0]))
    print(f"built {target}")
PY
