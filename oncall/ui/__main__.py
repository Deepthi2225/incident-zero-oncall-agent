"""python -m oncall.ui [--port N]: the read-only live dashboard on http://127.0.0.1:8090."""
import argparse

from oncall import config
from oncall.ui.server import make_server


def main():
    p = argparse.ArgumentParser(prog="oncall.ui", description=__doc__)
    p.add_argument("--port", type=int, default=config.UI_PORT)
    args = p.parse_args()
    server = make_server(port=args.port)
    print(f"live dashboard on http://127.0.0.1:{args.port} (read-only; approvals stay in the terminal)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
