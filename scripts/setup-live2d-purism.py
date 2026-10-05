"""Prepare the optional runtime from operator-provided local bundles, without network."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", type=Path, help="Directory with pixi.min.js (6.5.10), purismcore.js (v1.1.0), cubism4.min.js (0.4.0)")
    args = parser.parse_args()
    names = ("pixi.min.js", "purismcore.js", "cubism4.min.js")
    files = {name: args.bundle_dir / name for name in names}
    expected = {
        "pixi.min.js": "403f2f2ee8145fa17f60c5c89403056efe2680e5096ec2762036486914ed19c5",
        "purismcore.js": "3eec0b1e6cd20bab0773744228aac21f4c882dbef708c28379ba6315a11b15f4",
        "cubism4.min.js": "af1267e6d52759b245766c578d905bfa025b532d5c3cc727c370957c4409e21b",
    }
    for name, path in files.items():
        if not path.is_file() or not path.stat().st_size:
            parser.error(f"Missing local bundle: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]:
            parser.error(f"Unverified bundle/version: {name}; this adapter requires the documented build")
    if b"6.5.10" not in files["pixi.min.js"].read_bytes():
        parser.error("This adapter requires PixiJS 6.5.10")
    root = Path(__file__).resolve().parents[1]
    target = root / "src/character_memory/web/vendor/live2d"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(files["pixi.min.js"], target / "pixi.min.js")
    shutil.copyfile(files["cubism4.min.js"], target / "cubism.js")
    bridge = root / "src/character_memory/web/live2d_purism_bridge.js"
    (target / "live2dcubismcore.min.js").write_bytes(files["purismcore.js"].read_bytes() + b"\n;\n" + bridge.read_bytes())
    report = {"runtime": "Purism 1.1.0 / Pixi 6.5.10 / pixi-live2d-display 0.4.0", "network_used": False,
              "inputs": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}}
    (target / "runtime-provenance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()

