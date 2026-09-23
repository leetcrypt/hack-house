"""Run the web relay locally.

    cd web-relay && python -m relay --port 8080 --no-tls

P0 rails (GOAL): localhost only. `--host` defaults to 127.0.0.1 and TLS-off is
the local default; binding anywhere public or fronting this with Tailscale
Funnel / a port-forward is a human-gated later phase, not done here.
"""

from __future__ import annotations

import argparse

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="relay", description="hack-house web relay (P0)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (P0: localhost only; default %(default)s)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-tls", action="store_true",
                    help="serve plain http/ws (the P0 local default)")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        ap.error(
            f"refusing to bind {args.host!r}: P0 is localhost-only. Public "
            "exposure is a human-gated phase (see GOAL autonomy envelope)."
        )

    app = create_app()
    scheme = "http" if args.no_tls else "https"
    print(f"[relay] listening on {scheme}://{args.host}:{args.port}  (RAM-only, zero-knowledge)")
    if not args.no_tls:
        print("[relay] note: P0 expects --no-tls for local verification")
    app.run(host=args.host, port=args.port, single_process=True,
            debug=False, access_log=False)


if __name__ == "__main__":
    main()
