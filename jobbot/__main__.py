"""`jobbot` CLI entry: starts the local web app."""
from __future__ import annotations

import sys


def _setup_logging() -> None:
    """Send jobbot's own logs to data/jobbot.log and the terminal.

    Without this the root logger has no handlers, so every log.info is dropped and warnings reach only the
    terminal that started the server — which is why a failed application could only be reconstructed from
    the database afterwards. The thread name is in the format because this app runs one browser per thread
    and the owner matters (see jobbot/apply/runner.py).
    """
    import logging
    from logging.handlers import RotatingFileHandler
    from jobbot import config
    root = logging.getLogger()
    if any(getattr(h, "_jobbot", False) for h in root.handlers):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    handlers = [RotatingFileHandler(config.DATA_DIR / "jobbot.log", maxBytes=5_000_000, backupCount=3,
                                    encoding="utf-8"),
                logging.StreamHandler()]
    for h in handlers:
        h.setFormatter(fmt)
        h._jobbot = True
        root.addHandler(h)
    # Root stays at WARNING so httpx's one-line-per-request INFO chatter does not fill the file.
    root.setLevel(logging.WARNING)
    logging.getLogger("jobbot").setLevel(logging.INFO)


def main() -> None:
    _setup_logging()
    import logging
    from jobbot import config, db
    moved = config.migrate_secrets_from_facts()
    if moved:
        logging.getLogger("jobbot").warning(
            "moved %s out of facts.yaml into the keychain — that file is read into LLM prompts, "
            "so credentials must not live there", ", ".join(moved))
    db.init()
    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        # jobbot discover "Forward Deployed Engineer"
        from jobbot.discovery import run_search
        q = " ".join(sys.argv[2:]) or "Forward Deployed Engineer"
        sid = run_search(q)
        print(f"search {sid} done")
        return
    import uvicorn
    uvicorn.run("jobbot.web.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
