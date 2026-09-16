"""In-RAM room registry for the web relay.

Zero-knowledge by construction (spec §7): a room holds only **routing metadata**
(slug, seq, cols/rows, operator label, viewer counts, an optional PIN *hash*) and
an **opaque content payload** it never interprets. The relay has no room key and
no `K_web` — the payload under `data`/`ct` is copied through verbatim, never
decoded, never logged, never written to disk. Everything here dies with the
process; `delete()` / an ended publisher drops it immediately.

Content rides in opaque `ct`+`nonce` fields (AES-256-GCM under `K_web`, held only
by host + browser). The relay copies them through untouched — it cannot decrypt.

P3 adds the **sequencing / replay / backpressure** machinery (spec §5.3), all of
which operates on ciphertext only:
- a **byte-budgeted ciphertext ring** (default ~512 KB) for late-joiner replay;
- a latest opaque **snapshot** blob so a mid-session joiner reconstructs the
  current screen without replaying the whole ring;
- **reconnect-by-seq** helpers (replay the tail past a browser's last-seen seq);
- **per-viewer send queues** so one slow browser can't stall the others —
  overflow drops that viewer's backlog and re-`sync`s it to the latest snapshot.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field


# Byte-budgeted ring (spec §5.3): keep a bounded tail of opaque `out` frames so a
# late joiner sees current output. Bounded by ciphertext bytes *and* a frame count
# safety cap. Content stays opaque; we only sum `len(ct)`.
RING_MAX_BYTES = 512 * 1024
RING_MAX_FRAMES = 4096
# Bounded chat backlog for late joiners (spec §6 chat egress). Opaque ciphertext
# frames only — the relay never reads authorship or text. Small: chat is low-rate
# and a joiner only needs recent context, not full history.
CHAT_RING_MAX = 100
# Per-viewer send queue depth. A browser that falls this far behind is treated as
# slow: its backlog is dropped and it is re-synced to the latest snapshot (never
# stalls the fan-out to healthy viewers).
VIEWER_QUEUE_MAX = 128

# ── P4 resource/DoS caps (spec §7). Env-overridable so a load test can drive
# small values without touching code. All RAM-only; nothing is persisted. ──
# Max concurrent viewers per room — a full room refuses new subscribers.
MAX_VIEWERS_PER_ROOM = int(os.environ.get("RELAY_MAX_VIEWERS", "32"))
# A room with no publisher and no viewers, untouched for this many seconds, is
# reaped from RAM by the idle reaper (spec §7 "DELETE/idle-timeout purges rooms").
ROOM_IDLE_TIMEOUT = float(os.environ.get("RELAY_IDLE_TIMEOUT", "300"))


@dataclass
class Viewer:
    """One subscribed browser: its socket, a bounded outbound queue, and the sender
    task that drains it. Isolating the queue per viewer is what prevents a slow
    browser from applying backpressure to the whole room (spec §5.3)."""
    ws: object
    queue: "asyncio.Queue" = field(
        default_factory=lambda: asyncio.Queue(maxsize=VIEWER_QUEUE_MAX))
    task: object | None = None
    behind: bool = False
    # P4: this browser negotiated binary WS frames for `out` (spec §5 optimization).
    # When False the viewer stays on the JSON+base64 fallback path.
    binary: bool = False
    # Browser-supplied persistent identity (opaque, pseudonymous — never a
    # browser-asserted *privilege*, only continuity, spec §7). Lets a mobile socket
    # flap reuse the SAME viewer_id, so the driver/approval keyed on it survives a
    # reconnect instead of desyncing (the P0 drive-wedge bug).
    client_id: str = ""
    # False once this socket has dropped. A grace-period reaper only tears the
    # viewer down if it is STILL disconnected after the window (a reconnect flips
    # it back / supersedes the object), so a flap doesn't fire a spurious left.
    connected: bool = True


@dataclass
class Room:
    slug: str
    label: str
    cols: int
    rows: int
    publish_token: str
    # Host-console capability (spec §6 operator approval). SEPARATE from
    # publish_token: it authorizes only drive approve/revoke from the operator
    # web console — never publishing or reading the terminal (no #k). Kept out of
    # any share URL; delivered only to the host.
    host_token: str = ""
    # PIN gate (defence-in-depth, spec §7). P0 mints rooms without one; the seam
    # is here so P4 only wires the check, not the data model.
    #
    # `pin_pub` is the raw Ed25519 PUBLIC key (32 bytes, hex) of a keypair whose
    # seed is PBKDF2-HMAC-SHA256(pin, salt, 200k). A viewer proves knowledge of
    # the PIN by signing a per-connection nonce; the relay only verifies.
    #
    # This used to be `pin_hash` — the PBKDF2 digest itself — compared for
    # equality against what the browser sent. That made the stored value the
    # credential: anyone who read this struct could replay it verbatim and open
    # the room without ever knowing the PIN. A public key cannot be replayed,
    # so reading relay memory no longer authenticates you.
    #
    # There is deliberately NO salt here. The relay does not need it (it never
    # derives anything) and storing it would hand a RAM thief the one missing
    # input for an offline guess-and-check against `pin_pub`. The salt lives in
    # the share link's #fragment and in the publisher — never on the relay.
    pin_pub: str | None = None
    # Lobby visibility (hardening — enumeration). Rooms are UNLISTED by default so
    # the public lobby / GET /api/rooms cannot be used to harvest live slugs (the
    # enabler for the DELETE / viewer-flood attacks). A publisher opts a room in by
    # passing `listed: true` at creation.
    listed: bool = False
    created_at: float = field(default_factory=time.time)
    # Who asked for this room (post-proxy real IP). Operator-facing only: it is
    # what makes "one source is holding every slot" answerable, and it is the key
    # the per-IP standing-room cap counts against. Never leaves an admin response.
    created_ip: str = ""
    # Last time the room saw activity (publisher connect, output, or a viewer).
    # The idle reaper (spec §7) purges a room with no publisher/viewers that has
    # been silent past ROOM_IDLE_TIMEOUT. Starts at creation so a room that is
    # registered but never published still gets reaped.
    last_active: float = field(default_factory=time.time)

    # Live wiring — never serialized outward.
    publisher: object | None = None            # the single publish websocket
    hosts: set = field(default_factory=set)    # operator-console sockets (host_token)
    viewers: dict = field(default_factory=dict)  # viewer_id -> Viewer
    # Identity continuity (spec §7): browser client_id -> viewer_id. A reconnecting
    # browser presenting the same client_id reclaims its old viewer_id, so drive
    # approval / roster stay stable across a mobile socket flap. Opaque routing
    # metadata; forgotten on a genuine (grace-expired) leave.
    clients: dict = field(default_factory=dict)
    # Effective driver's viewer_id (P2), or None for view-only. The relay does NOT
    # decide this — the publisher declares it; the relay only mirrors it into the
    # roster badge. Opaque routing metadata, never room content.
    driver: str | None = None
    last_seq: int = 0
    # Byte-budgeted ring of opaque {seq, ct, nonce} frames; ciphertext never read.
    ring: deque = field(default_factory=deque)
    _ring_bytes: int = 0
    # Latest opaque full-screen replay blob {seq, ct, nonce} or None. Its `seq`
    # marks how far the snapshot has caught up, so a joiner applies only ring
    # frames *after* it (no double-render).
    snapshot: dict | None = None
    # Bounded backlog of opaque chat frames {seq, ct, nonce} for late joiners
    # (spec §6). Never decoded — {from, text} lives inside the ciphertext under
    # K_web, which the relay does not hold.
    chat_ring: deque = field(default_factory=lambda: deque(maxlen=CHAT_RING_MAX))

    # ── viewers ──────────────────────────────────────────────────────────
    def new_viewer(self, ws, client_id: str = "") -> tuple[str, Viewer, bool]:
        """Mint (or reclaim) an opaque pseudonymous viewer_id + Viewer WITHOUT
        registering it yet (spec §7 — drive approval keys off this id, never a
        browser-asserted name). The caller queues the initial replay, then
        `attach_viewer`s, so no live frame can race ahead of the first sync.

        When the browser presents a `client_id` it has used before, we REUSE its
        prior viewer_id (identity continuity across a reconnect) and flag it a
        reconnect so the caller skips the spurious join churn. Returns
        (viewer_id, Viewer, is_reconnect)."""
        if client_id and client_id in self.clients:
            return self.clients[client_id], Viewer(ws=ws, client_id=client_id), True
        viewer_id = "v_" + secrets.token_hex(6)
        if client_id:
            self.clients[client_id] = viewer_id
        return viewer_id, Viewer(ws=ws, client_id=client_id), False

    def attach_viewer(self, viewer_id: str, v: Viewer) -> None:
        self.viewers[viewer_id] = v
        self.last_active = time.time()

    def forget_client(self, client_id: str) -> None:
        """Drop a client_id->viewer_id mapping on a genuine (grace-expired) leave,
        so the continuity table only holds live / recently-dropped viewers."""
        if client_id:
            self.clients.pop(client_id, None)

    def remove_viewer(self, viewer_id: str) -> None:
        self.viewers.pop(viewer_id, None)
        self.last_active = time.time()

    @property
    def viewer_count(self) -> int:
        return len(self.viewers)

    def is_full(self) -> bool:
        """True when the room is at its concurrent-viewer cap (spec §7)."""
        return len(self.viewers) >= MAX_VIEWERS_PER_ROOM

    def touch(self) -> None:
        self.last_active = time.time()

    # ── ring / replay ────────────────────────────────────────────────────
    def record_out(self, seq: int, ct: str, nonce: str) -> None:
        """Buffer one opaque output frame for replay, evicting the oldest frames
        once over the byte/frame budget. `ct`/`nonce` are never inspected."""
        self.last_seq = seq
        self.last_active = time.time()
        self.ring.append({"seq": seq, "ct": ct, "nonce": nonce})
        self._ring_bytes += len(ct)
        while self.ring and (
                self._ring_bytes > RING_MAX_BYTES or len(self.ring) > RING_MAX_FRAMES):
            old = self.ring.popleft()
            self._ring_bytes -= len(old["ct"])

    def set_snapshot(self, seq: int, ct: str, nonce: str) -> None:
        self.snapshot = {"seq": seq, "ct": ct, "nonce": nonce}

    def record_chat(self, seq: int, ct: str, nonce: str) -> None:
        """Buffer one opaque chat frame for late-joiner replay (bounded deque,
        oldest auto-evicted). `ct`/`nonce` are never inspected."""
        self.last_active = time.time()
        self.chat_ring.append({"seq": seq, "ct": ct, "nonce": nonce})

    def _ring_after_snapshot(self) -> list[dict]:
        s = self.snapshot["seq"] if self.snapshot else -1
        return [f for f in self.ring if f["seq"] > s]

    def ring_after(self, seq: int) -> list[dict]:
        return [f for f in self.ring if f["seq"] > seq]

    def can_resume(self, last_seq: int | None) -> bool:
        """True when a reconnecting browser at `last_seq` can be replayed from the
        ring (its next frame is still buffered) — i.e. a delta, not a full reload."""
        if last_seq is None:
            return False
        if last_seq == self.last_seq:
            return True  # already current; resume with an empty delta
        if not self.ring:
            return False
        return last_seq >= self.ring[0]["seq"] - 1

    def sync_payload(self, viewer_id: str) -> dict:
        """Full-state `sync` for a fresh/late joiner (spec §5.3): its opaque
        viewer_id, dims + label + current driver, the latest opaque snapshot, and
        the ring tail *after* the snapshot's seq — so it reconstructs the current
        screen, then live `out` follows."""
        return {
            "type": "sync",
            "viewer_id": viewer_id,
            "cols": self.cols,
            "rows": self.rows,
            "label": self.label,
            "driver": self.driver,
            "snapshot": self.snapshot,
            "ring": self._ring_after_snapshot(),
            "chat": list(self.chat_ring),  # opaque recent-chat backlog
            "last_seq": self.last_seq,
        }

    def roster_payload(self) -> dict:
        """Content-free presence frame: viewer count + current driver's viewer_id."""
        return {
            "type": "roster",
            "viewers": self.viewer_count,
            "driver": self.driver,
        }

    def host_roster(self) -> dict:
        """Operator-console roster: the opaque viewer_ids (not just a count) so the
        host can approve a specific one, plus the current driver + label. No room
        content — viewer_ids are pseudonymous routing metadata (spec §7)."""
        return {
            "type": "roster",
            "label": self.label,
            "viewers": list(self.viewers.keys()),
            "count": self.viewer_count,
            "driver": self.driver,
        }

    def meta(self) -> dict:
        """Lobby-safe metadata only (spec §5.1 GET /api/rooms) — no content.
        `pin_required` lets the lobby badge a locked room without ever exposing
        the PIN (only its presence)."""
        return {
            "slug": self.slug,
            "label": self.label,
            "viewers": self.viewer_count,
            "started_at": self.created_at,
            "pin_required": self.pin_pub is not None,
        }

    def admin_meta(self) -> dict:
        """Operator view: enough to recognise an abusive room and decide to end
        it — and nothing whatsoever about what is inside one.

        Deliberately absent: `publish_token` and `host_token`. An operator can
        already end any room via the admin route, so exposing them would buy
        nothing and would hand out the ability to publish *into* a live room,
        which is strictly worse than ending it. Also absent, and not obtainable:
        anything derived from frame content — the relay cannot read it, and this
        endpoint must not become the reason someone teaches it how."""
        now = time.time()
        return {
            "slug": self.slug,
            "label": self.label,
            "listed": self.listed,
            "viewers": self.viewer_count,
            "publishing": self.publisher is not None,
            "created_ip": self.created_ip,
            "age_s": round(now - self.created_at, 1),
            "idle_s": round(now - self.last_active, 1),
            "ring_bytes": self._ring_bytes,
            "ring_frames": len(self.ring),
            "pin_required": self.pin_pub is not None,
        }


class RoomRegistry:
    """All live rooms, keyed by slug. RAM-only; no persistence."""

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    def __len__(self) -> int:
        return len(self._rooms)

    def create(self, label: str, cols: int, rows: int,
               pin_pub: str | None = None,
               listed: bool = False, created_ip: str = "") -> Room:
        slug = secrets.token_urlsafe(8)
        publish_token = secrets.token_urlsafe(24)
        host_token = secrets.token_urlsafe(24)
        room = Room(
            slug=slug, label=label, cols=cols, rows=rows,
            publish_token=publish_token, host_token=host_token,
            pin_pub=pin_pub, listed=listed, created_ip=created_ip,
        )
        self._rooms[slug] = room
        return room

    def count_for_ip(self, ip: str) -> int:
        """Standing rooms held by one source. Distinct from the create *rate*: a
        room with a publisher attached never idle-reaps, so rate alone lets a
        single caller accumulate slots indefinitely without ever tripping it."""
        if not ip:
            return 0
        return sum(1 for r in self._rooms.values() if r.created_ip == ip)

    def get(self, slug: str) -> Room | None:
        return self._rooms.get(slug)

    def delete(self, slug: str) -> Room | None:
        return self._rooms.pop(slug, None)

    def __iter__(self):
        """Iterate a snapshot, so a caller may close sockets (and so mutate the
        registry) while walking it."""
        return iter(list(self._rooms.values()))

    def purge_idle(self, timeout: float | None = None,
                   now: float | None = None) -> list[Room]:
        """Reap rooms with no publisher and no viewers that have been silent past
        `timeout` (spec §7 idle-timeout). RAM-only teardown — returns the removed
        rooms so the caller can close any lingering sockets (there are none by the
        no-viewers precondition, but the contract stays explicit)."""
        timeout = ROOM_IDLE_TIMEOUT if timeout is None else timeout
        now = time.time() if now is None else now
        dead = [
            slug for slug, r in self._rooms.items()
            if r.publisher is None and r.viewer_count == 0
            and (now - r.last_active) > timeout
        ]
        return [self._rooms.pop(slug) for slug in dead]

    def list_meta(self) -> list[dict]:
        """Lobby listing — only rooms the publisher explicitly opted in (`listed`).
        Unlisted rooms (the default) are reachable by direct link but never
        enumerable, closing the slug-harvesting path (hardening)."""
        return [r.meta() for r in self._rooms.values() if r.listed]

    def admin_list(self) -> list[dict]:
        """Every room, listed or not — the operator view. `list_meta` hides
        unlisted rooms to stop slug harvesting by the public; that same hiding is
        what leaves an operator unable to see what is running on their own box."""
        return [r.admin_meta() for r in self._rooms.values()]
