"""MonkeyType reviewer dashboard.

  python3 -m dashboard demo                     run the demo, project it and open the dashboard
  python3 -m dashboard prepare --demo           only (re)build the demo bundle
  python3 -m dashboard prepare --run DIR --episodes ROOT --split dev --audience reviewer --name my-run
  python3 -m dashboard prepare --release DIR --audience reviewer --name release
  python3 -m dashboard serve                    open whatever has been prepared

Bundles are written to .audit-sim/dashboard/ (ignored by git) unless --out is given.
"""

import argparse
from pathlib import Path
import sys
import threading
import webbrowser

from .projection import ROOT, PrepareError, prepare_demo, prepare_release, prepare_run, write_index
from .server import make_server

DEFAULT_OUT = ROOT / ".audit-sim" / "dashboard"


def serve(out, port, open_browser):
    out.mkdir(parents=True, exist_ok=True)
    if not (out / "index.json").exists():
        write_index(out)
    try:
        server = make_server(out, port)
    except OSError as error:
        print(f"dashboard: cannot listen on 127.0.0.1:{port}: {error}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"MonkeyType dashboard: {url}  (serving {out}; Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main():
    parser = argparse.ArgumentParser(prog="python3 -m dashboard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Sanitized bundle directory to write/serve")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="Prepare the demo bundle and open the dashboard")
    demo.add_argument("--port", type=int, default=8770)
    demo.add_argument("--no-browser", action="store_true")

    prepare = commands.add_parser("prepare", help="Project one source into a bundle")
    source = prepare.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="Run `audit_sim demo` and import it")
    source.add_argument("--run", type=Path, metavar="DIR", help="Output of `audit_sim run --output DIR`")
    source.add_argument("--release", type=Path, metavar="DIR", help="Output of `audit_sim release --output DIR`")
    prepare.add_argument("--episodes", type=Path, metavar="ROOT",
                         help="Dataset root with public/<split>/<episode> for --run (record evidence, split check)")
    prepare.add_argument("--split", help="Split of a --run when it cannot be derived from --episodes")
    prepare.add_argument("--audience", choices=("reviewer", "public"),
                         help="reviewer shows evaluator metrics (synthetic train/dev/demo only); public hides them")
    prepare.add_argument("--name", help="Bundle name: lowercase letters, digits and dashes")
    prepare.add_argument("--label", help="Human-readable source label")

    serve_parser = commands.add_parser("serve", help="Serve prepared bundles on 127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8770)
    serve_parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    out = args.out.resolve()
    try:
        if args.command == "demo":
            print("Running the audit_sim demo and projecting it...")
            prepare_demo(out)
            return serve(out, args.port, not args.no_browser)
        if args.command == "serve":
            return serve(out, args.port, not args.no_browser)
        if args.demo:
            path = prepare_demo(out, args.name or "demo")
        else:
            if args.audience is None:
                parser.error("--audience reviewer|public is required for --run and --release")
            if args.run:
                path = prepare_run(out, args.run, audience=args.audience, name=args.name or "run",
                                   episodes_root=args.episodes, split=args.split, label=args.label)
            else:
                path = prepare_release(out, args.release, audience=args.audience,
                                       name=args.name or "release", label=args.label)
        print(f"Prepared {path}. Open it with: python3 -m dashboard serve")
    except PrepareError as error:
        print(f"dashboard: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
