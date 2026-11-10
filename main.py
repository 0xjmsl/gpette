"""gpette: start the server. Settings come from gpette.toml (see gpette.example.toml)."""
import logging
import sys

from config import Config
from utils.db_logger import setup_db_logging
from server.server import create_server, run_server


def main():
    # The root logger already has handlers by now (setup_db_logging's, and the
    # console one an import-time logging.info() created), so logging.basicConfig()
    # would be a no-op: root stayed at WARNING and every INFO line was lost.
    # Set the level and the handlers directly. A new console handler writes to
    # the raw stderr: the DB handler already stores each record.
    setup_db_logging("gpette")
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(Config.LOG_LEVEL).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [logging.FileHandler(Config.LOG_FILE, encoding="utf-8")]      # data/logs/app.log
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        handlers.append(logging.StreamHandler(sys.__stderr__))
    for handler in handlers:
        handler.setFormatter(fmt)
        root.addHandler(handler)
    logging.info("Starting gpette...")
    server = create_server(Config)
    logging.info(f"Listening on http://{Config.API_HOST}:{Config.API_PORT}")
    run_server(server)


if __name__ == "__main__":
    main()
