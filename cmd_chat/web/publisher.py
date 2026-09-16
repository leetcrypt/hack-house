"""Web publisher — the headless room member that egresses a hack-house room's
live terminal to the web relay (spec §3, decision B).

It joins the room exactly like the AI agent (`cmd_chat/agent/bridge.py`): SRP →
room key → chat WS, reconnecting on any drop. As a legitimate member it receives
the decrypted `_sbx:data` PTY stream; it re-publishes each chunk to the relay
over an outbound publish WSS. Two independent connections, decoupled by a queue:

    room chat WS ──_sbx:data──► [out queue] ──► relay publish WSS

**P1 fragment-key E2E** (spec decisions A, §5/§7): each PTY chunk is encrypted
with AES-256-GCM under a per-share random 256-bit `K_web` (fresh random 96-bit
nonce per frame), emitted as `{seq, ct, nonce}`. `K_web` is delivered to viewers
**only** inside the share URL's `#fragment` (`…/r/<slug>#k=<base64url K_web>`),
which browsers never transmit to a server — so the relay brokers ciphertext only
and stays zero-knowledge. The relay never sees `K_web` or plaintext; it copies
`ct`/`nonce` through opaquely.

The driver-token input path (P2) and lobby (P4) remain deferred; their seams are
marked. No Rust/TUI changes; the chat server is untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import time

import requests
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cmd_chat.client.client import MAX_WS_FRAME, Client, _human_size
from cmd_chat.web.webkdf import derive_web_keys


# Frame contract we speak to the relay. MUST match `WIRE_PROTO` in
# `web-relay/relay/app.py`; the duplication is deliberate (the relay is a separate
# deployable, possibly public and possibly running someone else's checkout, so
# there is no shared module to import). Bump both sides by hand when the pub/sub
# frame contract changes in a way an older peer cannot parse — `preflight_relay`
# below turns the drift into one clear line instead of an endless reconnect loop.
WIRE_PROTO = 8

# Minimum PIN length, enforced here and nowhere else — the relay cannot check it,
# because all it ever receives is a public key. That asymmetry is the point of the
# scheme, so this is the only place the rule can live.
#
# Seven digits, not four. The PIN's entropy is the whole security of the room gate
# under a relay compromise: an attacker holding the salt and the public key can run
# candidate PINs offline until the derived key matches. Four digits is 10^4 — a few
# seconds even at 200k PBKDF2 rounds. Seven is 10^7, which those rounds turn into
# something genuinely expensive. Online guessing was never the binding constraint;
# `pin_locker` already caps that.
PIN_MIN_DIGITS = 7


def _local_provision_secret() -> str:
    """The provisioning secret a relay on THIS box wrote down, or "".

    Path convention duplicated from `web-relay/relay/provisioning.py` for the same
    reason as WIRE_PROTO above: the relay is a separate deployable with no shared
    module to import. Keep the two in step — if they drift, a same-box publisher
    silently stops finding the secret and every room creation 401s.

    Read-only by design. The relay generates; a publisher that invented its own
    would be minting a credential the relay has never heard of, turning a clear
    401 into a confusing one.
    """
    explicit = os.environ.get("HH_WEB_PROVISION_SECRET", "").strip()
    if explicit:
        return explicit
    override = os.environ.get("RELAY_STATE_DIR")
    if override:
        root = os.path.expanduser(override)
    else:
        base = os.environ.get("XDG_STATE_HOME")
        root = os.path.join(
            os.path.expanduser(base) if base else
            os.path.join(os.path.expanduser("~"), ".local", "state"),
            "hack-house")
    try:
        with open(os.path.join(root, "relay-secret")) as fh:
            return fh.read().strip()
    except Exception:
        return ""


class WebPublisher(Client):
    _RECONNECT_MAX_BACKOFF = 30.0
    # Keepalive for the relay leg. The room leg (cmd_chat/agent/bridge.py) has always
    # pinged; this one relied on library defaults, which a quiet tunnel outlived — with
    # no sandbox there are no `_sbx:data` frames, so the publish socket can sit silent
    # for minutes and an intermediary (e.g. Cloudflare) drops it. Ping often enough
    # that the socket is never idle, and fail fast so we reconnect instead of writing
    # into a half-open pipe.
    _PING_INTERVAL = 20.0
    _PING_TIMEOUT = 20.0
    # Coalescing (spec §5.3 backpressure): merge the room's many tiny `_sbx:data`
    # chunks into at most one flush every FLUSH_INTERVAL, each ≤ MAX_CHUNK bytes, so
    # a `yes`-flood becomes a few big frames instead of thousands of tiny ones.
    _FLUSH_INTERVAL = 0.03
    _MAX_CHUNK = 8192
    # Periodic current-screen snapshot for late joiners. Raw-byte replay (spec open
    # Q3): keep the last SNAPSHOT_BYTES of output and re-encrypt it whole so a
    # mid-session viewer reconstructs the screen without replaying the full ring.
    _SNAPSHOT_INTERVAL = 2.0
    _SNAPSHOT_BYTES = 64 * 1024
    # Shown to browsers whenever no sandbox is running: clear screen, home the
    # cursor, print a dim notice. Minted as the snapshot so a late joiner sees the
    # *current* state instead of the last dead session's screen, and pushed as an
    # `out` frame on stop so viewers already watching see the session end too.
    _IDLE_SCREEN = b"\x1b[2J\x1b[H\x1b[90m(no sandbox active)\x1b[0m\r\n"

    def __init__(self, server: str, port: int, name: str, relay_url: str,
                 label: str, password: str | None = None,
                 insecure: bool = False, no_tls: bool = False,
                 pin: str | None = None, listed: bool = False,
                 provision_secret: str | None = None,
                 allow_uploads: bool = False, public_url: str | None = None):
        super().__init__(server, port, username=name, password=password,
                         insecure=insecure, no_tls=no_tls)
        self.name = name
        # Path of the 0600 file holding the current share/host URLs, once written.
        # See `_emit_share_url` — the key must have somewhere to go that is not stdout.
        self._share_file: str | None = None
        # Two URLs, deliberately separable:
        #   relay_url  — where WE dial (REST provisioning + the publish websocket)
        #   public_url — what we put in links we hand to humans
        # They differ when the relay is reachable locally but published through a
        # tunnel. Routing our own traffic out to the public edge and back is a long
        # round trip over a link we do not control: when it flaps, the publish
        # socket drops, and anything the relay tries to push at us in that window
        # (a browser's upload offer, a drive request) is dropped on the floor. The
        # browser still needs the public name; we do not.
        self.relay_url = relay_url.rstrip("/")
        self.public_url = (public_url or relay_url).rstrip("/")
        self.label = label
        # Lobby visibility (hardening — enumeration). Rooms are UNLISTED by default:
        # reachable by direct share link but never enumerable via GET /api/rooms.
        # Opt in with --listed only when public discovery is genuinely wanted.
        self.listed = listed
        # Access PIN (defence-in-depth, spec §7). Optional. The relay stores ONLY an
        # Ed25519 PUBLIC key derived from it — never the PIN, and never anything
        # replayable — and the browser proves knowledge by signing a nonce to open
        # the subscribe socket. The PIN gates access/DoS; the #k fragment still gates
        # reading. Shared out-of-band; the PIN itself is NOT put in the share URL.
        self.pin = pin or None
        # Per-room KDF salt for the PIN, minted at register_room and published only
        # in the share link's #fragment. None until then, and None for pinless rooms.
        self.pin_salt: str | None = None
        # Shared provisioning secret (hardening). POST /api/rooms is refused (401)
        # without a matching X-Provision-Secret header. The publisher runs
        # server-side, so it can safely hold the secret.
        #
        # A relay now always has one — unset means it generates and persists it
        # rather than leaving the endpoint open. So when nothing was passed, look
        # for the file a same-box relay left behind: same machine, same user, and
        # the normal self-hosted shape is both processes on one box. Falling back
        # to None is still correct for a relay across the network, where the
        # operator is expected to pass the secret explicitly and would otherwise
        # get a 401 that says exactly that.
        self.provision_secret = provision_secret or _local_provision_secret() or None
        # Per-share E2E key: random 256-bit K_web, unrelated to the room key. It
        # lives ONLY here and in the share URL's #fragment — never sent to the
        # relay. Regenerated per publisher run (== per `/web share`); `/web stop`
        # (DELETE) invalidates it. Fresh 96-bit nonce per frame (never reused).
        # A bytearray, not bytes, so `_zeroise_key` can overwrite it in place —
        # `bytes` is immutable, so wiping it would only rebind the name and leave
        # the original sitting in the heap for a core dump or a swap file to find.
        self.k_web = bytearray(os.urandom(32))
        # Nothing encrypts under K_web itself — see webkdf: one key per channel.
        self._keys = derive_web_keys(bytes(self.k_web))
        # Terminal dims, learned from the room's `_sbx:status`/`resize`.
        self.cols = 80
        self.rows = 24
        # Relay handles, minted by POST /api/rooms.
        self.slug: str | None = None
        self.publish_token: str | None = None
        # Host-console capability (spec §6): a SEPARATE token that lets the operator
        # approve/revoke drive from any browser. Never in the share URL.
        self.host_token: str | None = None
        # Decouples the room recv loop from the relay send loop so a slow relay
        # can't stall room draining. Bounded: drop-oldest on overflow (a viewer
        # catches up on subsequent frames; snapshot replay is P3).
        self._out_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
        self._seq = 0
        # Coalescing buffer: raw PTY bytes tapped from the room, flushed as bounded
        # `out` frames by `_coalesce_loop`. `_screen` is a rolling tail used to mint
        # the periodic snapshot. Both hold plaintext locally only — encrypted under
        # K_web before anything leaves for the relay.
        self._pending = bytearray()
        self._screen = bytearray()
        # Whether a sandbox is currently running in the room. Until a `_sbx:status`
        # frame says otherwise we assume none: a browser that connects before any
        # sandbox exists must see "no sandbox active", not an empty black screen.
        # Flipped by `_sbx:status` state ready/stopped; gates what the snapshot
        # loop mints so a joiner never inherits a dead session's last screen.
        self._sbx_live = False
        # Chat egress (spec §6): monotonic seq for chat frames (independent of the
        # PTY `out` seq) so browsers de-dupe chat on reconnect without colliding
        # with terminal frames. Chat plaintext is encrypted under K_web like `out`.
        self._chat_seq = 0

        # ── P2 input path (spec §6) — two independent gates, both required ──
        # Read-only is the default: keystrokes flow to the PTY only when BOTH
        # hold, so the web path never invents a new bypass around the room's
        # collaborative-sandbox driver-token ACL.
        #   Gate A — the real driver-token ACL: our room username is in the
        #     broker's `drivers` set (learned from `_perm:acl`). A TUI member
        #     grabbing drive clears the set → this drops → browser keys stop.
        #   Gate B — operator per-viewer approval: the operator approved THIS
        #     browser viewer_id, via the host console, the stdin REPL
        #     (`/web allow-input <viewer_id>`), or the owner-only in-room channel
        #     (owner types `/web allow <n>` in the TUI chat).
        self.granted = False                 # Gate A: broker driver-token held
        self.approved_viewer: str | None = None  # Gate B: operator-approved viewer_id
        # WIRE_PROTO 6 / B2. `viewer_id` is minted by the RELAY, so keying Gate B on
        # it means a hostile relay can relabel an unapproved viewer's `in` frame as
        # the approved driver's and its keystrokes reach the PTY — with no key at
        # all. So the browser mints its own 16-byte `vsid` and carries it INSIDE the
        # ciphertext; the relay never learns it and cannot forge one. `viewer_id` is
        # demoted to a routing label. Approval pins the vsid, and only frames whose
        # inner vsid matches it may drive.
        self._vsid_by_vid: dict[str, bytes] = {}   # routing label → identity
        self._vid_by_vsid: dict[bytes, str] = {}   # one vsid may claim only one vid
        self._approved_vsid: bytes | None = None   # Gate B, pinned at approval time
        self._in_ctr: dict[bytes, int] = {}        # replay watermark per vsid
        self._effective_driver: str | None = None  # last value pushed to the relay
        self._room_ws = None                 # live room socket, for submitting input
        # Outstanding drive requests (viewer_ids) awaiting operator approval — shown
        # in the host console so the operator can approve from any browser (spec §6).
        self._pending_requests: set[str] = set()
        # In-room operator channel (owner-only `/web` commands). The room owner,
        # learned from the broker's `_perm:acl` broadcast (`owner` field, hh
        # app.rs:722), is the ONLY member allowed to approve/deny drive from the
        # TUI chat — so this control surface never widens Gate B beyond the person
        # who already holds Gate A. It still cannot bypass Gate A.
        self.room_owner: str | None = None
        # Short ordinal aliases (#1, #2 …) for opaque viewer_ids so the owner can
        # type `/web allow 1` in the TUI instead of a 12-hex id.
        self._alias_by_vid: dict[str, str] = {}
        self._vid_by_alias: dict[str, str] = {}
        self._alias_counter = 0
        # T1.1 — web guests as roster members. Present browser viewers keyed by
        # opaque viewer_id → stable pseudonymous handle. Fed to the Rust TUI as an
        # encrypted `_web:presence` control frame so the host sees who/how-many web
        # guests are in the room. Display-only: these have no room socket and hold
        # only view-only #k, so they can never post as clergy.
        self._web_viewers: dict[str, str] = {}   # viewer_id → handle
        # Deliberately-chosen display names, kept SEPARATELY from the live roster and
        # outliving a viewer's presence in it. A browser announces its name as soon as
        # it is synced, which can land before we have registered the viewer (and a
        # reconnect re-registers it), so the roster alone cannot hold this: the name
        # would be dropped on arrival or reset to the pseudonym on (re)join.
        self._web_names: dict[str, str] = {}     # viewer_id → chosen name
        self._WEB_NAMES_MAX = 256                # bound: opaque ids from a public page
        # A browser announces its name one round trip AFTER it syncs, so a viewer we
        # have no remembered name for would enter the roster as `web-xxxx` and rename
        # itself a moment later — the host watches a stranger appear and mutate. For
        # a brand-new viewer we hold the roster back this long: whichever lands first,
        # the name or this timer, is the first thing the host ever sees.
        self._settle_tasks: dict[str, asyncio.Task] = {}
        # ── file delivery to the web (receiver side; spec §6 extension) ──
        # The publisher proxies room `_ft` offers to browsers: it taps a room-wide
        # (or to-web-publisher) offer, egresses it to viewers, and — when a browser
        # accepts — accepts once into the room and streams the arriving chunks back
        # out under K_web. Web viewers are receivers only (no browser→room upload).
        #   _web_offers: opaque per-offer web token → offer meta (the real room `id`
        #     never reaches the relay; the token is random, so the relay stays blind).
        #   _web_ft: real room offer id → {"tok", "viewers": set(vid), "started"}.
        #     `started` freezes the accepter set once the first chunk flows, so
        #     late accepters don't get a corrupt partial stream.
        self._web_offers: dict[str, dict] = {}   # web token → {id,name,size,sha256,dir,from,desc}
        self._web_ft: dict[str, dict] = {}       # room id → {tok, viewers:set, started:bool}
        self._ft_seq = 0                          # monotonic seq for file byte frames
        # A browser's pending /savevm request (viewer_id) — set when a viewer asks
        # to save the live VM, consumed by the host's resulting `hh-snap-vm-*.tar`
        # offer (which then bypasses the browser size cap, since it streams).
        self._savevm_requester: str | None = None
        # ── file upload FROM the web (sender side; docs/plan-web-upload.md) ──
        # The reverse direction, and a fundamentally different trust problem: the
        # share URL is a bearer capability, so "a viewer may upload" means "anyone
        # the link was forwarded to may write to the operator's machine". An upload
        # is therefore an OFFER, never a write — it is synthesised into a normal
        # room `_ft` offer that terminates in the host's existing `/accept` prompt,
        # and it is refused outright unless the operator passed --allow-uploads.
        #   _uploads: "<viewer_id>:<tok>" → staged transfer. Keyed by both because
        #     `tok` is browser-minted and two guests can pick the same one.
        self.allow_uploads = allow_uploads
        self._uploads: dict[str, dict] = {}
        self._upload_by_fid: dict[str, str] = {}  # room offer id → _uploads key

    # Largest file the publisher will offer/stream to a browser. Browsers hold the
    # whole payload in RAM to build the download Blob (mobile especially can't hold
    # much), so this is far below the 16 GiB room-to-room disk-streamed ceiling.
    WEB_FT_MAX = 100 * 1024 * 1024               # 100 MB
    # A room file is streamed once, but every browser that accepts must get it —
    # accepting is per-viewer, so one viewer taking a file cannot consume it. We
    # retain the assembled bytes and re-serve later accepters from RAM rather than
    # asking the sender to stream again. Retention is per-transfer capped: above
    # this a file is fanned out live but not kept, and a late accepter is told to
    # ask for a resend. Keeps the common case working without holding 100 MB.
    WEB_FT_RETAIN_MAX = 32 * 1024 * 1024         # 32 MB per transfer
    WEB_FT_RETAIN_TOTAL = 64 * 1024 * 1024       # 64 MB across all retained
    # How long a brand-new viewer is held out of the roster while its chosen name
    # (if any) makes the one round trip from `sync` back to us. Long enough to beat
    # a mobile RTT, short enough that a genuinely anonymous guest appears promptly.
    WEB_NAME_SETTLE = 1.0                        # seconds
    # ── upload caps (docs/plan-web-upload.md §4) ──
    # The download path caps things because mobile browsers are fragile. The upload
    # path caps them because the sender is hostile-capable: anyone holding the share
    # URL can start one, so every number here is a DoS bound, not an ergonomic one.
    # 25 MB rather than WEB_FT_MAX's 100 MB because WebCrypto has no streaming
    # digest — the browser must hold the whole file in RAM to hash it (§5) — and
    # because staging lives in publisher RAM on the RAM-only rail.
    WEB_UPLOAD_MAX = 25 * 1024 * 1024            # 25 MB per upload
    WEB_UPLOAD_PER_VIEWER = 1                    # one guest cannot fan out N streams
    WEB_UPLOAD_PER_ROOM = 3                      # bounds staging RAM at ~75 MB
    # An offer nobody answers, or a stream that stalls, must not pin a slot for the
    # rest of the session — otherwise three guests can close the upload path to
    # everyone else by offering files and walking away.
    WEB_UPLOAD_OFFER_TTL = 120.0                 # seconds awaiting a host /accept
    WEB_UPLOAD_STREAM_TTL = 300.0                # seconds mid-stream

    # ── relay control plane (REST) ───────────────────────────────────────
    def preflight_relay(self) -> None:
        """Refuse a relay that cannot speak our frame contract, loudly and up front.

        Without this the failure mode is silent and expensive: an older relay
        accepts the socket, chokes on a frame it does not understand, drops the
        connection, and we reconnect forever while every share URL we mint expires
        seconds after we print it. That looked like a network fault for a whole
        session on 2026-07-26; it was a relay two days stale.

        Advisory by design — a `proto` we cannot read (old relay predating the
        stamp, or an unreachable /health) warns and continues, because a relay we
        do not manage is allowed to be a stranger. Only a definite, parsed
        mismatch is fatal.
        """
        try:
            info = requests.get(f"{self.relay_url}/health", timeout=8,
                                verify=self._rest_verify).json()
        except Exception as exc:
            self.console.print(
                f"[yellow]⚠ relay /health did not answer ({type(exc).__name__}) — "
                f"continuing, but {self.relay_url} may be down[/]")
            return
        proto = info.get("proto")
        if proto is None:
            self.console.print(
                f"[yellow]⚠ {self.relay_url} reports no wire protocol — it predates "
                f"the build stamp and is probably stale. Restart it if it is yours.[/]")
            return
        if proto != WIRE_PROTO:
            raise SystemExit(
                f"✖ relay wire protocol mismatch: {self.relay_url} speaks proto "
                f"{proto}, this publisher speaks {WIRE_PROTO}.\n"
                f"  The relay is running older (or newer) code than this checkout. "
                f"Restart it — locally that is:\n"
                f"      HH_DOWN_RELAY=1 ./scripts/hh-down.sh && ./scripts/hh-up.sh\n"
                f"  Refusing to start: connecting anyway would loop on reconnect and "
                f"burn a new share URL every few seconds.")
        rev = info.get("rev")
        if rev:
            self.info(f"relay ok — proto {proto}, build {rev}")

    def register_room(self) -> None:
        """POST /api/rooms → {slug, publish_token}. Label is public operator text;
        no room content leaves here."""
        body = {"label": self.label, "cols": self.cols, "rows": self.rows,
                "listed": self.listed}
        if self.pin:
            # Only a PUBLIC key leaves here (spec §7). Previously we sent the PBKDF2
            # digest, which the relay stored and compared for equality — so the value
            # in its RAM *was* the credential and could be replayed verbatim by
            # anyone who read it. A public key cannot be used to authenticate.
            #
            # The salt deliberately does NOT go to the relay. It goes in the share
            # link's #fragment (see `share_url`), which never leaves the browser.
            # Keeping the two apart means a relay compromise yields a public key with
            # no salt to run an offline PIN search against.
            self.pin_salt = os.urandom(16).hex()
            body["pin_pub"] = self._pin_public_key(self.pin, self.pin_salt)
        headers = {}
        if self.provision_secret:
            # Gate the relay's create endpoint when it enforces a provisioning secret.
            headers["X-Provision-Secret"] = self.provision_secret
        resp = requests.post(
            f"{self.relay_url}/api/rooms",
            json=body,
            headers=headers,
            timeout=15,
            verify=self._rest_verify,
        )
        resp.raise_for_status()
        body = resp.json()
        self.slug = body["slug"]
        self.publish_token = body["publish_token"]
        self.host_token = body.get("host_token")

    def end_room(self) -> None:
        if not self.slug:
            return
        try:
            # DELETE is authed by the publish_token (hardening) — the same
            # credential /pub uses. Header, not a query arg: a URL-borne bearer
            # token ends up in every access log between here and the relay.
            requests.delete(
                f"{self.relay_url}/api/rooms/{self.slug}",
                headers={"X-Publish-Token": self.publish_token or ""},
                timeout=10, verify=self._rest_verify)
        except Exception:
            pass

    # The relay no longer knows this number — it does not derive anything, it only
    # verifies a signature. The two parties that must agree are this publisher and
    # the browser, and they agree because we put `i=` in the share link beside the
    # salt. Raising it therefore costs nothing but is a breaking change for links
    # already in flight, which die with the publisher run anyway.
    PIN_KDF_ITERS = 200_000

    @classmethod
    def _pin_seed(cls, pin: str, salt_hex: str) -> bytes:
        """Stretch the PIN into a 32-byte Ed25519 seed. This is the only step that
        costs an attacker anything: a PIN is low-entropy, so the 200k rounds are
        what stand between a leaked salt+pubkey pair and a recovered PIN."""
        return hashlib.pbkdf2_hmac(
            "sha256", pin.encode(), bytes.fromhex(salt_hex), cls.PIN_KDF_ITERS,
            dklen=32,
        )

    @classmethod
    def _pin_public_key(cls, pin: str, salt_hex: str) -> str:
        """The room's PIN verifier: raw Ed25519 public key, hex. Safe to publish —
        it verifies a proof, it cannot produce one."""
        sk = Ed25519PrivateKey.from_private_bytes(cls._pin_seed(pin, salt_hex))
        return sk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    @classmethod
    def _pin_sign(cls, pin: str, salt_hex: str, nonce: bytes) -> str:
        """Answer a relay `pin_challenge`. Used by the lab harness and tests; the
        real viewer path is the equivalent WebCrypto code in `room.html`."""
        sk = Ed25519PrivateKey.from_private_bytes(cls._pin_seed(pin, salt_hex))
        return sk.sign(nonce).hex()

    @property
    def _rest_verify(self) -> bool:
        """`verify=` for the REST leg. The WS leg has honoured `--insecure` since
        it was added; these three calls did not, so `--insecure` against a
        self-signed relay failed at room creation with a cert error while the
        socket it was meant to fix would have connected fine. Fails closed —
        verification is only skipped when the operator asked for it."""
        return not self.insecure

    @property
    def _relay_ws_base(self) -> str:
        if self.relay_url.startswith("https://"):
            return "wss://" + self.relay_url[len("https://"):]
        if self.relay_url.startswith("http://"):
            return "ws://" + self.relay_url[len("http://"):]
        return self.relay_url

    def _relay_ssl_context(self):
        # The relay leg's transport security follows the RELAY url scheme, not the
        # room's --no-tls (which governs only the room host). The relay commonly
        # sits behind TLS (e.g. Cloudflare, wss://) even when the room host is
        # plaintext over Tailscale, so keying off self.no_tls would wrongly pass
        # ssl=None to a wss:// URI (websockets raises ValueError).
        if not self._relay_ws_base.startswith("wss://"):
            return None
        if self.insecure:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return True  # default verification

    def share_url(self) -> str:
        # K_web rides ONLY in the #fragment — browsers never send it to a server,
        # so the relay stays blind. base64url, unpadded (URL-clean).
        k = base64.urlsafe_b64encode(self.k_web).rstrip(b"=").decode()
        frag = f"k={k}"
        if self.pin_salt:
            # The PIN's KDF parameters ride here too, for the same reason K_web does:
            # the fragment is the one part of a URL a browser never transmits. The
            # relay used to hand these to any caller that opened /sub without a PIN,
            # which told unauthenticated strangers the salt. Now only someone who
            # already holds the link has it — and the relay itself does not.
            #
            # This is not a secret and it is not a second factor. It is a public KDF
            # input whose whole job is to keep one precomputed table from covering
            # every room; parking it beside the key it is useless without simply
            # denies it to everyone else. The PIN still travels out of band.
            frag += f"&s={self.pin_salt}&i={self.PIN_KDF_ITERS}"
        return f"{self.public_url}/r/{self.slug}#{frag}"

    # ── Share-URL hygiene (2026-08-04) ───────────────────────────────────────
    # `K_web` is minted correctly (os.urandom(32), never POSTed, never in argv) and
    # then used to leak through the one channel nobody audited: stdout. Printing the
    # share URL was the ONLY way to distribute it, so every harness scraped stdout —
    # and on this box that put a live key into `/tmp/hh-demo/share.env` (mode 0644)
    # and `publisher.log`, readable by every local user, for weeks.
    #
    # The fix is to give the key a destination that is not a terminal: a 0600 file.
    # The URL is still printed inline when stdout is a TTY, because a human reading
    # their own terminal is the intended recipient and refusing them would just push
    # people back to `tee`. When stdout is a pipe or a file — i.e. something is
    # capturing it — we print the PATH instead. That is the exact case that burned us.
    def _share_dir(self) -> str:
        """Per-user runtime dir for share files. Prefers XDG_RUNTIME_DIR (tmpfs,
        already 0700, cleared on logout); falls back to XDG_STATE_HOME."""
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            root = os.path.join(os.path.expanduser(runtime), "hack-house")
        else:
            base = os.environ.get("XDG_STATE_HOME")
            root = os.path.join(
                os.path.expanduser(base) if base else
                os.path.join(os.path.expanduser("~"), ".local", "state"),
                "hack-house")
        os.makedirs(root, mode=0o700, exist_ok=True)
        return root

    def _write_share_file(self) -> str | None:
        """Write the current share + host URLs to a 0600 file; return its path.

        Opened with an explicit mode rather than written and chmod'd after: the
        chmod version leaves a window where the file exists at the umask default,
        which is precisely how the original leak looked."""
        try:
            path = os.path.join(self._share_dir(), f"share-{self.slug}.url")
            body = (f"# hack-house share URLs — BEARER CREDENTIALS, keep 0600\n"
                    f"# the #k fragment is the end-to-end key; anyone with it reads\n"
                    f"# the terminal, room chat, and any file offered to the room.\n"
                    f"SHARE_URL={self.share_url()}\n"
                    f"HOST_URL={self.host_url()}\n")
            # The access PIN is a SECOND factor, meant to be spoken out of band and
            # NOT alongside the link. It lives here only so the host's own scan-me
            # pane can surface it on this machine's screen — it never rides the QR
            # or the URL. Written last, clearly separated, and only when set.
            if self.pin:
                body += (f"# PIN — say this OUT LOUD to viewers, separately from the\n"
                         f"# link above. It never travels in the URL or the QR.\n"
                         f"PIN={self.pin}\n")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, body.encode())
            finally:
                os.close(fd)
            self._share_file = path
            return path
        except OSError as e:  # noqa: BLE001 — never let this kill the publisher
            self.error(f"could not write share file ({e}) — use `/web link` on a "
                       "terminal to read the URL")
            return None

    def _emit_share_url(self, label: str) -> None:
        """Surface the share URL without letting it reach a log.

        Always refreshes the 0600 file (the URL changes on every reprovision, and a
        stale file pointing at a dead room is worse than none)."""
        path = self._write_share_file()
        if sys.stdout.isatty():
            self.success(f"{label} {self.share_url()}")
            if path:
                self.info(f"  (also written to {path}, mode 0600)")
            return
        # Captured stdout: a log, a pipe, a tmux scrollback dump. Do not print the key.
        if path:
            self.success(f"{label} written to {path} (mode 0600)")
            self.info("  stdout is not a terminal — withholding the #k key so it "
                      "does not land in a log. `cat` the file, or run `/web link` "
                      "on a terminal.")
        else:
            self.error(f"{label} not printed: stdout is not a terminal and the "
                       "share file could not be written.")

    def host_url(self) -> str:
        # Operator console. The host_token rides in the #fragment (kept out of any
        # Referer); it authorizes only drive approve/revoke, NOT reading (no #k).
        return f"{self.public_url}/host/{self.slug}#t={self.host_token}"

    def _enqueue(self, frame: dict) -> None:
        """Non-blocking hand-off to the relay loop; drop-oldest when full."""
        try:
            self._out_q.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                self._out_q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._out_q.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    # ── entry point ──────────────────────────────────────────────────────
    def run(self) -> None:
        self.preflight_relay()
        self.register_room()
        self._print_banner()
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            self.info("\nweb publisher stopped")
        finally:
            self.end_room()
            self._forget_share_file()
            self._zeroise_key()

    def _zeroise_key(self) -> None:
        """Overwrite our copy of K_web in place.

        Honest about what this is: each channel's `AESGCM` holds its own copy
        inside OpenSSL that we cannot reach, so this does not scrub the key
        material from the process. What it does remove is the long-lived *Python*
        copy — the one that would otherwise sit in the heap for the life of a
        hung interpreter, and land in a core dump or a swap page. Cheap, so worth
        doing; not a substitute for ending the room, which is what actually
        revokes the link. Dropping `_keys` at least makes the derived keys
        collectable, which the master copy alone would not."""
        k = getattr(self, "k_web", None)
        if isinstance(k, bytearray):
            for i in range(len(k)):
                k[i] = 0
        self.k_web = bytearray(32)
        self._keys = {}

    def _forget_share_file(self) -> None:
        """Remove the 0600 share file on exit. Its key is dead once `end_room`
        has run, and a file that still reads like a live invitation is worse than
        no file: the next person to `cat` it gets a URL that silently opens
        nothing, and the key sits on disk for no reason at all."""
        path, self._share_file = self._share_file, None
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    def _print_banner(self) -> None:
        # ACCURATE CAPABILITY TEXT (2026-08-04). This used to say "read-only", which
        # was false in three directions: `_post_chat` lets any link holder POST into
        # room chat under our identity, `_tap_chat` egresses EVERY human room message
        # to browsers, and `_web_offer` egresses every room-wide file offer. An
        # overstated security claim is worse than a stated limitation — it is the
        # thing that gets tested first by the audience this ships to.
        self.console.print(
            "[yellow bold]⚠ web share is a real trust change.[/] The share URL "
            "(slug + [bold]#k[/]) is a bearer capability — treat it like a password.\n"
            "[bold]Anyone with the link can:[/]\n"
            "  • read the room terminal, live\n"
            "  • read [bold]all room chat[/], including messages from native members\n"
            "  • download [bold]any file offered to the room[/]\n"
            "  • [bold]post chat messages into the room[/]\n"
            "[bold]They cannot[/] type into the terminal unless you approve them "
            "(driver-token ACL + per-viewer approval).\n"
            "[dim]End-to-end encrypted: AES-256-GCM under a per-share key carried "
            "only in the #fragment. The relay brokers ciphertext and never sees the "
            "key or plaintext — but it is not a trusted party, and everyone holding "
            "the link shares one key, so there is no per-viewer secrecy.[/]"
        )
        self._emit_share_url("share URL (full room access):")
        self.console.print(
            f"[magenta bold]host console (operator — approve drive):[/] {self.host_url()}\n"
            "[dim]keep this one private: it approves drive requests. It cannot read "
            "the terminal (no #k) and cannot bypass the room's driver-token ACL.[/]")
        if self.public_url != self.relay_url:
            self.info(f"relay: {self.relay_url} (dialled)  →  {self.public_url} (public)"
                      f"   room label: {self.label!r}")
        else:
            self.info(f"relay: {self.relay_url}   room label: {self.label!r}")
        self.console.print(
            "[dim]interactive REPL: type [/][bold]/web qr[/][dim] for a scannable "
            "terminal QR of the share URL, [/][bold]/web link[/][dim] to reprint it.[/]")
        if self.pin:
            self.console.print(
                f"[cyan bold]room PIN: {self.pin}[/] — share this out-of-band "
                "(NOT in the URL). Viewers must enter it to open the room.")

    async def _run_async(self) -> None:
        await asyncio.gather(
            self._room_loop(), self._relay_loop(), self._console_loop(),
            self._coalesce_loop(), self._snapshot_loop())

    # ── coalescing + snapshot (spec §5.3 backpressure / replay) ──────────
    async def _coalesce_loop(self) -> None:
        """Drain the raw PTY buffer every FLUSH_INTERVAL into bounded `out` frames.
        Merging many tiny room chunks into a few ≤ MAX_CHUNK frames keeps a flood
        from saturating the relay socket (spec §5.3)."""
        while True:
            await asyncio.sleep(self._FLUSH_INTERVAL)
            while self._pending:
                chunk = bytes(self._pending[:self._MAX_CHUNK])
                del self._pending[:self._MAX_CHUNK]
                self._enqueue({"type": "out", "pt": chunk})

    def _set_sbx_live(self, live: bool) -> None:
        """Track sandbox liveness from `_sbx:status`, discarding the screen tail on
        every transition.

        Both edges must clear it. On stop the tail is a dead session's final screen,
        which the snapshot loop would otherwise keep serving to anyone opening the
        share link — the host sees no sandbox, the browser sees a terminal. On start
        it belongs to the *previous* sandbox, so replaying it would prepend a
        stranger's output to a fresh session. Viewers already watching are told too:
        the snapshot only reaches joiners, so without an `out` frame a live browser
        would sit on a frozen screen with no hint the session ended."""
        if live == self._sbx_live:
            return
        self._sbx_live = live
        self._pending.clear()
        self._screen.clear()
        if not live:
            # Release the owner pin (see the `_perm:acl` handler). The pin exists so
            # a room member cannot displace the owner mid-session; it must NOT
            # outlive the session, or the next person to start a sandbox in this
            # room is permanently unable to drive the `/web` channel. A stopped
            # sandbox is exactly the boundary where re-pinning is safe: Gate A is
            # already false, so an attacker who wins the next pin still holds no
            # driver token and reaches no PTY.
            self.room_owner = None
            self.granted = False
            self._recompute_driver()
            self._enqueue({"type": "out", "pt": self._IDLE_SCREEN})

    async def _snapshot_loop(self) -> None:
        """Periodically publish the rolling screen tail as an encrypted `snapshot`
        so a late joiner reconstructs the current screen from one blob + the ring
        tail, not the whole history. `seq` is the last-drained seq (biased low → a
        joiner may re-apply a few frames, never miss any).

        With no sandbox running the tail is empty, so mint the idle notice instead —
        the relay keeps the last snapshot it was given, and skipping the publish
        would leave a joiner reconstructing whatever was on screen before."""
        while True:
            await asyncio.sleep(self._SNAPSHOT_INTERVAL)
            if not self._sbx_live:
                self._enqueue({"type": "snapshot", "pt": self._IDLE_SCREEN,
                               "seq": self._seq})
            elif self._screen:
                self._enqueue({"type": "snapshot", "pt": bytes(self._screen),
                               "seq": self._seq})

    # ── P2 input path: gate + submit ─────────────────────────────────────
    def _recompute_driver(self) -> None:
        """Recompute the effective driver (approved viewer *and* driver token held)
        and, if it changed, tell the relay so it can mirror the roster badge. The
        relay never decides this — it only reflects our decision (opaque viewer_id).
        Always re-broadcasts gate state to the host console so the operator sees
        Gate A (driver token) flips even when the effective driver is unchanged."""
        effective = self.approved_viewer if (self.granted and self.approved_viewer) else None
        if effective != self._effective_driver:
            self._effective_driver = effective
            self._enqueue({"type": "driver", "viewer_id": effective})
            # Refresh the host roster so the `driving` badge tracks the new driver.
            # Sync method, live loop: schedule the async broadcast, ignore if no loop.
            try:
                asyncio.get_running_loop().create_task(self._broadcast_web_presence())
            except RuntimeError:
                pass
        self._emit_host_state()

    # ── host console (spec §6): notify + approve from any browser ────────
    def _emit_host_state(self) -> None:
        """Push current gate state to the operator console(s): Gate A (driver token
        held), the approved viewer (Gate B), the effective driver, and outstanding
        requests. Routing metadata only — no room content ever leaves here."""
        self._enqueue({
            "type": "host_event", "kind": "state",
            "granted": self.granted,
            "approved": self.approved_viewer,
            "driver": self._effective_driver,
            "pending": sorted(self._pending_requests),
        })

    def _emit_host_request(self, vid: str) -> None:
        """Announce a new drive request to the console (drives the notification)."""
        self._enqueue({"type": "host_event", "kind": "request", "viewer_id": vid})

    # ── in-room operator channel (owner-only `/web`) ─────────────────────
    # Bounded like `_web_names`, and for the same reason: a guest who churns its
    # client_id mints a fresh opaque viewer_id every time, and the alias tables
    # were only ever cleared on reprovision. Unbounded growth driven by an
    # anonymous public page is a memory DoS that costs the attacker nothing.
    _ALIAS_MAX = 256

    def _alias_for(self, vid: str) -> str:
        """Stable short ordinal (#1, #2 …) for an opaque viewer_id, so the owner
        types `/web allow 1` in the TUI instead of a 12-hex id."""
        a = self._alias_by_vid.get(vid)
        if a is None:
            # Evict the oldest pair before minting. Both directions must go, or
            # `_vid_by_alias` keeps resolving an alias whose viewer is long gone.
            while len(self._alias_by_vid) >= self._ALIAS_MAX:
                old_vid = next(iter(self._alias_by_vid))
                old_alias = self._alias_by_vid.pop(old_vid)
                self._vid_by_alias.pop(old_alias, None)
            self._alias_counter += 1
            a = str(self._alias_counter)
            self._alias_by_vid[vid] = a
            self._vid_by_alias[a] = vid
        return a

    def _resolve_vid(self, token: str) -> str | None:
        """Map an owner-typed token to a viewer_id: an ordinal alias (`1`), or a
        raw `v_…` id typed in full. Anything else is unknown."""
        if token in self._vid_by_alias:
            return self._vid_by_alias[token]
        if token.startswith("v_"):
            return token
        return None

    # ── T1.1: web guests as roster members ───────────────────────────────
    @staticmethod
    def _web_handle(vid: str) -> str:
        """Stable pseudonymous handle for a web guest, derived from its opaque
        viewer_id (which the relay keeps stable across reconnects). Not a secret —
        just a human-readable label so the host roster reads `web-a1b2` instead of
        a 12-hex id."""
        return "web-" + hashlib.sha1(vid.encode()).hexdigest()[:4]

    def _chat_handle(self, vid: str | None) -> str:
        """The name a web guest's room chat is posted under — chosen by us.

        A name the guest set via `_apply_web_name` is honoured (that is opt-in
        de-anonymisation and the host already sees it on the roster), but it is
        always carried behind the `web/` marker, so `web/alice` can never be
        confused with a native member called `alice`. `/` cannot appear in the
        marker position of a native handle by accident."""
        if not vid:
            return "web/guest"
        name = self._web_names.get(vid)
        if name:
            safe = name.replace("/", "-").replace(":", "-").strip()[:24]
            if safe:
                return f"web/{safe}"
        return f"web/{self._web_handle(vid)[4:]}"

    async def _broadcast_web_presence(self) -> None:
        """Publish the current web-guest roster to the room as an encrypted
        `_web:presence` control frame. The Rust TUI decrypts it and merges the
        guests into its clergy panel; the E2E server never reads it. Starts with
        `{"_` so `_tap_chat` treats it as a control frame (not chat) and does NOT
        egress it back out to web viewers."""
        ws = self._room_ws
        if ws is None:
            return
        viewers = [
            {
                "id": vid,
                "alias": self._alias_for(vid),
                "handle": handle,
                "driving": vid == self._effective_driver,
            }
            for vid, handle in self._web_viewers.items()
        ]
        frame = json.dumps({
            "_web": "presence",
            "count": len(viewers),
            "publisher": self.name,
            "viewers": viewers,
        })
        try:
            await ws.send(self.room_fernet.encrypt(frame.encode()).decode())
        except Exception:
            pass

    async def _post_room_notice(self, text: str) -> None:
        """Post a plain chat line into the room as this member, so the owner's TUI
        shows drive requests/approvals inline — no separate host-console link.
        Plain text (never starts with '{\"_') so the broker treats it as chat, not
        a control frame. Must contain no ': ' so our own echo isn't split by
        `_tap_chat`'s 'handle: text' web-egress rule."""
        ws = self._room_ws
        if ws is None:
            return
        try:
            await ws.send(self.room_fernet.encrypt(text.encode()).decode())
        except Exception:
            pass

    async def _handle_room_command(self, cmd: str, is_owner: bool) -> None:
        """Apply a `/web …` command from room chat. Read-only queries (link/help)
        answer any member; drive-control (allow/deny/revoke/list) requires
        `is_owner` and routes to the SAME `_apply_allow` / `_apply_revoke` the host
        console uses — Gate A still gates real typing."""
        parts = cmd.split()
        sub = parts[1].lower() if len(parts) > 1 else ""
        # Open to any member: the #k link is strictly weaker than the room key every
        # member already holds, so reprinting it is not a widening. Works even
        # before the acl is seen (owner unknown) — the common late-join case.
        #
        # Not "view-only". Holding this URL also grants room chat, file download and
        # posting into the room; only *typing* into the terminal stays gated. The
        # start page leads with that warning, and calling it view-only here
        # contradicted the one thing a host most needs to get right before sharing.
        if sub in ("link", "url"):
            await self._post_room_notice(
                f"⌂ web share link — bearer capability, treat it like a password "
                f"→ {self.share_url()}")
            return
        if sub in ("help", ""):
            await self._post_room_notice(
                "⌂ /share (= /web link) · /web allow <n> · /web deny <n> · /web revoke · /web list")
            return
        # Drive-control (Gate B) — owner only. Silently ignore a non-owner so they
        # can't probe viewer state; fails closed until the acl names the owner.
        if not is_owner:
            return
        if sub == "allow" and len(parts) >= 3:
            vid = self._resolve_vid(parts[2])
            if not vid:
                await self._post_room_notice(f"⌂ /web — no such viewer {parts[2]!r}")
                return
            a = self._alias_for(vid)
            if not self._apply_allow(vid):
                await self._post_room_notice(
                    f"⌂ web viewer #{a} has not identified itself yet — ask them to "
                    f"reload the page, then /web allow {a}")
                return
            if self.granted:
                await self._post_room_notice(f"⌂ approved web viewer #{a} — typing enabled")
            else:
                await self._post_room_notice(
                    f"⌂ approved web viewer #{a}, but drive token not held — /grant {self.name}")
        elif sub in ("deny", "reject") and len(parts) >= 3:
            vid = self._resolve_vid(parts[2])
            if not vid:
                await self._post_room_notice(f"⌂ /web — no such viewer {parts[2]!r}")
                return
            self._pending_requests.discard(vid)
            if vid == self.approved_viewer:
                self._apply_revoke()
            else:
                self._emit_host_state()
            await self._post_room_notice(f"⌂ denied web viewer #{self._alias_for(vid)}")
        elif sub == "revoke":
            self._apply_revoke()
            await self._post_room_notice("⌂ web input revoked — view-only")
        elif sub in ("list", "viewers"):
            if self._pending_requests:
                pend = " ".join(f"#{self._alias_for(v)}" for v in sorted(self._pending_requests))
                await self._post_room_notice(f"⌂ web viewers requesting drive — {pend}")
            else:
                await self._post_room_notice("⌂ no web viewers requesting drive")
        else:
            await self._post_room_notice(
                "⌂ /web link · /web allow <n> · /web deny <n> · /web revoke · /web list")

    def _apply_allow(self, vid: str) -> bool:
        """Approve a viewer for drive (Gate B). Shared by the stdin console and the
        host web console — both routes are identical; neither bypasses Gate A.

        The operator picks a viewer by its relay-assigned label, but what gets
        PINNED is the vsid that label is bound to (B2). From here on the relay can
        relabel all it likes; only frames carrying that identity may drive.

        Fails closed if the label has no identity yet. Every browser announces its
        vsid on connect, so this means the label does not correspond to a live
        viewer of this build — and resolving it lazily on the first `in` frame
        would hand the relay a window to choose who gets approved."""
        vsid = self._vsid_by_vid.get(vid)
        if vsid is None:
            self.error(f"[web] cannot approve {vid} — that viewer has not identified "
                       f"itself (no vsid). Ask them to reload the page.")
            return False
        self.approved_viewer = vid
        self._approved_vsid = vsid
        self._pending_requests.discard(vid)
        self._recompute_driver()
        if self.granted:
            self.success(f"[web] input approved for {vid} (driver token held — driving)")
        else:
            self.info(f"[web] approved {vid}, but no driver token yet — grant drive "
                      f"to '{self.name}' in the room first")
        return True

    def _apply_revoke(self) -> None:
        prev = self.approved_viewer
        self.approved_viewer = None
        self._approved_vsid = None
        self._recompute_driver()
        self.info(f"[web] input revoked ({prev}) — now view-only")

    def _may_drive(self, vid: str | None) -> bool:
        """Is this viewer allowed to act on the sandbox right now? Gate A (the room
        owner granted us the driver token) AND Gate B (the operator approved this
        specific viewer). Both are live state — a revoke takes effect on the next
        frame, with no cached capability to leak."""
        return bool(self.granted and vid and vid == self.approved_viewer)

    def _aad(self, kind: str, seq: int = 0) -> bytes:
        """Additional authenticated data binding the routing metadata the RELAY
        controls into the AEAD tag.

        Without this the relay can renumber, reorder and relabel frames it cannot
        read — enough to censor the transcript (bump one `seq` to 999999 and the
        browser's `<= lastSeq` guard silently discards everything after) or to
        route an unapproved viewer's keystrokes to the PTY. GCM authenticates the
        AAD without encrypting it, so binding costs nothing on the wire and makes
        every one of those edits a decrypt failure.

        `hh1` is a version tag so a future format change cannot be confused with
        this one. Mirrored EXACTLY in room.html::aadFor — the two must agree byte
        for byte or nothing decrypts."""
        return f"hh1|{self.slug}|{kind}|{seq}".encode()

    # In-frame plaintext layout (WIRE_PROTO 7):
    #   vsid(16) || ctr(6, big-endian) || len(2, big-endian) || payload || zero pad
    # The counter is 6 bytes because the browser seeds it from Date.now(): that
    # makes it monotonic across a page reload without persisting anything, which
    # matters because a reload that restarted at 0 would look exactly like a replay
    # and lock the viewer out of their own keyboard.
    #
    # B7: the payload is zero-padded up to a multiple of 64 bytes, which is why the
    # length has to be carried explicitly. GCM ciphertext is plaintext+16, so an
    # unpadded frame told the relay the exact byte length of every keystroke burst
    # — including each character of a password typed into a `sudo` prompt. Bucketing
    # removes that channel; the arrival-time channel is only blunted (see the
    # browser's coalescing timer) and is documented as residual, because closing it
    # needs constant-rate cover traffic.
    _VSID_LEN = 16
    _CTR_LEN = 6
    _LEN_LEN = 2
    _IN_HDR = _VSID_LEN + _CTR_LEN + _LEN_LEN
    _IN_BUCKET = 64

    def _bind_vsid(self, vid: str, vsid: bytes) -> bool:
        """Bind a routing label to the identity the browser minted for itself.

        First claim wins, and a vsid may claim only ONE vid. That second rule is
        what stops a hostile relay replaying a captured frame under someone else's
        label to poison the binding: the vsid inside it is already spoken for.

        Residual, stated plainly: a relay COLLUDING with a viewer can still have
        that viewer mint a fresh vsid and announce it under another label before
        the real viewer connects. That is the malicious-co-viewer case, which one
        shared K_web cannot address at all — it needs per-viewer keys (deferred)."""
        known = self._vsid_by_vid.get(vid)
        if known is not None:
            return known == vsid
        owner = self._vid_by_vsid.get(vsid)
        if owner is not None and owner != vid:
            self.info(f"[web] refused to bind {vid} to a vsid already held by "
                      f"{owner} — relabelled or replayed frame")
            return False
        self._vsid_by_vid[vid] = vsid
        self._vid_by_vsid[vsid] = vid
        return True

    def _may_drive(self, vid: str | None) -> bool:
        """Is this viewer allowed to act on the sandbox right now? Gate A (the room
        owner granted us the driver token) AND Gate B (the operator approved this
        specific viewer). Both are live state — a revoke takes effect on the next
        frame, with no cached capability to leak.

        Kept for the non-input paths (chat, `/ai`) that have no vsid of their own.
        The PTY path uses `_may_drive_vsid`, which the relay cannot influence."""
        return bool(self.granted and vid and vid == self.approved_viewer)

    def _may_drive_vsid(self, vsid: bytes) -> bool:
        """Gate A AND Gate B, decided on the browser-minted identity rather than
        the relay-assigned label. A relabelled frame fails here."""
        return bool(self.granted and self._approved_vsid
                    and hmac.compare_digest(vsid, self._approved_vsid))

    async def _forward_input(self, msg: dict) -> None:
        """A browser `in` frame arrived via the relay. Decrypt under K_web (AAD-
        bound), establish who really sent it from the vsid inside the ciphertext,
        reject replays, enforce BOTH gates, then submit as a room `_sbx:input`
        frame (the broker re-checks our driver-token membership before the PTY).

        Order matters: decrypt FIRST, gate second. The old code gated on the
        relay's `viewer_id` before decrypting, which is precisely the label a
        hostile relay gets to choose."""
        vid = msg.get("viewer_id")
        ct, nonce = msg.get("ct"), msg.get("nonce")
        if not vid or not ct or not nonce:
            return
        try:
            pt = self._keys["in"].decrypt(base64.b64decode(nonce),
                                          base64.b64decode(ct), self._aad("in"))
        except Exception:
            return  # undecryptable/forged → drop; the relay could not have made this
        if len(pt) < self._IN_HDR:
            return
        vsid = bytes(pt[:self._VSID_LEN])
        ctr = int.from_bytes(pt[self._VSID_LEN:self._VSID_LEN + self._CTR_LEN], "big")
        n = int.from_bytes(pt[self._VSID_LEN + self._CTR_LEN:self._IN_HDR], "big")
        if self._IN_HDR + n > len(pt):
            return  # a length that runs past the frame is malformed, not short input
        payload = pt[self._IN_HDR:self._IN_HDR + n]   # the rest is padding

        if not self._bind_vsid(vid, vsid):
            return
        # Monotonic per-identity counter: a captured `in` frame replayed later (the
        # relay can hold and re-send anything) lands on a counter it already used.
        if ctr <= self._in_ctr.get(vsid, 0):
            return
        self._in_ctr[vsid] = ctr

        # An empty payload is the announce a browser sends on connect purely to
        # register its vsid, so that approval has something to pin. It carries no
        # keystrokes and is not gated.
        if not payload:
            return
        ws = self._room_ws
        if ws is None or not self._may_drive_vsid(vsid):
            return  # read-only: unapproved identity or no driver token → dropped
        frame = json.dumps({"_sbx": "input", "b64": base64.b64encode(payload).decode()})
        try:
            await ws.send(self.room_fernet.encrypt(frame.encode()).decode())
        except Exception:
            pass

    @staticmethod
    def _is_sandbox_ask(text: str) -> bool:
        """True if `text` is an `/ai` form that makes the agent act in the sandbox.

        Asking the agent a question is just chat, and chat is operator-enabled.
        These two forms are different in kind — they make the agent run commands
        under *its own* driver token, which is the same capability keystrokes
        carry. So they are held to the same bar: `_post_chat` allows them only
        for a viewer that `_may_drive`, and refuses them from everyone else.
        Without that check any holder of the share URL would have a shell.

        Two verbs reach the sandbox:

        - ``!<task>`` — `AgentBridge._run_in_sandbox`.
        - ``confirm`` — `AgentBridge._confirm_pending`, which injects the plan
          the agent held back *precisely because it was destructive*. Gating `!`
          while letting `confirm` through would leave the approval on the gated
          command open to anyone with the link, which is the whole point of
          gating it.

        Both shapes the agent accepts are covered for each: sole-agent
        (`/ai !x`) and targeted (`/ai <name> !x`). We do not know which room
        members are agents, so anything of the form `/ai <word> !…` counts
        rather than being guessed at — over-matching costs an unapproved guest
        one reworded question, while under-matching costs a shell.
        """
        if not (text == "/ai" or text.startswith("/ai ")):
            return False
        rest = text[3:].strip()
        _, _, tail = rest.partition(" ")
        tail = tail.strip()
        if rest.startswith("!") or tail.startswith("!"):
            return True
        # `_addressed_question` lower-cases and strips before matching `confirm`,
        # so match the same way or `/ai CONFIRM ` walks straight past this.
        return rest.lower() == "confirm" or tail.lower() == "confirm"

    # ── chat egress (spec §6) — operator-enabled read+send ───────────────
    def _tap_chat(self, username: str, text: str) -> None:
        """Egress one plain room chat message to the web, encrypted under K_web.
        Our own web-origin posts arrive as 'handle: text' — split them so the
        browser shows the web handle, not our room username."""
        frm = username
        if username == self.name and ": " in text:
            handle, rest = text.split(": ", 1)
            frm, text = handle + " (web)", rest
        self._chat_seq += 1
        payload = json.dumps({
            "from": str(frm)[:32], "text": str(text)[:2000],
            "ts": int(time.time() * 1000),
        })
        self._enqueue({"type": "chat", "pt": payload.encode(), "seq": self._chat_seq})

    async def _post_chat(self, msg: dict) -> None:
        """A browser chat message arrived via the relay. Decrypt under K_web and
        post it into the room as a normal message. Read+send chat is operator-
        enabled — any holder of the share URL can post. The mandatory 'handle: '
        prefix guarantees the room text can never begin with {" and so cannot be
        mis-parsed as a _sbx/_ft/_perm control frame by other room members.

        The handle is OURS, not the browser's. The message body used to carry a
        `from` field that we printed verbatim, so any link holder could post as
        the host — or as a native room member — to an audience with no way to tell.
        We substitute the roster handle we already hold for this viewer, and mark
        every web-origin line `web/…` so a room member can always distinguish a
        browser guest from a native member."""
        ct, nonce = msg.get("ct"), msg.get("nonce")
        ws = self._room_ws
        if not ct or not nonce or ws is None:
            return
        try:
            pt = self._keys["chat"].decrypt(base64.b64decode(nonce), base64.b64decode(ct), None)
            obj = json.loads(pt)
        except Exception:
            return  # undecryptable/forged/malformed → drop
        handle = self._chat_handle(msg.get("viewer_id"))
        text = str(obj.get("text") or "").replace("\r", " ").replace("\n", " ").strip()[:2000]
        if not text:
            return
        # `/ai !<task>` and `/ai confirm` make the agent act in the sandbox, so
        # they need the same approval a keystroke needs — checked here, live,
        # against the same two gates rather than a capability handed out earlier.
        if self._is_sandbox_ask(text) and not self._may_drive(msg.get("viewer_id")):
            text = (
                "[refused: /ai !<task> and /ai confirm run commands in the sandbox — "
                "ask the host for drive first]"
            )
        line = f"{handle}: {text}"
        try:
            await ws.send(self.room_fernet.encrypt(line.encode()).decode())
        except Exception:
            pass

    async def _apply_web_name(self, msg: dict) -> None:
        """A browser set/changed its display name. Decrypt the chosen name under
        K_web and adopt it as this viewer's roster handle, then re-broadcast
        presence so the TUI's clergy panel shows the real name instead of the
        derived `web-xxxx` pseudonym. Opt-in de-anonymisation — a guest is
        pseudonymous until it deliberately sends a name; an empty name reverts to
        the pseudonym."""
        vid = msg.get("viewer_id")
        ct, nonce = msg.get("ct"), msg.get("nonce")
        if not vid or not ct or not nonce:
            return
        try:
            pt = self._keys["chat"].decrypt(base64.b64decode(nonce), base64.b64decode(ct), None)
            obj = json.loads(pt)
        except Exception:
            return  # undecryptable/forged/malformed → drop
        name = str(obj.get("name") or "").replace("\n", " ").replace("\r", " ").strip()[:24]
        # Record the choice even if this viewer is not in the roster yet. A browser
        # announces its name the moment it is synced, which can arrive before we have
        # registered the viewer; requiring registration here is what silently threw
        # the name away and left the host looking at a `web-xxxx` pseudonym until the
        # user set it a second time.
        if name:
            if len(self._web_names) >= self._WEB_NAMES_MAX and vid not in self._web_names:
                self._web_names.pop(next(iter(self._web_names)), None)
            self._web_names[vid] = name
        else:
            self._web_names.pop(vid, None)   # empty name → back to the pseudonym
        if vid not in self._web_viewers:
            return  # remembered; applied when the viewer registers
        # The name won the race against the settle timer — publish it directly.
        pending = self._cancel_settle(vid)
        handle = name or self._web_handle(vid)
        if self._web_viewers.get(vid) == handle and not pending:
            return  # no change → no needless presence churn
        # `pending` forces the broadcast even when the handle is unchanged: nothing
        # has been published for this viewer yet, so skipping here would strand it
        # off the roster until the next unrelated presence change.
        self._web_viewers[vid] = handle
        await self._broadcast_web_presence()

    # ── new-viewer roster settle ─────────────────────────────────────────
    def _cancel_settle(self, vid: str) -> bool:
        """Drop any pending roster-settle for `vid`. True if one was still armed."""
        task = self._settle_tasks.pop(vid, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def _schedule_settle(self, vid: str) -> None:
        """Publish this viewer's pseudonym after the settle window — unless a name
        arrives first and `_apply_web_name` cancels us."""
        self._cancel_settle(vid)
        self._settle_tasks[vid] = asyncio.create_task(self._settle_presence(vid))

    async def _settle_presence(self, vid: str) -> None:
        try:
            await asyncio.sleep(self.WEB_NAME_SETTLE)
            self._settle_tasks.pop(vid, None)
            await self._broadcast_web_presence()
        except asyncio.CancelledError:
            pass

    # ── file delivery to the web (receiver side) ─────────────────────────
    async def _tap_ft(self, username: str, frame: dict) -> None:
        """Route a room `_ft` control frame through the web-delivery proxy. Offers
        become a trust-annotated browser popup; chunks/done for an offer a browser
        accepted stream back out under K_web. Unrelated human-to-human transfers and
        our own accept echoes are ignored (only ids in `_web_ft` are forwarded)."""
        kind = frame.get("_ft")
        if kind in ("chunk", "done"):
            # A sender streams one addressed copy per accepter, and the relay
            # broadcasts them all. Consume only our own copy — ingesting another
            # member's would interleave two streams into one browser download.
            # An absent `to` is an untargeted stream (a sender from before
            # addressing), which is ours as much as anyone's.
            to = frame.get("to")
            if to and to != self.name:
                return
        if kind in ("accept", "reject"):
            # The host answering a guest's upload offer. Only ids we synthesised
            # are ours; every other accept/reject is human-to-human traffic.
            await self._upload_answered(kind, username, frame)
            return
        if kind == "offer":
            await self._web_offer(username, frame)
        elif kind == "chunk":
            await self._web_ft_chunk(frame)
        elif kind == "done":
            await self._web_ft_done(frame)

    async def _web_savevm(self, msg: dict) -> None:
        """A browser asked to save the LIVE VM to its machine. Inject the request
        into the room (as `_sbx:savevm`, encrypted under the room key) so the host
        sees it and approves via /accept-save, and remember which viewer asked so
        the resulting `hh-snap-vm-*.tar` offer bypasses the browser size cap."""
        vid = msg.get("viewer_id")
        ws = self._room_ws
        if not vid or ws is None:
            return
        self._savevm_requester = vid
        try:
            await ws.send(self.room_fernet.encrypt(
                json.dumps({"_sbx": "savevm"}).encode()).decode())
            await self._post_room_notice(
                "⌂ a web viewer requested a VM save — /accept-save to send the live "
                "state to them (rejects it? /reject-save)"
            )
        except Exception:
            self._savevm_requester = None

    async def _web_offer(self, username: str, frame: dict) -> None:
        """A room member offered a file. Egress it to browsers iff it's room-wide or
        addressed to us (`web-publisher`) and within the browser size cap. A random
        per-offer token stands in for the real room id on the web leg so the relay
        never learns the sender's room username."""
        if username == self.name:
            return  # our own offer echo, if any
        to = frame.get("to")
        if to and to != self.name:
            return  # direct send to a specific human — not for the web
        fid = frame.get("id")
        if not fid:
            return
        size = int(frame.get("size") or 0)
        # The response to a browser's /savevm request: a live VM .tar, which the
        # browser downloads by STREAMING to disk (flat RAM), so the mobile-safety
        # size cap doesn't apply. Recognised by a pending requester + the name the
        # host's save writes. Consumed here so it applies to exactly one offer.
        vm_save = (
            getattr(self, "_savevm_requester", None) is not None
            and str(frame.get("name") or "").startswith("hh-snap-vm-")
        )
        if vm_save:
            self._savevm_requester = None
        if size > self.WEB_FT_MAX and not vm_save:
            # Tell the ROOM, not just our own console. The sender has no other way
            # to learn the browser leg refused: their TUI reports a successful
            # broadcast either way, so a silent return here looks exactly like
            # "offered to everyone, nobody wanted it". Sending a 1.8 GB directory
            # and watching every web viewer stay silent is the case that burned us.
            name = str(frame.get("name") or "file")[:120].replace(": ", " ")
            self.info(f"[web] not offering {frame.get('name')!r} to browsers — "
                      f"{size} B exceeds the {self.WEB_FT_MAX} B web cap")
            await self._post_room_notice(
                f"⚠ {name} ({_human_size(size)}) was NOT sent to browser viewers — over "
                f"the {_human_size(self.WEB_FT_MAX)} web cap. Room members can still /accept it."
            )
            return
        if len(self._web_offers) > 128:          # bound unaccepted-offer growth
            self._web_offers.pop(next(iter(self._web_offers)), None)
        tok = secrets.token_hex(8)
        meta = {
            "id": fid, "tok": tok,
            "name": str(frame.get("name") or "file")[:255],
            "size": size,
            "sha256": str(frame.get("sha256") or ""),
            "dir": bool(frame.get("dir")),
            "from": username,
            "desc": str(frame.get("desc") or "")[:400],
        }
        self._web_offers[tok] = meta
        payload = json.dumps({
            "tok": tok, "name": meta["name"], "size": size,
            "sha256": meta["sha256"], "dir": meta["dir"],
            "from": username, "desc": meta["desc"],
        })
        self._enqueue({"type": "file_offer", "pt": payload.encode()})
        self.info(f"[web] offered {meta['name']!r} ({size} B) from {username} to browsers")

    async def _web_ft_accept(self, msg: dict) -> None:
        """A browser accepted an offer (by its web token). The first accepter makes
        the publisher accept once into the room (starting the sender's stream);
        further browsers that accept before the first chunk join the delivery set."""
        vid, tok = msg.get("viewer_id"), msg.get("tok")
        if not vid or not tok:
            return
        meta = self._web_offers.get(tok)
        if not meta:
            await self._web_ft_error(vid, "that file offer expired — ask the sender to resend")
            return
        fid = meta["id"]
        st = self._web_ft.get(fid)
        if st is None:
            self._web_ft[fid] = {"tok": tok, "viewers": {vid}, "started": False,
                                 "buf": bytearray(), "complete": False}
            await self._room_send_ft_accept(fid)
            self.info(f"[web] #{self._alias_for(vid)} accepted {meta['name']!r} — accepting into room")
        elif not st["started"]:
            st["viewers"].add(vid)
        else:
            # The stream is already running or finished. One viewer accepting must
            # not consume the offer for everyone else, so serve this viewer from the
            # bytes we retained instead of refusing.
            await self._web_ft_serve_retained(vid, st, meta)

    def _web_ft_reject(self, msg: dict) -> None:
        """A browser declined an offer — drop it from the accepter set if the stream
        hasn't started. No room action: other browsers may still want the file."""
        vid, tok = msg.get("viewer_id"), msg.get("tok")
        meta = self._web_offers.get(tok) if tok else None
        if not meta:
            return
        st = self._web_ft.get(meta["id"])
        if st and not st["started"]:
            st["viewers"].discard(vid)

    async def _room_send_ft_accept(self, fid: str) -> None:
        """Post `{"_ft":"accept"}` into the room (as web-publisher) so the sender
        begins streaming chunks. Encrypted with the room key like any room frame."""
        ws = self._room_ws
        if ws is None:
            return
        try:
            frame = json.dumps({"_ft": "accept", "id": fid})
            await ws.send(self.room_fernet.encrypt(frame.encode()).decode())
        except Exception:
            pass

    async def _web_ft_chunk(self, frame: dict) -> None:
        """A file chunk arrived in the room for an offer a browser accepted. Freeze
        the accepter set on the first chunk, then fan the raw bytes to each accepting
        viewer, re-encrypted under K_web (the relay never sees plaintext)."""
        fid = frame.get("id")
        st = self._web_ft.get(fid)
        if not st:
            return  # not a web-accepted transfer → ignore (other members' files)
        b64 = frame.get("data")
        if not b64:
            return
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError):
            return
        st["started"] = True
        # Retain a copy so a browser that accepts later still gets the whole file.
        # Past the per-transfer cap we stop retaining (and drop what we held) — the
        # live fan-out below is unaffected, only re-serving is given up.
        buf = st["buf"]
        if buf is not None:
            if len(buf) + len(raw) > self.WEB_FT_RETAIN_MAX:
                st["buf"] = None
            else:
                buf += raw
        for vid in list(st["viewers"]):
            self._enqueue({"type": "file_chunk", "viewer_id": vid, "pt": raw})

    async def _web_ft_done(self, frame: dict) -> None:
        """The sender finished. Tell each accepting viewer to finalize (assemble the
        Blob + verify the sha256 it already has from the offer) and clean up state."""
        fid = frame.get("id")
        st = self._web_ft.get(fid)
        if not st:
            return
        # Keep the transfer and its offer registered: browsers that have not accepted
        # yet still need the popup to work, and a late accepter is served from `buf`.
        # Dropping both here is what made a second accepter fail.
        st["complete"] = True
        meta = self._web_offers.get(st["tok"]) or {}
        payload = json.dumps({
            "tok": st["tok"], "name": meta.get("name", "file"),
            "sha256": meta.get("sha256", ""), "dir": bool(meta.get("dir")),
        })
        for vid in list(st["viewers"]):
            self._enqueue({"type": "file_done", "viewer_id": vid, "pt": payload.encode()})
        if st["buf"] is None:
            self._web_ft.pop(fid, None)  # nothing retained → nothing to re-serve
        else:
            self._evict_retained()

    async def _web_ft_serve_retained(self, vid: str, st: dict, meta: dict) -> None:
        """Deliver an already-streaming or already-finished transfer to a viewer who
        accepted late, from the bytes we retained. Chunks are re-sliced to the normal
        frame size so one big file cannot land as a single oversized frame.

        If the transfer is still in flight the viewer joins `viewers` after receiving
        the prefix, so the remainder arrives live and in order."""
        buf = st["buf"]
        if buf is None:
            await self._web_ft_error(
                vid, "that file is too large to re-send — ask the sender to resend it")
            return
        for i in range(0, len(buf), self._MAX_CHUNK):
            self._enqueue({"type": "file_chunk", "viewer_id": vid,
                           "pt": bytes(buf[i:i + self._MAX_CHUNK])})
        if st["complete"]:
            payload = json.dumps({
                "tok": st["tok"], "name": meta.get("name", "file"),
                "sha256": meta.get("sha256", ""), "dir": bool(meta.get("dir")),
            })
            self._enqueue({"type": "file_done", "viewer_id": vid, "pt": payload.encode()})
        else:
            st["viewers"].add(vid)
        self.info(f"[web] #{self._alias_for(vid)} accepted {meta.get('name')!r} late "
                  f"— served {len(buf)} B from the retained copy")

    def _evict_retained(self) -> None:
        """Bound total retained bytes, dropping the oldest completed transfers first.
        In-flight transfers are never evicted — their buffer is still being filled and
        viewers may yet join it."""
        total = sum(len(s["buf"]) for s in self._web_ft.values() if s["buf"] is not None)
        if total <= self.WEB_FT_RETAIN_TOTAL:
            return
        for fid, s in list(self._web_ft.items()):   # dicts iterate in insertion order
            if total <= self.WEB_FT_RETAIN_TOTAL:
                break
            if s["complete"] and s["buf"] is not None:
                total -= len(s["buf"])
                self._web_ft.pop(fid, None)

    async def _web_ft_error(self, vid: str, message: str) -> None:
        """Send a targeted file-transfer error to one viewer (undecryptable to the
        relay, like every other web frame)."""
        self._enqueue({"type": "file_error", "viewer_id": vid,
                       "pt": json.dumps({"msg": message[:200]}).encode()})

    # ── uploads: browser → host (docs/plan-web-upload.md) ────────────────
    @staticmethod
    def safe_upload_name(raw: str) -> str:
        """A filename from a browser guest is fully attacker-controlled, so treat it
        as a hostile string that happens to look like a name. Keep only the final
        path component, drop anything that is not plainly safe, and refuse a leading
        dot so an upload cannot land as `.bashrc`. Collisions are the receiver's
        problem (`ft::unique`); this only has to guarantee a basename."""
        name = str(raw or "").replace("\\", "/").split("/")[-1]
        name = "".join(c for c in name if c.isprintable() and c not in '\0:*?"<>|')
        name = name.strip().strip(".").strip()
        keep = [c for c in name if c.isalnum() or c in "._- ()[]"]
        name = "".join(keep).strip()[:96]
        return name or "upload"

    def _upload_decrypt(self, msg: dict) -> dict | None:
        """Decrypt one upload control frame under K_web. Undecryptable means forged
        or corrupt — either way it is not from a holder of the share key, so drop."""
        ct, nonce = msg.get("ct"), msg.get("nonce")
        if not ct or not nonce:
            return None
        try:
            pt = self._keys["file"].decrypt(base64.b64decode(nonce), base64.b64decode(ct), None)
            obj = json.loads(pt)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    async def _upload_error(self, vid: str, tok: str, message: str) -> None:
        self._enqueue({"type": "upload_error", "viewer_id": vid,
                       "pt": json.dumps({"tok": tok[:64],
                                         "msg": message[:200]}).encode()})

    async def _web_upload_offer(self, msg: dict) -> None:
        """A browser guest wants to send a file to the host. Nothing is written and
        nothing is even received yet: this only synthesises a room `_ft` offer so the
        host's existing `/accept` prompt becomes the gate. Bytes do not leave the
        browser until that offer is accepted."""
        # Every refusal below is logged. An upload that vanishes with nothing on
        # either screen is the worst failure this feature has: the guest sees a
        # stalled offer, the host sees nothing at all, and there is no thread to
        # pull. The frames are ciphertext by design, so this log is the ONLY place
        # the operator can learn a guest tried at all — silence here cost a whole
        # debugging session on 2026-07-26. Log the reason, never the content.
        vid, tok = msg.get("viewer_id"), str(msg.get("tok") or "")[:64]
        if not vid or not tok:
            self.info("[web] ignored an upload offer with no viewer/token — "
                      "malformed or not from this relay")
            return
        if not self.allow_uploads:
            # Off by default. Say so plainly rather than failing silently — a guest
            # staring at a dead progress bar will just retry, and the operator needs
            # to know the feature exists to turn it on.
            await self._upload_error(vid, tok, "the host has not enabled uploads")
            self.info(f"[web] refused upload from #{self._alias_for(vid)} "
                      f"— uploads disabled (start with --allow-uploads)")
            return
        obj = self._upload_decrypt(msg)
        if obj is None:
            # Undecryptable under K_web: forged, corrupt, or a viewer holding a key
            # from a previous share. Worth saying out loud — it is indistinguishable
            # from "nothing happened" otherwise, and it is the shape an attack takes.
            self.info(f"[web] dropped an undecryptable upload offer from "
                      f"#{self._alias_for(vid)} — wrong share key or forged")
            return
        self._sweep_uploads()
        key = f"{vid}:{tok}"
        if key in self._uploads:
            return  # duplicate offer for a transfer already staged
        size = obj.get("size")
        size = int(size) if isinstance(size, int) and not isinstance(size, bool) else -1
        if size <= 0 or size > self.WEB_UPLOAD_MAX:
            await self._upload_error(
                vid, tok, f"file too large — the limit is {self.WEB_UPLOAD_MAX // (1024 * 1024)} MB")
            self.info(f"[web] refused upload from #{self._alias_for(vid)} — size {size} B "
                      f"outside 1..{self.WEB_UPLOAD_MAX} B")
            return
        mine = sum(1 for u in self._uploads.values() if u["vid"] == vid)
        if mine >= self.WEB_UPLOAD_PER_VIEWER:
            await self._upload_error(vid, tok, "finish or cancel your current upload first")
            self.info(f"[web] refused upload from #{self._alias_for(vid)} — already has "
                      f"{mine} in flight (limit {self.WEB_UPLOAD_PER_VIEWER})")
            return
        if len(self._uploads) >= self.WEB_UPLOAD_PER_ROOM:
            await self._upload_error(vid, tok, "too many uploads in flight — try again shortly")
            self.info(f"[web] refused upload from #{self._alias_for(vid)} — room is at "
                      f"{self.WEB_UPLOAD_PER_ROOM} uploads in flight")
            return
        sha = str(obj.get("sha256") or "").lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            await self._upload_error(vid, tok, "malformed upload")
            self.info(f"[web] refused upload from #{self._alias_for(vid)} — "
                      f"missing or malformed SHA-256")
            return
        name = self.safe_upload_name(obj.get("name"))
        fid = "web-" + secrets.token_hex(8)
        self._uploads[key] = {
            "vid": vid, "tok": tok, "fid": fid, "name": name, "size": size,
            "sha256": sha, "buf": bytearray(), "accepted_by": None,
            "created": time.monotonic(),
        }
        self._upload_by_fid[fid] = key
        handle = self._web_viewers.get(vid) or self._web_handle(vid)
        await self._room_send_upload_offer(fid, name, size, sha, vid, handle)
        self.info(f"[web] #{self._alias_for(vid)} offers {name!r} ({size} B) to the room "
                  f"— awaiting a host /accept")

    async def _room_send_upload_offer(self, fid: str, name: str, size: int,
                                      sha: str, vid: str, handle: str) -> None:
        """Post the guest's offer into the room as a normal `_ft` offer, so it lands
        in the host's ordinary /accept prompt with no new approval UI.

        Two fields are not negotiable and are set here rather than copied from the
        browser: `web: true`, which keeps the accepted file off the sandbox
        auto-bridge and the .ova auto-import (hh/src/app.rs), and `dir: false`,
        which stops it being tar-extracted into many files. The offer is unsigned —
        browsers hold no Ed25519 persona — so the TUI will correctly print
        "(unsigned — no attribution proof)". That stays true; `desc` names the guest
        so the host knows *which* guest without the signature line lying about what
        has actually been proven."""
        ws = self._room_ws
        if ws is None:
            return
        frame = json.dumps({
            "_ft": "offer", "id": fid, "name": name, "size": size,
            "sha256": sha, "dir": False, "web": True,
            "desc": f"upload from web guest #{self._alias_for(vid)} “{handle}”"[:400],
        })
        try:
            await ws.send(self.room_fernet.encrypt(frame.encode()).decode())
        except Exception:
            pass

    def _drop_upload(self, key: str) -> dict | None:
        st = self._uploads.pop(key, None)
        if st is not None:
            self._upload_by_fid.pop(st["fid"], None)
        return st

    async def _web_upload_cancel(self, msg: dict) -> None:
        """The guest aborted. Drop the staged bytes; the room offer simply goes
        unanswered, which is already a state the host's prompt handles."""
        vid, tok = msg.get("viewer_id"), str(msg.get("tok") or "")[:64]
        if not vid or not tok:
            return
        st = self._drop_upload(f"{vid}:{tok}")
        if st is not None:
            self.info(f"[web] #{self._alias_for(vid)} cancelled the upload of {st['name']!r}")

    def _sweep_uploads(self) -> None:
        """Retire staged uploads that have gone quiet. Checked lazily whenever a new
        offer arrives, which is the only moment a stale slot can actually hurt
        anyone — no background task, and the slots are bounded anyway.

        The clock restarts on accept: waiting for a distracted host is a different
        thing from a stream that stalled, and the host may reasonably take a while
        to look at their terminal."""
        now = time.monotonic()
        for key, st in list(self._uploads.items()):
            ttl = (self.WEB_UPLOAD_STREAM_TTL if st["accepted_by"]
                   else self.WEB_UPLOAD_OFFER_TTL)
            if now - st["created"] > ttl:
                self._drop_upload(key)
                self.info(f"[web] upload of {st['name']!r} timed out after {ttl:.0f}s")

    def _drop_uploads_for_viewer(self, vid: str) -> None:
        """A guest left — nothing it staged can ever complete, so free it now
        rather than waiting for a timeout to notice."""
        for key in [k for k, u in self._uploads.items() if u["vid"] == vid]:
            self._drop_upload(key)

    async def _upload_answered(self, kind: str, username: str, frame: dict) -> None:
        """A room member answered a guest's upload offer. `username` is the
        server-authenticated sender, so it is safe to address the stream at it.

        Accept unblocks the browser (`upload_ack`) — this is the moment, and the
        only moment, at which the guest's bytes are allowed to move. The first
        accepter wins: an upload is one guest handing one file to one person, not
        a room-wide publication, so a second accept is answered with nothing
        rather than starting a second stream out of a browser that is already
        sending."""
        key = self._upload_by_fid.get(frame.get("id"))
        if key is None:
            return                                  # not one of ours
        st = self._uploads.get(key)
        if st is None:
            return
        if kind == "reject":
            self._drop_upload(key)
            self._enqueue({"type": "upload_reject", "viewer_id": st["vid"],
                           "pt": json.dumps({"tok": st["tok"]}).encode()})
            self.info(f"[web] {username} declined the upload of {st['name']!r}")
            return
        if st["accepted_by"] is not None:
            return
        st["accepted_by"] = username
        st["created"] = time.monotonic()          # the stream clock starts here
        self._enqueue({"type": "upload_ack", "viewer_id": st["vid"],
                       "pt": json.dumps({"tok": st["tok"]}).encode()})
        self.info(f"[web] {username} accepted {st['name']!r} — guest may now send")

    def _upload_for(self, msg: dict) -> tuple[str, dict] | None:
        """Locate the staged upload a browser frame refers to, keyed by the
        server-side viewer_id so one guest can never drive another's transfer."""
        vid, tok = msg.get("viewer_id"), str(msg.get("tok") or "")[:64]
        if not vid or not tok:
            return None
        key = f"{vid}:{tok}"
        st = self._uploads.get(key)
        return None if st is None else (key, st)

    async def _web_upload_chunk(self, msg: dict) -> None:
        """A chunk of the guest's file. Decrypt it out of K_web and hand it
        straight into the room under the room key, addressed to the one member who
        accepted. Nothing is buffered: the publisher is a re-encryption point, not
        a staging area, so a 25 MB upload costs one chunk of RAM rather than 25 MB
        and the receiver's disk-backed sink does the accumulating it already knows
        how to do.

        The declared size is enforced on the way through. A guest can lie about
        `size` or simply keep streaming, and the offer the host accepted named a
        number — so exceeding it aborts the transfer here rather than letting the
        receiver's own cap discover it later.

        So is the order. The browser numbers every chunk inside the ciphertext,
        but this used to append in arrival order and ignore that number, which
        left chunk ordering resting entirely on TCP — and on the relay, which is
        untrusted by design and can duplicate or reorder frames at will. The
        damage was bounded (the guest hashed the file on its own device, so the
        host's SHA-256 check discards a scrambled result rather than saving it),
        but only after up to 25 MB had crossed the wire, and the guest was told
        nothing that distinguished tampering from an ordinary failure. Check the
        number we are already being sent, and fail loudly at the first bad chunk
        instead of silently at the last."""
        found = self._upload_for(msg)
        if found is None:
            return
        key, st = found
        if st["accepted_by"] is None:
            return          # bytes before an /accept — the gate has not opened
        obj = self._upload_decrypt(msg)
        if obj is None:
            return
        got = obj.get("seq")
        want = st.get("seq", 0) + 1
        if not isinstance(got, int) or isinstance(got, bool) or got != want:
            self._drop_upload(key)
            await self._upload_error(st["vid"], st["tok"],
                                     "upload arrived out of order — please try again")
            self.info(f"[web] aborted upload of {st['name']!r} — chunk {got!r} arrived "
                      f"where {want} was expected (duplicated, reordered, or tampered)")
            return
        # Past the sequence check, a chunk we cannot use is a HOLE, not something
        # to skip: dropping it here would hand the receiver a short file that
        # fails its SHA-256 with no explanation on either screen. Every exit below
        # therefore ends the transfer and says why.
        try:
            data = base64.b64decode(obj.get("data") or "", validate=True)
        except Exception:
            data = b""
        if not data:
            self._drop_upload(key)
            await self._upload_error(st["vid"], st["tok"], "upload was corrupted in transit")
            self.info(f"[web] aborted upload of {st['name']!r} — chunk {want} carried no "
                      f"usable data")
            return
        st["received"] = st.get("received", 0) + len(data)
        if st["received"] > st["size"]:
            self._drop_upload(key)
            await self._upload_error(st["vid"], st["tok"],
                                     "upload exceeded the size you declared")
            self.info(f"[web] aborted upload of {st['name']!r} — overran its declared size")
            return
        ws = self._room_ws
        if ws is None:
            self._drop_upload(key)
            await self._upload_error(st["vid"], st["tok"],
                                     "the host went offline mid-transfer — try again")
            self.info(f"[web] aborted upload of {st['name']!r} — no room socket to "
                      f"forward it to")
            return
        # Same counter, deliberately: inbound chunk N becomes room chunk N, so the
        # order the guest sent in is the order the receiver reassembles in.
        st["seq"] = want
        out = json.dumps({"_ft": "chunk", "id": st["fid"], "to": st["accepted_by"],
                          "seq": st["seq"],
                          "data": base64.b64encode(data).decode()})
        try:
            await ws.send(self.room_fernet.encrypt(out.encode()).decode())
        except Exception:
            pass

    async def _web_upload_done(self, msg: dict) -> None:
        """The guest finished sending. Close the room-side stream and let the
        receiver do what it already does for every transfer: verify SHA-256 against
        the digest in the offer and discard on mismatch. The publisher deliberately
        does not compute or vouch for that digest — the browser hashed the file it
        actually holds, so the integrity boundary stays on the guest's device and
        a corrupted upload cannot verify clean by passing through here."""
        found = self._upload_for(msg)
        if found is None:
            return
        key, st = found
        if st["accepted_by"] is None:
            return
        self._drop_upload(key)
        ws = self._room_ws
        if ws is None:
            return
        out = json.dumps({"_ft": "done", "id": st["fid"], "to": st["accepted_by"]})
        try:
            await ws.send(self.room_fernet.encrypt(out.encode()).decode())
        except Exception:
            pass
        self._enqueue({"type": "upload_progress", "viewer_id": st["vid"],
                       "pt": json.dumps({"tok": st["tok"], "sent": st.get("received", 0),
                                         "complete": True}).encode()})
        self.info(f"[web] upload of {st['name']!r} delivered to {st['accepted_by']} "
                  f"({st.get('received', 0)} B) — awaiting their SHA-256 check")

    async def _read_from_relay(self, ws) -> None:
        """Read control/input frames the relay routes back from browsers: drive
        requests, releases, encrypted keystrokes, and chat. The relay stays blind —
        it only tags each with the server-side viewer_id."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            t = msg.get("type")
            if t == "in":
                await self._forward_input(msg)
            elif t == "chat_in":
                await self._post_chat(msg)
            elif t == "name_in":
                await self._apply_web_name(msg)
            elif t == "file_accept":
                await self._web_ft_accept(msg)
            elif t == "file_reject":
                self._web_ft_reject(msg)
            elif t == "savevm":
                await self._web_savevm(msg)
            elif t == "upload_offer":
                await self._web_upload_offer(msg)
            elif t == "upload_chunk":
                await self._web_upload_chunk(msg)
            elif t == "upload_done":
                await self._web_upload_done(msg)
            elif t == "upload_cancel":
                await self._web_upload_cancel(msg)
            elif t == "host_cmd":
                # Operator command from the host web console (relayed). Same effect
                # as the stdin console; approval still can't bypass the room ACL.
                cmd = msg.get("cmd")
                if cmd == "allow" and msg.get("viewer_id"):
                    self._apply_allow(msg["viewer_id"])
                elif cmd == "revoke":
                    self._apply_revoke()
                elif cmd == "sync":
                    self._emit_host_state()
            elif t == "viewer_joined":
                vid = msg.get("viewer_id")
                if vid:
                    a = self._alias_for(vid)
                    # Restore a name this viewer already chose rather than resetting to
                    # the pseudonym: a phone that slept and came back reclaims its
                    # viewer_id, and overwriting here is what made it return as
                    # `web-xxxx` despite having set a name.
                    known = self._web_names.get(vid)
                    self._web_viewers[vid] = known or self._web_handle(vid)
                    if not msg.get("reconnect"):
                        cnt = msg.get("count")
                        await self._post_room_notice(
                            f"🌐 web viewer #{a} joined (view-only) — {cnt} watching · /web list")
                    if known:
                        await self._broadcast_web_presence()   # no race to lose
                    else:
                        # Unknown id: its name, if it has one, is one round trip
                        # behind. Hold the roster so the host sees the name first
                        # rather than a `web-xxxx` that renames itself.
                        self._schedule_settle(vid)
                    self._emit_host_state()
            elif t == "drive_request":
                vid = msg.get("viewer_id")
                if vid:
                    self._pending_requests.add(vid)
                    self._emit_host_request(vid)      # → host console notification
                    self._emit_host_state()
                    a = self._alias_for(vid)
                    # Surface the request in the owner's TUI chat so they can approve
                    # inline with `/web allow <n>` — no host-console link required.
                    await self._post_room_notice(
                        f"🔔 web viewer #{a} ({vid}) asks to drive — "
                        f"owner types  /web allow {a}  or  /web deny {a}")
                    self.info(f"[web] viewer {vid} (#{a}) requests drive — owner: "
                              f"/web allow {a} (TUI) · host console · stdin /web allow-input {vid}")
            elif t in ("drive_release", "viewer_left"):
                vid = msg.get("viewer_id")
                if vid:
                    self._pending_requests.discard(vid)
                if t == "viewer_left" and vid:
                    # The guest is gone — drop it from the roster the host sees.
                    self._cancel_settle(vid)   # left inside the settle window
                    self._web_viewers.pop(vid, None)
                    self._drop_uploads_for_viewer(vid)
                    # Free the label, but KEEP the vsid→ctr watermark: a departed
                    # viewer's frames are exactly what a relay would replay, and
                    # forgetting the counter would make them fresh again.
                    gone = self._vsid_by_vid.pop(vid, None)
                    if gone is not None:
                        self._vid_by_vsid.pop(gone, None)
                if vid and vid == self.approved_viewer:
                    self.approved_viewer = None
                    self._approved_vsid = None
                    self._recompute_driver()
                    self.info(f"[web] drive released by {vid} — now view-only")
                else:
                    self._emit_host_state()
                if t == "viewer_left":
                    await self._broadcast_web_presence()

    # ── room side: tap _sbx:data ─────────────────────────────────────────
    async def _room_loop(self) -> None:
        """Join the room and drain frames forever, reconnecting on any drop —
        mirrors AgentBridge.run_async (server frees our session on a drop, so we
        re-run SRP each attempt)."""
        backoff = 1.0
        while True:
            try:
                self.srp_authenticate()
                url = (f"{self.ws_url}/ws/chat?user_id={self.user_id}"
                       f"&ws_token={self.ws_token}")
                async with websockets.connect(url, ssl=self._ws_ssl_context(),
                                              max_size=MAX_WS_FRAME) as ws:
                    self._room_ws = ws
                    self.success("web publisher joined room — tapping _sbx:data")
                    backoff = 1.0
                    # Repaint the web-guest roster so a TUI that joined while
                    # viewers were already watching (or after a room blip)
                    # converges — otherwise it sees no guests until the next
                    # join/leave. Empty when nobody is watching, which is fine.
                    await self._broadcast_web_presence()
                    async for raw in ws:
                        await self._handle_room_frame(raw)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001 — reconnect, don't die
                self.info(f"room connection lost ({type(e).__name__}); reconnecting…")
            finally:
                # Room gone → we can't hold a driver token; stop driving until the
                # broker re-broadcasts `_perm:acl` (fail-closed, never fail-open).
                self._room_ws = None
                self.granted = False
                self._recompute_driver()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._RECONNECT_MAX_BACKOFF)

    async def _handle_room_frame(self, raw) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if data.get("type") == "user_joined":
            # A native member just joined the room. The web-guest overlay is
            # broadcast live-only and is NOT replayed from history (the TUI skips
            # non-live `_web:presence` frames on its init snapshot), so a host that
            # joins *after* browser viewers are already connected would otherwise
            # never see them. Re-emit a fresh live presence frame so the newcomer's
            # roster converges on the current web guests + driver.
            await self._broadcast_web_presence()
            return
        if data.get("type") != "message":
            return
        msg = self.decrypt_message(data.get("data", {}))
        text = msg.get("text", "")
        if not text or text == "[decrypt failed]":
            return
        # Bound for BOTH paths: control frames need the sender too (`_tap_ft` labels
        # the browser's file-offer popup with it). Binding it only in the chat branch
        # made every `_ft` frame raise UnboundLocalError, which killed the room socket
        # mid-transfer — the offer never reached browsers and the publisher visibly
        # left and rejoined the room on each /send.
        username = msg.get("username", "?")
        if not text.startswith('{"_'):
            # In-room `/web …` operator channel — consumed here (never egressed to
            # web). Read-only queries (link/help) answer any member; drive-control
            # (allow/deny/revoke/list) is authorised strictly against the acl
            # `owner` inside `_handle_room_command` (fails closed until the acl is
            # seen, and never bypasses Gate A).
            stripped = text.lstrip()
            if stripped.startswith("/web "):
                is_owner = (self.room_owner is not None and username == self.room_owner)
                await self._handle_room_command(text.strip(), is_owner)
                return
            # `/share` (and `/link`) — a discoverable alias for `/web link`: reprint
            # the browser share link for this web-published room. Read-only, any
            # member (same posture as `/web link`), routed through the same handler.
            _parts = stripped.split()
            if _parts and _parts[0] in ("/share", "/link"):
                await self._handle_room_command("/web link", False)
                return
            # Plain room chat (not a _sbx/_perm/_ft control frame) → egress to the
            # web (encrypted under K_web). This is the operator-enabled read+send
            # chat (spec §6): it widens egress beyond the terminal, by design.
            self._tap_chat(username, text)
            return
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        # Driver-token ACL broadcast (`_perm:acl`) — Gate A. Our input reaches the
        # PTY only while our room username is in the broker's `drivers` set; a TUI
        # force-grab clears it, which drops browser input immediately (spec §6).
        #
        # AUTHENTICATED (2026-08-04). This handler previously accepted an acl from
        # ANY room member, and the frame's own `owner` field was the only thing that
        # set `self.room_owner`. That made both PTY gates fall to one forged frame:
        # `{"_perm":"acl","owner":"mallory","drivers":["web-publisher"]}` flipped
        # Gate A on AND installed the sender as the owner who may drive the `/web`
        # channel (Gate B) at :1562. Room membership is full trust per SECURITY.md,
        # but the driver-token ACL is *intra-room* privilege separation — honoring a
        # forged one hands the PTY to a mere link holder, which is the "add no new
        # bypass path" invariant breaking.
        #
        # Two checks, both cheap:
        #   1. SELF-CONSISTENCY — the sender must BE the owner it claims. Kills
        #      "owner: <someone else>" outright.
        #   2. TRUST-ON-FIRST-USE PIN — once an owner is pinned, no other member can
        #      displace them. `hh/src/app.rs:931` has the real owner broadcasting an
        #      acl as soon as the sandbox starts, long before a web guest can exist,
        #      so in practice the pin is taken by the legitimate owner.
        # This does not make a hostile *room member* impossible — it makes them race
        # the owner's first broadcast instead of forging at will, which is the right
        # level given the declared threat model.
        if frame.get("_perm") == "acl":
            owner = frame.get("owner") or None
            if owner is None or username != owner:
                self.error(f"ignored _perm:acl from {username!r} claiming owner "
                           f"{owner!r} — sender is not the owner it names")
                return
            if self.room_owner is not None and username != self.room_owner:
                self.error(f"ignored _perm:acl from {username!r} — room owner is "
                           f"pinned to {self.room_owner!r} for this session")
                return
            if self.room_owner is None:
                self.room_owner = owner
            was = self.granted
            self.granted = self.name in (frame.get("drivers") or [])
            if self.granted != was:
                self._recompute_driver()
            return
        if frame.get("_ft"):
            await self._tap_ft(username, frame)
            return
        kind = frame.get("_sbx")
        if kind == "data":
            # Decrypted PTY chunk (base64 raw bytes on the room wire). Accumulate
            # into the coalescing buffer + rolling screen tail; `_coalesce_loop`
            # flushes bounded `out` frames and `_snapshot_loop` mints snapshots.
            b64 = frame.get("b64")
            if b64:
                try:
                    raw = base64.b64decode(b64)
                except (ValueError, TypeError):
                    return
                # PTY output *is* proof of life. We may never see the `_sbx:status`
                # that started a sandbox — it is sent once, so joining a room with
                # one already running leaves us defaulted to "not live". Promote on
                # data (before buffering, since the transition clears the tail).
                self._set_sbx_live(True)
                self._pending += raw
                self._screen += raw
                if len(self._screen) > self._SNAPSHOT_BYTES:
                    del self._screen[:-self._SNAPSHOT_BYTES]
        elif kind in ("status", "resize"):
            if kind == "status":
                self._set_sbx_live(str(frame.get("state") or "") == "ready")
            cols = int(frame.get("cols") or self.cols)
            rows = int(frame.get("rows") or self.rows)
            if (cols, rows) != (self.cols, self.rows):
                self.cols, self.rows = cols, rows
                self._enqueue({"type": "resize", "cols": cols, "rows": rows})

    # ── relay side: publish WSS ──────────────────────────────────────────
    @staticmethod
    def _close_code(exc: BaseException) -> int | None:
        """Websocket close code from a ConnectionClosed, across websockets versions
        (`.rcvd` since 10.x; `.code` on older/other paths)."""
        rcvd = getattr(exc, "rcvd", None)
        code = getattr(rcvd, "code", None)
        return code if code is not None else getattr(exc, "code", None)

    async def _reprovision(self) -> bool:
        """The relay no longer recognises our slug — mint a new room and say so.

        The relay closes /pub with 4401 for both a bad token and an unknown room, and
        an unknown room is the common case: it reaps a publisher-less room after
        RELAY_IDLE_TIMEOUT, so any disconnect longer than that (or a relay restart)
        leaves us re-dialling a slug that will refuse us forever. Recovering means
        registering a fresh room.

        The old share URL cannot be revived: the relay dropped the room's RAM state on
        purpose, and we mint a fresh K_web so a holder of the dead link can never read
        the new stream. Announce the replacement in room chat — a silently-changed URL
        is what makes this look like "the web page just stopped working"."""
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.register_room)
        except Exception as e:  # noqa: BLE001 — stay in the reconnect loop and retry
            self.info(f"relay room re-registration failed ({type(e).__name__}); retrying…")
            return False
        self._zeroise_key()   # the old K_web is dead the moment the new one exists
        self.k_web = bytearray(os.urandom(32))
        # Nothing encrypts under K_web itself — see webkdf: one key per channel.
        self._keys = derive_web_keys(bytes(self.k_web))
        # B2 identity state is scoped to the old room's viewer_ids and to a key that
        # no longer decrypts anything, so it must not carry over: a stale approved
        # vsid would be a pin nobody can present, and stale counters would reject
        # the first frames from viewers arriving on the new link.
        self._approved_vsid = None
        self._vsid_by_vid.clear()
        self._vid_by_vsid.clear()
        self._in_ctr.clear()
        # Viewer state is per-room and the old room is gone: forget the approval and
        # the roster so Gate B is re-earned rather than inherited by whoever lands on
        # the new link. Gate A (`self.granted`) belongs to the room, so it stands.
        self.approved_viewer = None
        self._pending_requests.clear()
        for vid in list(self._settle_tasks):     # every viewer_id just died with the room
            self._cancel_settle(vid)
        self._web_viewers.clear()
        # `_web_names` deliberately survives: it is keyed by viewer_id and the new room
        # mints new ones, so the stale entries simply age out under _WEB_NAMES_MAX —
        # but clearing here would also throw away a name a browser has *already*
        # re-announced against its new id during the reconnect.
        self._alias_by_vid.clear()
        self._vid_by_alias.clear()
        self._effective_driver = None
        # Point at `/web link` in the same breath: this notice is the one thing a
        # host needs and the one thing that scrolls away, and the guest side now
        # says "ask the host for a new link" — so the host has to be able to
        # produce it later without knowing that command already exists.
        self._emit_share_url("relay room was gone — new share URL:")
        self.info("  (re-print it any time with `/web link`, or `/web qr`)")
        # Deliberately NOT the URL. `_post_room_notice` goes into room chat, which
        # the server persists (`cmd_chat/server/stores.py`), so broadcasting the
        # freshly-rotated key here wrote every K_web this room ever used into a
        # durable transcript — defeating the point of rotating in the first place.
        # Members can pull the new link on demand with `/web link`.
        await self._post_room_notice(
            "⌂ web link changed (relay room expired) — the previous link is dead. "
            "Run `/web link` for the new one.")
        return True

    async def _relay_loop(self) -> None:
        """Hold the outbound publish WSS and drain the out-queue to the relay,
        reconnecting on any drop. Re-sends `hello` on each (re)connect so a
        reconnecting relay relearns the dims."""
        backoff = 1.0
        while True:
            # Rebuilt each pass: _reprovision may have replaced slug + publish_token.
            url = f"{self._relay_ws_base}/pub/{self.slug}"
            try:
                async with websockets.connect(
                        url, ssl=self._relay_ssl_context(),
                        ping_interval=self._PING_INTERVAL,
                        ping_timeout=self._PING_TIMEOUT) as ws:
                    # WIRE_PROTO 6: the publish token goes in frame one, not in the
                    # URL. A `?token=` is a bearer credential written into every
                    # access log and proxy trace on the path to the relay.
                    await ws.send(json.dumps(
                        {"type": "auth", "token": self.publish_token or ""}))
                    await ws.send(json.dumps(
                        {"type": "hello", "proto": 1, "cols": self.cols, "rows": self.rows}))
                    self.success("connected to relay (publish)")
                    backoff = 1.0
                    # Re-assert the current driver on (re)connect so a reconnecting
                    # relay relearns the roster; then run send + receive together.
                    self._effective_driver = None
                    self._recompute_driver()
                    drain = asyncio.create_task(self._drain_to_relay(ws))
                    read = asyncio.create_task(self._read_from_relay(ws))
                    try:
                        done, pending = await asyncio.wait(
                            {drain, read}, return_when=asyncio.FIRST_COMPLETED)
                        for t in pending:
                            t.cancel()
                        for t in done:
                            t.result()  # surface a real error to reconnect logic
                    finally:
                        for t in (drain, read):
                            t.cancel()
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001
                if self._close_code(e) == 4401:
                    # Unknown room (reaped/relay restarted) or stale token — retrying
                    # this slug can only ever be refused again, so re-register first.
                    # Still falls through to the sleep below: if a fresh room is also
                    # refused, that bounds us to one room per second instead of a hot
                    # loop hammering the relay's create endpoint.
                    if await self._reprovision():
                        backoff = 1.0
                else:
                    self.info(f"relay connection lost ({type(e).__name__}); reconnecting…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._RECONNECT_MAX_BACKOFF)

    async def _drain_to_relay(self, ws) -> None:
        while True:
            frame = await self._out_q.get()
            ftype = frame.get("type")
            if ftype == "out":
                self._seq += 1
                # AES-256-GCM under K_web, fresh 96-bit nonce per frame (never
                # reused). Relay gets ciphertext + nonce only — no key, no plaintext.
                # AAD binds slug|kind|seq, so the relay can no longer renumber a
                # frame to push the browser's `lastSeq` past the rest of the
                # transcript, nor pass an `out` off as a `snapshot`.
                nonce = os.urandom(12)
                ct = self._keys["out"].encrypt(nonce, frame["pt"], self._aad("out", self._seq))
                frame = {
                    "type": "out", "seq": self._seq,
                    "ct": base64.b64encode(ct).decode(),
                    "nonce": base64.b64encode(nonce).decode(),
                }
            elif ftype == "snapshot":
                # Current-screen replay blob, same K_web, fresh nonce. `seq` marks
                # how far it has caught up (opaque to the relay).
                snap_seq = int(frame.get("seq") or self._seq)
                nonce = os.urandom(12)
                ct = self._keys["out"].encrypt(nonce, frame["pt"], self._aad("snapshot", snap_seq))
                frame = {
                    "type": "snapshot", "seq": snap_seq,
                    "ct": base64.b64encode(ct).decode(),
                    "nonce": base64.b64encode(nonce).decode(),
                }
            elif ftype == "chat":
                # Chat message {from,text,ts} → AES-256-GCM under K_web, fresh
                # nonce. Relay sees ciphertext only; it cannot read author or text.
                nonce = os.urandom(12)
                ct = self._keys["chat"].encrypt(nonce, frame["pt"], None)
                frame = {
                    "type": "chat", "seq": int(frame.get("seq") or 0),
                    "ct": base64.b64encode(ct).decode(),
                    "nonce": base64.b64encode(nonce).decode(),
                }
            elif ftype in ("file_offer", "file_chunk", "file_done", "file_error",
                           "upload_ack", "upload_reject", "upload_error",
                           "upload_progress"):
                # File frames in both directions, AES-256-GCM under K_web, fresh
                # nonce. The relay sees ciphertext only. `file_offer` fans out to
                # all viewers; everything else carries a `viewer_id` so the relay
                # targets the one guest concerned — an upload's fate is that
                # guest's business and nobody else's.
                self._ft_seq += 1
                nonce = os.urandom(12)
                ct = self._keys["file"].encrypt(nonce, frame["pt"], None)
                out = {
                    "type": ftype, "seq": self._ft_seq,
                    "ct": base64.b64encode(ct).decode(),
                    "nonce": base64.b64encode(nonce).decode(),
                }
                if frame.get("viewer_id"):
                    out["viewer_id"] = frame["viewer_id"]
                frame = out
            await ws.send(json.dumps(frame))

    # ── operator console (stdin) ─────────────────────────────────────────
    async def _console_loop(self) -> None:
        """Minimal operator REPL. `/web allow-input <viewer_id>` is the ONLY way a
        browser gets to drive — and even then only while the room's driver token is
        held (Gate A). Read-only is the default; there is no auto-approve."""
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if line == "":  # stdin EOF (non-interactive / piped) — stop the REPL
                return
            cmd = line.strip()
            if not cmd:
                continue
            self._handle_console(cmd)

    def _handle_console(self, cmd: str) -> None:
        parts = cmd.split()
        if cmd.startswith("/web allow-input"):
            if len(parts) >= 3:
                self._apply_allow(parts[2])
            else:
                self.error("usage: /web allow-input <viewer_id>")
        elif cmd.startswith("/web revoke"):
            self._apply_revoke()
        elif cmd.startswith("/web viewers"):
            self.info(f"[web] approved={self.approved_viewer} granted={self.granted} "
                      f"driver={self._effective_driver} pending={sorted(self._pending_requests)}")
        elif cmd.startswith("/web link") or cmd in ("/share", "/link"):
            self._emit_share_url("share URL (view-only):")
        elif cmd.startswith("/web qr"):
            self._print_qr()
        elif cmd.startswith("/web host"):
            self.success(self.host_url())
        else:
            self.error("commands: /web allow-input <viewer_id> | /web revoke | "
                       "/web viewers | /web link (=/share) | /web qr | /web host")

    def _print_qr(self) -> None:
        """Render the share URL as a terminal block-QR via `qrencode` (ANSIUTF8), so
        a phone can scan it straight from the REPL. The URL carries the #k read-key —
        it is a bearer capability, so only scan/share it over trusted channels.

        The URL is fed on STDIN, never argv: as an argument it sat in
        `/proc/<pid>/cmdline` for the life of the child, so any local user running
        `ps aux` at the right moment read the end-to-end key straight off the
        process table. qrencode reads from stdin when given no positional argument.

        A QR is a picture of the key, so this is TTY-only for the same reason
        `_emit_share_url` is — there is no sense rendering one into a log file."""
        if not sys.stdout.isatty():
            self.error("`/web qr` needs a terminal — stdout is being captured and a "
                       "QR of the #k key must not land in a log.")
            return
        if shutil.which("qrencode") is None:
            self.error("qrencode not found — install it, or use `/web link` and the "
                       "browser QR button, or `hh-web-relay qr`.")
            self._emit_share_url("share URL (view-only):")
            return
        try:
            out = subprocess.run(
                ["qrencode", "-t", "ANSIUTF8", "-m", "1"],
                input=self.share_url(),
                capture_output=True, text=True, timeout=10)
        except Exception as e:  # noqa: BLE001 — never let the REPL die on this
            self.error(f"qrencode failed ({type(e).__name__}) — use `/web link`.")
            return
        if out.returncode != 0:
            self.error("qrencode failed — use `/web link`.")
            return
        self.console.print(out.stdout)
        self._emit_share_url("share URL (view-only):")


def generate_pin(digits: int = PIN_MIN_DIGITS) -> str:
    """A uniformly random numeric PIN. Offered because an operator asked for a
    memorable number will produce a birthday or a repdigit, and the offline attack
    on a leaked salt+pubkey pair is a dictionary attack before it is a brute force —
    so a hand-picked PIN can be far weaker than its digit count suggests."""
    return "".join(secrets.choice("0123456789") for _ in range(digits))


def _resolve_pin(pin: str | None) -> str | None:
    """Validate a PIN from `--pin` / `HH_WEB_PIN`, or mint one for 'random'.

    Enforced publisher-side only: the relay receives a public key and cannot tell a
    7-digit PIN from a 1-digit one. Refuse rather than warn — a room silently
    running a weak PIN is exactly the outcome the length rule exists to prevent."""
    if pin is None:
        return None
    pin = pin.strip()
    if pin.lower() == "random":
        pin = generate_pin()
        print(f"[web] generated room PIN: {pin}  "
              f"(say it out loud — it is not in the share link)", file=sys.stderr)
        return pin
    if not pin.isdigit() or len(pin) < PIN_MIN_DIGITS:
        ap_err = (f"--pin must be at least {PIN_MIN_DIGITS} digits "
                  f"(got {len(pin)}). A short PIN is recoverable offline from a "
                  f"compromised relay. Use --pin random to generate one.")
        raise SystemExit(f"error: {ap_err}")
    return pin


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="cmd_chat.web",
        description="hack-house web publisher — egress a room's terminal to the relay (P0)",
    )
    ap.add_argument("server", help="room host")
    ap.add_argument("port", type=int, help="room port")
    ap.add_argument("--name", default="web-publisher", help="room display name")
    ap.add_argument("--password", "-p", default=None, help="room password")
    ap.add_argument("--relay", default="http://127.0.0.1:8080",
                    help="relay base URL (default %(default)s); override via HH_WEB_RELAY_URL")
    ap.add_argument("--public-url", default=None,
                    help="base URL to put in the share/host links when it differs from "
                         "--relay (e.g. dial http://127.0.0.1:8090 but hand out "
                         "https://relay.example.com). Keeps our own publish socket off a "
                         "flapping tunnel; override via HH_WEB_PUBLIC_URL")
    ap.add_argument("--label", default="hack-house room",
                    help="public operator label shown in the lobby (no room content)")
    ap.add_argument("--pin", default=None,
                    help=f"optional access PIN (defence-in-depth), min "
                         f"{PIN_MIN_DIGITS} digits; the relay stores only a public "
                         f"key derived from it, viewers must enter it to open the "
                         f"room. Pass 'random' to generate one.")
    ap.add_argument("--insecure", "-k", action="store_true",
                    help="skip TLS cert verification (self-signed room server)")
    ap.add_argument("--no-tls", action="store_true", help="plain ws/http (local)")
    ap.add_argument("--listed", action="store_true",
                    help="opt this room into the public lobby (GET /api/rooms); "
                         "unlisted by default so slugs can't be enumerated")
    ap.add_argument("--provision-secret", default=None,
                    help="shared secret for a relay that enforces RELAY_PROVISION_SECRET "
                         "(sent as X-Provision-Secret); override via HH_WEB_PROVISION_SECRET")
    ap.add_argument("--allow-uploads", action="store_true",
                    help="let browser guests OFFER files to the room. The share URL is a "
                         "bearer capability, so this means anyone holding the link may ask "
                         "to send you a file; each one still requires your /accept, and an "
                         "accepted upload is saved but never bridged into the sandbox")
    args = ap.parse_args()

    import os
    relay = os.environ.get("HH_WEB_RELAY_URL", args.relay)
    if args.password is None:
        import getpass
        args.password = os.environ.get("CMD_CHAT_PASSWORD") or getpass.getpass("Room password: ")

    WebPublisher(
        args.server, args.port, name=args.name, relay_url=relay, label=args.label,
        password=args.password, insecure=args.insecure, no_tls=args.no_tls,
        pin=_resolve_pin(os.environ.get("HH_WEB_PIN", args.pin)), listed=args.listed,
        provision_secret=os.environ.get("HH_WEB_PROVISION_SECRET", args.provision_secret),
        allow_uploads=args.allow_uploads,
        public_url=os.environ.get("HH_WEB_PUBLIC_URL", args.public_url),
    ).run()


if __name__ == "__main__":
    main()
