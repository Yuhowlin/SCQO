"""``python -m scqo.viewer`` — serve the run-viewer (lab convention: port 8080).

Zero-config: reads data_root/device from the lab config; state files are per SETUP,
found via each device's cooldown registry (``scqo.datastore.setup_state_path``). Ports:
8001 qualibrate · 8080 THIS viewer · 8081 datasette (``python -m scqo.browse``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..labconfig import load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; 0.0.0.0 serves the lab LAN (zero-install browsing from any laptop)")
    parser.add_argument("--data-root", help="override the lab config's data_root")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    cfg = load(args.config)
    root = Path(args.data_root) if args.data_root else cfg.data_root
    if root is None:
        raise SystemExit("no data_root configured — set it in ~/.scqo/config.toml or pass --data-root")
    if not (root / "index.sqlite").is_file():
        if not root.is_dir():
            # a mistyped path must fail loudly, never silently serve an empty lab
            raise SystemExit(f"data_root does not exist: {root} — check ~/.scqo/config.toml or --data-root")
        # a fresh lab: the folder exists but nothing was measured yet — start empty
        from ..datastore import DataStore

        DataStore(root)
        print(f"new data_root: initialized an empty index at {root / 'index.sqlite'}")

    try:
        import uvicorn

        from .app import create_app
    except ModuleNotFoundError as err:
        repo = Path(__file__).resolve().parents[2]
        raise SystemExit(
            f"missing package: {err.name}\n"
            "Wrong venv? The view env already has the viewer — activate it first\n"
            "(Windows: ..\\.venv-view\\Scripts\\Activate.ps1 next to the repos; macOS/Linux: source ../.venv-view/bin/activate).\n"
            f'Or install the extras into THIS env:  uv pip install -e "{repo}[viewer]"'
        )

    app = create_app(root, device_name=cfg.device or "device")
    shown = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    print(f"SCQO run viewer: http://{shown}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
