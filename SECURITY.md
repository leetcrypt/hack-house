# Security Policy

hack-house is an end-to-end-encrypted tool. We take vulnerability reports
seriously and appreciate responsible disclosure.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Instead, use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** (Private Vulnerability Reporting).
3. Describe the issue, how to reproduce it, and the impact.

If private reporting is unavailable, contact a maintainer directly rather than
filing a public issue.

We aim to acknowledge reports within a few days and to keep you updated as we
investigate and fix the issue. We'll credit reporters in the release notes
unless you prefer to remain anonymous.

## Scope

This project's security rests on a few load-bearing properties. Reports that
break any of these are especially valuable:

- **Zero-knowledge server** — the server must only ever handle ciphertext. Any
  path where it can recover plaintext messages, file contents, file names, or
  terminal output is in scope.
- **SRP authentication** — the room password must never traverse the network.
  Anything that leaks it, enables offline cracking beyond the SRP design, or
  permits authentication bypass is in scope.
- **Room key derivation** — the room key (`HKDF(password, room_salt)`) must
  stay client-side. Key recovery by the server or a passive observer is in
  scope.
- **Crypto parity** — the Rust client and Python server must agree on SRP +
  Fernet. Divergences that weaken encryption are in scope.
- **Sandbox isolation / permissions** — bypassing the drive ACL or VM
  sudo/unix-user controls (e.g. driving without a grant, escalating to
  superuser without `/sudo`) is in scope.
- **WebSocket auth** — session hijacking or replay against the post-SRP
  HMAC token is in scope.

## Out of scope

- The `--no-tls` / `--insecure` modes: these intentionally disable transport
  security for local or trusted-tunnel (e.g. Tailscale) use. Issues that only
  manifest when TLS is deliberately turned off are not vulnerabilities.
- Self-signed certificate warnings.
- Denial of service from a participant who already holds the room password
  (anyone in the room is, by design, trusted with the room key).

## Operational guidance

- Always share the room password out-of-band over a trusted channel.
- Use TLS (or a trusted tunnel such as Tailscale) for anything beyond local
  testing; reserve `--no-tls` for `127.0.0.1` and tunneled traffic.
- Anyone with the room password can read all traffic — treat room membership
  as full trust.
