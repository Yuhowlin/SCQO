"""scqo close_qm — close all open Quantum Machine instances on OPX to release locked resources."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Injects ASqum_QM_lab Superconducting path so custom QUAM classes (quam_libs) resolve cleanly
ASQUM_PATH = Path(r"C:\Users\ASUS\Documents\GitHub\ASqum_QM_lab\Quantum-Control-Applications-QuAM\Superconducting")
if ASQUM_PATH.exists() and str(ASQUM_PATH) not in sys.path:
    sys.path.insert(0, str(ASQUM_PATH))


def main(argv: list[str] | None = None, prog: str = "scqo close_qm") -> int:
    parser = argparse.ArgumentParser(prog=prog, description="Close all active Quantum Machines on OPX to free locked hardware resources.")
    parser.parse_args(argv)

    try:
        from scqo import load_lab_config
        from scqo.cli._backends import build_session

        cfg = load_lab_config()
        sess, _ = build_session()
        
        backend = getattr(sess, "backend", None)
        if backend is not None:
            qmm = getattr(backend, "qmm", None)
            if qmm is None and hasattr(backend, "machine") and hasattr(backend.machine, "connect"):
                qmm = backend.machine.connect()
            
            if qmm is not None and hasattr(qmm, "close_all_qms"):
                qmm.close_all_qms()
                print("[SUCCESS] Closed all active Quantum Machine instances on OPX.")
                return 0

        # Fallback via direct Quam
        from quam_config import Quam
        machine = Quam.load()
        qmm = machine.connect()
        qmm.close_all_qms()
        print("[SUCCESS] Closed all active Quantum Machine instances on OPX via QUAM.")
        return 0
    except Exception as e:
        print(f"[ERROR] Failed to close Quantum Machines: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
