"""Serve the installable mobile PWA and disk-backed APIs through one HTTP port."""

import os
import signal
import tempfile
import threading
from pathlib import Path

from transfer_server import TransferService


def main():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    root = Path(os.environ.get("PDF_TEMP_ROOT", str(Path(tempfile.gettempdir()) / "pdf-mobile-splitter")))
    service = TransferService(root, host=os.environ.get("PDF_TRANSFER_HOST", "127.0.0.1"),
                              port=int(os.environ.get("PORT", "8765")),
                              ttl_seconds=int(os.environ.get("PDF_RETENTION_SECONDS", "1800")))
    print(f"PDF mobile server listening on port {service.port}", flush=True)
    try:
        stop.wait()
    finally:
        service.close()


if __name__ == "__main__":
    main()
