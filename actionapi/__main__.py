"""python -m actionapi [--port N]: start our own Action API on 127.0.0.1 (see README)."""
import argparse
import sys

from actionapi import config
from actionapi.server import make_server


def main():
    p = argparse.ArgumentParser(prog="actionapi", description=__doc__)
    p.add_argument("--port", type=int, default=config.PORT)
    args = p.parse_args()
    try:
        server = make_server(port=args.port)
    except ValueError as e:
        sys.exit(f"actionapi: {e}")
    print(f"actionapi listening on http://{config.HOST}:{args.port} "
          f"(audit log {config.AUDIT_LOG}, cooldown {config.COOLDOWN_S:g}s)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
