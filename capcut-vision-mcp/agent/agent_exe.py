from __future__ import annotations

import logging
import pathlib
import sys

import agent


BASE_DIR = (
    pathlib.Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else pathlib.Path(__file__).resolve().parent
)

# The source agent normally resolves these beside agent.py. A one-file
# PyInstaller build extracts modules into a temporary directory, so redirect
# editable configuration and logs to the folder containing the EXE.
agent.CONFIG_PATH = BASE_DIR / "config.json"
agent.LOG_PATH = BASE_DIR / "agent.log"

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass

logging.basicConfig(
    filename=agent.LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


if __name__ == "__main__":
    agent.main()
