"""Public hosting entry point.

  python3 -m webapp serve [--host 0.0.0.0] [--port 7860]   playground at /, dashboard at /dashboard/
  python3 -m webapp export-space DIR                       write a Hugging Face Docker Space into DIR
"""

import argparse
import os
from pathlib import Path
import sys
import tempfile

from playground.server import DEFAULT_EPISODES, Playground


def main():
    parser = argparse.ArgumentParser(prog="python3 -m webapp", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the playground and the dashboard on one port")
    serve.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7860")))
    serve.add_argument("--episodes", type=Path, default=DEFAULT_EPISODES)
    export = commands.add_parser("export-space", help="Write a Hugging Face Docker Space folder")
    export.add_argument("directory", type=Path)
    args = parser.parse_args()

    if args.command == "export-space":
        from .space import export_space
        try:
            target = export_space(args.directory)
        except ValueError as error:
            print(f"webapp: {error}", file=sys.stderr)
            return 1
        print(f"Space files written to {target}. Upload them to your Space (see docs/HOSTING.md).")
        return 0

    from .server import make_server, prepare_dashboard
    dashboard_dir = Path(tempfile.mkdtemp(prefix="monkeytype-dashboard-"))
    print("Preparing dashboard data (runs the demo and the dev split once)...", flush=True)
    prepare_dashboard(dashboard_dir, args.episodes)
    try:
        app = Playground(args.episodes)
        server = make_server(app, dashboard_dir, args.host, args.port)
    except (ValueError, OSError) as error:
        print(f"webapp: {error}", file=sys.stderr)
        return 1
    shown = "localhost" if args.host in ("0.0.0.0", "127.0.0.1") else args.host
    print(f"Playground: http://{shown}:{server.server_port}/   Dashboard: http://{shown}:{server.server_port}/dashboard/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
