"""Start the audit playground: python3 -m playground"""

import argparse
from pathlib import Path
import sys
import threading
import webbrowser

from .server import DEFAULT_EPISODES, Playground, make_server


def main():
    parser = argparse.ArgumentParser(prog="python3 -m playground", description=__doc__)
    parser.add_argument("--episodes", type=Path, default=DEFAULT_EPISODES,
                        help="Dataset root with public/ and private/ (default: the repository's episodes/)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Address to listen on (use 0.0.0.0 inside Docker)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    args = parser.parse_args()

    try:
        app = Playground(args.episodes)
        server = make_server(app, args.host, args.port)
    except (ValueError, OSError) as error:
        print(f"playground: {error}", file=sys.stderr)
        return 1
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{server.server_port}/"
    print(f"Audit playground with {len(app.episodes)} companies: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
