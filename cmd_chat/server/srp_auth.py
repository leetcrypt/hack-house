import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

import srp


srp.rfc5054_enable()

# Half-finished handshakes (init without a matching verify) would otherwise pile
# up forever, letting an attacker exhaust memory. Evict any session that hasn't
# authenticated within this many seconds whenever a new handshake begins.
UNVERIFIED_TTL_SECONDS = 60


@dataclass
class SRPSession:
    user_id: str = field(default_factory=lambda: str(uuid4()))
    username: str = ""
    svr: Optional[srp.Verifier] = None
    session_key: Optional[bytes] = None
    authenticated: bool = False
    created_at: float = field(default_factory=time.monotonic)


class SRPAuthManager:
    def __init__(self, password: str):
        self.password = password.encode()
        self.sessions: dict[str, SRPSession] = {}
        self.salt, self.vkey = srp.create_salted_verification_key(
            b"chat", self.password, hash_alg=srp.SHA256
        )

    def _evict_stale_unverified(self) -> None:
        now = time.monotonic()
        stale = [
            uid
            for uid, s in self.sessions.items()
            if not s.authenticated and now - s.created_at > UNVERIFIED_TTL_SECONDS
        ]
        for uid in stale:
            del self.sessions[uid]

    def init_auth(
        self, username: str, client_public: bytes
    ) -> tuple[str, bytes, bytes]:
        self._evict_stale_unverified()
        session = SRPSession(username=username)

        svr = srp.Verifier(
            b"chat", self.salt, self.vkey, client_public, hash_alg=srp.SHA256
        )

        s, B = svr.get_challenge()

        if B is None:
            raise ValueError("SRP challenge generation failed")

        session.svr = svr
        self.sessions[session.user_id] = session

        return session.user_id, B, s

    def verify_auth(self, user_id: str, client_proof: bytes) -> tuple[bytes, bytes]:
        session = self.sessions.get(user_id)
        if not session or not session.svr:
            raise ValueError("Invalid session")

        H_AMK = session.svr.verify_session(client_proof)

        if H_AMK is None:
            del self.sessions[user_id]
            raise ValueError("Authentication failed")

        session.session_key = session.svr.get_session_key()
        session.authenticated = True

        return H_AMK, session.session_key

    def get_session(self, user_id: str) -> Optional[SRPSession]:
        session = self.sessions.get(user_id)
        if session and session.authenticated:
            return session
        return None

    def remove_session(self, user_id: str) -> None:
        self.sessions.pop(user_id, None)
