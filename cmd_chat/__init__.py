import argparse
import getpass
import os
import sys

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# NOTE: the server (run_server) and client (Client) are imported lazily inside
# main() below. Importing them here would pull sanic/pydantic (server) at
# package-init time for *any* `cmd_chat.*` import — including
# `python -m cmd_chat.operator` on a phone/Termux where those server-only deps
# aren't installed. See docs/termux-operator.md (Phase 0).


def resolve_password(args_password: str | None, prompt: str = "Room password: ") -> str:
    if args_password:
        return args_password
    if env_pw := os.environ.get("CMD_CHAT_PASSWORD"):
        return env_pw
    return getpass.getpass(prompt)


def main():
    parser = argparse.ArgumentParser(description="Command-line chat application")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_p = subparsers.add_parser("serve", help="Run server")
    serve_p.add_argument("ip_address")
    serve_p.add_argument("port")
    serve_p.add_argument("--password", "-p", default=None)
    serve_p.add_argument("--cert", default=None, help="Path to TLS certificate")
    serve_p.add_argument("--key", default=None, help="Path to TLS private key")
    serve_p.add_argument("--no-tls", action="store_true", help="Disable TLS (insecure)")
    serve_p.add_argument(
        "--tor", action="store_true",
        help="Expose this room via an ephemeral Tor v3 onion service (spec-tor-p2p-relay.md)",
    )
    serve_p.add_argument(
        "--tor-allow-public-bind", action="store_true",
        help="Allow --tor with a non-loopback bind address (dual public+onion exposure; see spec §4.2)",
    )
    serve_p.add_argument(
        "--tor-control-socket", default=None,
        help="Unix socket path for the Tor ControlPort, instead of TCP (see spec §4.4)",
    )

    connect_p = subparsers.add_parser("connect", help="Connect to server")
    connect_p.add_argument("ip_address")
    connect_p.add_argument("port")
    connect_p.add_argument("username")
    connect_p.add_argument("--password", "-p", default=None)
    connect_p.add_argument(
        "--insecure", "-k", action="store_true",
        help="Skip TLS certificate verification (for self-signed certs)",
    )
    connect_p.add_argument(
        "--no-tls", action="store_true",
        help="Connect without TLS (insecure)",
    )

    args = parser.parse_args()

    if args.command == "serve":
        from cmd_chat.server.server import run_server

        # Checked before resolve_password() — a refused --tor invocation must
        # not first block on an interactive password prompt it was always
        # going to throw away.
        if args.tor and args.ip_address not in LOOPBACK_HOSTS and not args.tor_allow_public_bind:
            parser.error(
                f"--tor with a non-loopback bind ({args.ip_address!r}) would expose this "
                "room over BOTH the onion address and a plain public/LAN listener at once. "
                "Bind to 127.0.0.1 (recommended), or pass --tor-allow-public-bind if that "
                "dual exposure is deliberate (spec-tor-p2p-relay.md §4.2)."
            )

        password = resolve_password(args.password)

        onion = None
        try:
            if args.tor:
                from cmd_chat.tor.onion import EphemeralOnion

                onion = EphemeralOnion(control_socket=args.tor_control_socket)
                service = onion.start(target_port=int(args.port), virtual_port=int(args.port))
                print(f"[tor] ephemeral onion service: {service.address}:{service.port}")
                # The address is published (and printed) before run_server()
                # below actually binds the local port — a guest who connects
                # in the first instant can hit connection-refused. Accepted
                # tradeoff: closing that window needs the bind to happen
                # before minting the onion, which isn't available without
                # reaching into Sanic's own startup sequence. Self-heals on
                # any client's normal reconnect/retry.

            run_server(
                host=args.ip_address,
                port=int(args.port),
                password=password,
                cert_path=args.cert,
                key_path=args.key,
                no_tls=args.no_tls,
            )
        finally:
            if onion is not None:
                try:
                    onion.stop()
                except Exception as exc:
                    # A teardown failure must never mask whatever exception
                    # (if any) is already propagating out of this block.
                    print(f"[tor] warning: failed to clean up the onion service: {exc}", file=sys.stderr)
    elif args.command == "connect":
        from cmd_chat.client.client import Client

        password = resolve_password(args.password)
        Client(
            server=args.ip_address,
            port=int(args.port),
            username=args.username,
            password=password,
            insecure=args.insecure,
            no_tls=args.no_tls,
        ).run()


if __name__ == "__main__":
    main()
