"""Run with python3 -m books_api from the project root."""

import argparse
import logging
import sys

from .server import make_server


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only API for the public Books to Scrape sandbox.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only).")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000; 0 chooses a free port).")
    arguments = parser.parse_args()
    if not 0 <= arguments.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        server = make_server(arguments.host, arguments.port)
    except OSError as error:
        print("Could not start the API: {}".format(error), file=sys.stderr)
        return 1
    print("Public Books API listening on http://{}:{}".format(arguments.host, server.server_port), flush=True)
    print("Unofficial sandbox wrapper. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the API.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())