"""hack-house web egress — headless publisher sidecar (P0).

`publisher.py` joins a hack-house room as a legitimate headless member (mirroring
`cmd_chat/agent/`), taps the decrypted `_sbx:data` PTY stream, and republishes it
to the separate, opt-in web relay. `emit_sbx.py` is a throwaway P0 verification
helper that fakes the sandbox output source. See docs/spec-lobby-web-relay.md.
"""
