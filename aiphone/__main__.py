"""Entry point: ``python -m aiphone`` starts the web interface, the AudioSocket
bridge and the scheduler in one process."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from .db import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="aiphone")
    parser.add_argument("--host", default=os.environ.get("AIPHONE_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--write-asterisk-config", action="store_true",
                        help="Generate the Asterisk configuration files and exit")
    parser.add_argument("--reset-password", action="store_true",
                        help="Remove the admin password so the next web visit asks for a new one")
    parser.add_argument("--log-level", default=os.environ.get("AIPHONE_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database()

    if args.reset_password:
        db.set("admin_password_hash", "")
        print("Admin password removed. Open the web interface to set a new one.")
        return
    if args.write_asterisk_config:
        from . import asterisk
        for path in asterisk.write_configs(db):
            print(f"wrote {path}")
        return

    port = args.port or int(db.bootstrap().get("web_port", 8080))
    asyncio.run(serve(db, args.host, port))


async def serve(db: Database, host: str, port: int) -> None:
    from . import asterisk
    from .manager import CallManager
    from .scheduler import Scheduler
    from .web.app import create_app

    manager = CallManager(db)
    await manager.start()
    scheduler = Scheduler(db, manager)
    scheduler.start()
    try:
        asterisk.write_configs(db)
    except OSError as exc:
        logging.warning("Could not write Asterisk configuration: %s", exc)
    app = create_app(db, manager, scheduler)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            proxy_headers=False)
    server = uvicorn.Server(config)
    logging.info("Web interface on http://%s:%s", host, port)
    try:
        await server.serve()
    finally:
        await manager.stop()


if __name__ == "__main__":
    sys.exit(main())
