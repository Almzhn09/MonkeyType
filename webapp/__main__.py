"""Public hosting entry point.

  python3 -m webapp serve [--host 0.0.0.0] [--port N] [--data DIR]   playground at /, dashboard at /dashboard/
  python3 -m webapp prepare DIR                                       precompute dashboard data and baselines
  python3 -m webapp export-space DIR                                  write a Hugging Face Docker Space into DIR

The port defaults to $PORT (set by hosts such as Render), else 7860.
"""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import threading

from playground.server import DEFAULT_EPISODES, Playground


def prepare(directory, episodes):
    from .server import prepare_baselines, prepare_dashboard
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    prepare_dashboard(directory / "dashboard", episodes)
    prepare_baselines(directory / "baselines.json", episodes)


def main():
    parser = argparse.ArgumentParser(prog="python3 -m webapp", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Serve the playground and the dashboard on one port")
    serve.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7860")))
    serve.add_argument("--episodes", type=Path, default=DEFAULT_EPISODES)
    serve.add_argument("--data", type=Path, help="Folder written by `prepare`; prepared in the background if absent")
    prep = commands.add_parser("prepare", help="Precompute dashboard bundles and baseline scores")
    prep.add_argument("directory", type=Path)
    prep.add_argument("--episodes", type=Path, default=DEFAULT_EPISODES)
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
    if args.command == "prepare":
        prepare(args.directory, args.episodes)
        return 0

    from .server import load_baselines, make_server, prepare_dashboard
    data = args.data if args.data and (args.data / "dashboard" / "index.json").is_file() else None
    dashboard_dir = data / "dashboard" if data else Path(tempfile.mkdtemp(prefix="monkeytype-dashboard-"))
    baselines = load_baselines(data / "baselines.json") if data else None
    try:
        app = Playground(args.episodes, baselines=baselines)
        server = make_server(app, dashboard_dir, args.host, args.port)
    except (ValueError, OSError) as error:
        print(f"webapp: {error}", file=sys.stderr)
        return 1
    if data is None:
        # Open the port first so hosts see the service as up; the dashboard fills in shortly after.
        from dashboard.projection import write_index
        write_index(dashboard_dir)
        print("Preparing dashboard data in the background...", flush=True)
        threading.Thread(target=prepare_dashboard, args=(dashboard_dir, args.episodes), daemon=True).start()
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
