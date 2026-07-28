"""``python -m scqo.browse`` — raw-SQL power tool over the run index (datasette).

The daily GUI is ``python -m scqo.viewer`` (port 8080); this serves datasette on 8081
for ad-hoc SQL, facets and CSV export, shipping canned queries (runs by tag / by
target / failures / fitted-quantity trend) so nobody has to write JSON1 SQL by hand.

Port convention: 8001 qualibrate / 8080 viewer / 8081 this datasette browser.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .labconfig import load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8081,
                        help="lab convention: 8081 (8080 = viewer, 8001 = qualibrate)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; 0.0.0.0 serves the lab LAN")
    parser.add_argument("--data-root", help="override the lab config's data_root")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    cfg = load(args.config)
    root = Path(args.data_root) if args.data_root else cfg.data_root
    if root is None or not (root / "index.sqlite").is_file():
        raise SystemExit(
            f"no index.sqlite under {root or '(no data_root configured)'} — "
            "run a measurement first, or check ~/.scqo/config.toml"
        )

    exe = shutil.which("datasette")
    if exe is None:
        candidate = Path(sys.executable).with_name("datasette.exe")
        if not candidate.exists():
            raise SystemExit("datasette is not installed in this environment:  uv pip install datasette")
        exe = str(candidate)

    metadata = Path(__file__).with_name("browse_metadata.json")
    print(f"serving {root / 'index.sqlite'} at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    return subprocess.call([exe, str(root / "index.sqlite"), "--host", args.host,
                            "--port", str(args.port), "-m", str(metadata)])


if __name__ == "__main__":
    sys.exit(main())
