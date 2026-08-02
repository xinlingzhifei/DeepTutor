from __future__ import annotations

import os
from pathlib import Path
import time


def blocking_pdf_worker(path, connection, *_limits) -> None:
    Path(f"{path}.pid").write_text(str(os.getpid()), encoding="ascii")
    try:
        while True:
            time.sleep(0.05)
    finally:
        connection.close()


def silent_pdf_worker(_path, connection, *_limits) -> None:
    connection.close()
