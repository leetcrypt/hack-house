<div align="center">

# hack-house

### encrypted collaborative sessions with a summoned sandbox

`zero-knowledge server · end-to-end fernet · srp · ratatui`

*they want you dependent. we want you free.*

</div>

---

**hack-house** is the evolution of `cmd-chat`: a multi-user, end-to-end-encrypted
terminal session where a small crew shares chat, files, and — when
summoned — a disposable sandboxed Linux box they drive together, with real
Linux permissions and an owner who can delegate the keys.

The server never sees plaintext. Everything — messages, files, terminal output —
is relayed as opaque ciphertext. Close the window, the house empties.

## status

This is the Rust client (`ratatui`) for the unchanged Python (Sanic) server. The
wire protocol is JSON-over-WebSocket; SRP + HKDF→Fernet are byte-for-byte
compatible with the Python `srp` / `cryptography` stack.

| phase | feature | state |
|---|---|---|
| **P0** | Rust↔Python SRP / Fernet crypto parity | ✅ proven (golden vectors + live + cross-lang E2E) |
| **P2** | multi-user session (cap 4, infra for more) + authoritative roster | ✅ done |
| **P1** | ratatui UI (chat, roster, themes, help overlay) | ✅ done |
| **P3** | sandbox box (local / docker / multipass) + shared PTY | ✅ done |
| **P4** | permissions (app drive ACL + VM unix users / sudo) | ✅ done |
| **P5** | file + directory transfer into the shared session | ✅ done |

## crypto parity — the load-bearing proof

```
$ hack-house selftest            # offline: Rust SRP ≡ Python srp golden vectors
$ hack-house handshake <ip> <port> <name> --password <pw> --no-tls
  /srp/verify ok — server identity proven (H_AMK ✓)
  round-trip ✓ decrypted: "the house is open"
```

`tools/gen_vectors.py` regenerates the golden vectors from the live Python
library (must match the server's `_ctsrp` backend with `rfc5054_enable()`).

> **note:** the SRP identity is always the fixed room identity `b"chat"`; the
> display name is carried only in JSON, never in the SRP proof. The Python `srp`
> package's `rfc5054_enable()` toggles the *active backend's* flag — vectors must
> be generated with the same backend the server actually loads (`_ctsrp`).

## license

MIT · *malware bless · hack the planet*
