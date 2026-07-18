"""Pure-Python SRP-6a client — a drop-in fallback for the ``srp`` PyPI package.

The ``srp`` PyPI package is a C extension with no reliable aarch64 wheel, so it
often fails to build under Termux/Android. This module reproduces the *exact*
client-side derivation of that package's pure-Python reference (``srp._pysrp``)
with ``rfc5054_enable()`` active, using only the standard library.

The client surface ``cmd_chat/client/client.py`` consumes:

    rfc5054_enable()                                  # module-level toggle
    SHA256                                            # hash-alg constant
    usr = User(b"chat", password, hash_alg=SHA256)
    _, A = usr.start_authentication()                 # -> (username, A_bytes)
    M    = usr.process_challenge(salt, B)             # -> M_bytes or None
    usr.verify_session(H_AMK)                          # sets authenticated flag
    usr.authenticated()                               # -> bool

The server surface ``cmd_chat/server/srp_auth.py`` consumes (so a phone/Termux
box with no ``srp`` C-ext can *host* a room, not only join one):

    salt, vkey = create_salted_verification_key(b"chat", pw, hash_alg=SHA256)
    svr  = Verifier(b"chat", salt, vkey, A, hash_alg=SHA256)
    s, B = svr.get_challenge()                         # -> (salt, B_bytes)
    H_AMK = svr.verify_session(M)                      # -> H_AMK or None
    K    = svr.get_session_key()

Both sides mirror ``srp._pysrp`` byte for byte — every constant, padding rule,
and hash input — otherwise the M1 / H_AMK proofs would not match the real
library and auth would fail. Correctness is gated by
tests/test_srp_pure_interop.py (pure↔real in both directions).
"""

import hashlib
import os

# ── RFC 5054 compatibility toggle ────────────────────────────────────────────
# Mirrors srp._pysrp: when enabled, integer hash inputs are zero-padded to the
# width of N and the generator is zero-padded to N's length inside H(N, g).
_rfc5054_compat = False
_no_username_in_x = False


def rfc5054_enable(enable: bool = True) -> None:
    global _rfc5054_compat
    _rfc5054_compat = enable


def no_username_in_x(enable: bool = True) -> None:
    global _no_username_in_x
    _no_username_in_x = enable


# ── Hash-algorithm constants (values match srp._pysrp exactly) ───────────────
SHA1 = 0
SHA224 = 1
SHA256 = 2
SHA384 = 3
SHA512 = 4

# ── N/g group selectors (values match srp._pysrp exactly) ────────────────────
NG_1024 = 0
NG_2048 = 1
NG_4096 = 2
NG_8192 = 3
NG_CUSTOM = 4

_hash_map = {
    SHA1: hashlib.sha1,
    SHA224: hashlib.sha224,
    SHA256: hashlib.sha256,
    SHA384: hashlib.sha384,
    SHA512: hashlib.sha512,
}

# The RFC 5054 groups, copied verbatim from srp._pysrp so the numeric N/g are
# bit-identical to the server's. Only NG_2048 is used by the hack-house client
# (User's default ng_type), but the full table is kept for parity.
_ng_const = (
    # 1024-bit
    (
        "EEAF0AB9ADB38DD69C33F80AFA8FC5E86072618775FF3C0B9EA2314C9C256576D674DF7496"
        "EA81D3383B4813D692C6E0E0D5D8E250B98BE48E495C1D6089DAD15DC7D7B46154D6B6CE8E"
        "F4AD69B15D4982559B297BCF1885C529F566660E57EC68EDBC3C05726CC02FD4CBF4976EAA"
        "9AFD5138FE8376435B9FC61D2FC0EB06E3",
        "2",
    ),
    # 2048-bit
    (
        "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CBB4"
        "A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50E8083969EDB767B0CF60"
        "95179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF"
        "747359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481F1D2B907"
        "8717461A5B9D32E688F87748544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB37861"
        "60279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DB"
        "FBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73",
        "2",
    ),
    # 4096-bit
    (
        "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E08"
        "8A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
        "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9"
        "A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE6"
        "49286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8"
        "FD24CF5F83655D23DCA3AD961C62F356208552BB9ED529077096966D"
        "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3BE39E772C"
        "180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
        "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D"
        "04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7D"
        "B3970F85A6E1E4C7ABF5AE8CDB0933D71E8C94E04A25619DCEE3D226"
        "1AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
        "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFC"
        "E0FD108E4B82D120A92108011A723C12A787E6D788719A10BDBA5B26"
        "99C327186AF4E23C1A946834B6150BDA2583E9CA2AD44CE8DBBBC2DB"
        "04DE8EF92E8EFC141FBECAA6287C59474E6BC05D99B2964FA090C3A2"
        "233BA186515BE7ED1F612970CEE2D7AFB81BDD762170481CD0069127"
        "D5B05AA993B4EA988D8FDDC186FFB7DC90A6C08F4DF435C934063199"
        "FFFFFFFFFFFFFFFF",
        "5",
    ),
    # 8192-bit
    (
        "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E08"
        "8A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
        "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9"
        "A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE6"
        "49286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8"
        "FD24CF5F83655D23DCA3AD961C62F356208552BB9ED529077096966D"
        "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3BE39E772C"
        "180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
        "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D"
        "04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7D"
        "B3970F85A6E1E4C7ABF5AE8CDB0933D71E8C94E04A25619DCEE3D226"
        "1AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
        "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFC"
        "E0FD108E4B82D120A92108011A723C12A787E6D788719A10BDBA5B26"
        "99C327186AF4E23C1A946834B6150BDA2583E9CA2AD44CE8DBBBC2DB"
        "04DE8EF92E8EFC141FBECAA6287C59474E6BC05D99B2964FA090C3A2"
        "233BA186515BE7ED1F612970CEE2D7AFB81BDD762170481CD0069127"
        "D5B05AA993B4EA988D8FDDC186FFB7DC90A6C08F4DF435C934028492"
        "36C3FAB4D27C7026C1D4DCB2602646DEC9751E763DBA37BDF8FF9406"
        "AD9E530EE5DB382F413001AEB06A53ED9027D831179727B0865A8918"
        "DA3EDBEBCF9B14ED44CE6CBACED4BB1BDB7F1447E6CC254B33205151"
        "2BD7AF426FB8F401378CD2BF5983CA01C64B92ECF032EA15D1721D03"
        "F482D7CE6E74FEF6D55E702F46980C82B5A84031900B1C9E59E7C97F"
        "BEC7E8F323A97A7E36CC88BE0F1D45B7FF585AC54BD407B22B4154AA"
        "CC8F6D7EBF48E1D814CC5ED20F8037E0A79715EEF29BE32806A1D58B"
        "B7C5DA76F550AA3D8A1FBFF0EB19CCB1A313D55CDA56C9EC2EF29632"
        "387FE8D76E3C0468043E8F663F4860EE12BF2D5B0B7474D6E694F91E"
        "6DBE115974A3926F12FEE5E438777CB6A932DF8CD8BEC4D073B931BA"
        "3BC832B68D9DD300741FA7BF8AFC47ED2576F6936BA424663AAB639C"
        "5AE4F5683423B4742BF1C978238F16CBE39D652DE3FDB8BEFC848AD9"
        "22222E04A4037C0713EB57A81A23F0C73473FC646CEA306B4BCBC886"
        "2F8385DDFA9D4B7FA2C087E879683303ED5BDD3A062B3CF5B3A278A6"
        "6D2A13F83F44F82DDF310EE074AB6A364597E899A0255DC164F31CC5"
        "0846851DF9AB48195DED7EA1B1D510BD7EE74D73FAF36BC31ECFA268"
        "359046F4EB879F924009438B481C6CD7889A002ED5EE382BC9190DA6"
        "FC026E479558E4475677E9AA9E3050E2765694DFC81F56E880B96E71"
        "60C980DD98EDD3DFFFFFFFFFFFFFFFFF",
        "0x13",
    ),
)


def _get_ng(ng_type: int, n_hex, g_hex):
    if ng_type < NG_CUSTOM:
        n_hex, g_hex = _ng_const[ng_type]
    return int(n_hex, 16), int(g_hex, 16)


# ── Integer <-> big-endian byte conversions (match srp._pysrp exactly) ───────
def _bytes_to_long(s: bytes) -> int:
    return int.from_bytes(s, "big")


def _long_to_bytes(n: int) -> bytes:
    # srp._pysrp yields the minimal big-endian encoding with no leading zero
    # byte, and b"" for zero. int.to_bytes with the exact bit-length does the
    # same for n > 0; special-case 0 to match the empty-string behaviour.
    if n == 0:
        return b""
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def _get_random(nbytes: int) -> int:
    return _bytes_to_long(os.urandom(nbytes))


def _get_random_of_length(nbytes: int) -> int:
    offset = (nbytes * 8) - 1
    return _get_random(nbytes) | (1 << offset)


def _H(hash_class, *args, width=None) -> bytes:
    """Concatenating hash. Integer args become minimal big-endian bytes; when
    ``width`` is set and RFC 5054 mode is on, each arg is left zero-padded to
    ``width`` bytes before hashing (mirrors srp._pysrp.H)."""
    h = hash_class()
    for s in args:
        if s is None:
            continue
        data = _long_to_bytes(s) if isinstance(s, int) else s
        if width is not None and _rfc5054_compat:
            h.update(bytes(width - len(data)))
        h.update(data)
    return h.digest()


def _HNxorg(hash_class, N: int, g: int) -> bytes:
    bin_N = _long_to_bytes(N)
    bin_g = _long_to_bytes(g)
    padding = len(bin_N) - len(bin_g) if _rfc5054_compat else 0
    hN = hash_class(bin_N).digest()
    hg = hash_class(b"".join([b"\0" * padding, bin_g])).digest()
    return bytes(hN[i] ^ hg[i] for i in range(len(hN)))


def _gen_x(hash_class, salt: bytes, username, password) -> int:
    username = username.encode() if hasattr(username, "encode") else username
    password = password.encode() if hasattr(password, "encode") else password
    if _no_username_in_x:
        username = b""
    return _bytes_to_long(
        _H(hash_class, salt, _H(hash_class, username + b":" + password))
    )


def _calculate_M(hash_class, N, g, I, s, A, B, K) -> bytes:
    I = I.encode() if hasattr(I, "encode") else I
    h = hash_class()
    h.update(_HNxorg(hash_class, N, g))
    h.update(hash_class(I).digest())
    if isinstance(s, int):
        s = _long_to_bytes(s)
    h.update(s)
    h.update(_long_to_bytes(A))
    h.update(_long_to_bytes(B))
    h.update(K)
    return h.digest()


def _calculate_H_AMK(hash_class, A, M, K) -> bytes:
    h = hash_class()
    h.update(_long_to_bytes(A))
    h.update(M)
    h.update(K)
    return h.digest()


class User:
    """Client side of the SRP-6a exchange, matching srp._pysrp.User's surface."""

    def __init__(
        self,
        username,
        password,
        hash_alg=SHA1,
        ng_type=NG_2048,
        n_hex=None,
        g_hex=None,
        bytes_a=None,
        bytes_A=None,
        k_hex=None,
    ):
        if ng_type == NG_CUSTOM and (n_hex is None or g_hex is None):
            raise ValueError("Both n_hex and g_hex are required when ng_type = NG_CUSTOM")
        if bytes_a and len(bytes_a) != 256:
            raise ValueError("256 bytes required for bytes_a")

        N, g = _get_ng(ng_type, n_hex, g_hex)
        hash_class = _hash_map[hash_alg]
        if k_hex is None:
            k = _bytes_to_long(_H(hash_class, N, g, width=len(_long_to_bytes(N))))
        else:
            k = int(k_hex, 16)

        self.I = username
        self.p = password
        if bytes_a:
            self.a = _bytes_to_long(bytes_a)
        else:
            self.a = _get_random_of_length(256)
        if bytes_A:
            self.A = _bytes_to_long(bytes_A)
        else:
            self.A = pow(g, self.a, N)
        self.v = None
        self.M = None
        self.K = None
        self.H_AMK = None
        self._authenticated = False

        self.hash_class = hash_class
        self.N = N
        self.g = g
        self.k = k

    def authenticated(self) -> bool:
        return self._authenticated

    def get_username(self):
        return self.I

    def get_ephemeral_secret(self) -> bytes:
        return _long_to_bytes(self.a)

    def get_session_key(self):
        return self.K if self._authenticated else None

    def start_authentication(self):
        return (self.I, _long_to_bytes(self.A))

    def process_challenge(self, bytes_s, bytes_B):
        """Returns M (bytes) or None if an SRP-6a safety check is violated."""
        self.s = bytes_s
        self.B = _bytes_to_long(bytes_B)

        N = self.N
        g = self.g
        k = self.k
        hash_class = self.hash_class

        # SRP-6a safety check
        if (self.B % N) == 0:
            return None

        self.u = _bytes_to_long(_H(hash_class, self.A, self.B, width=len(_long_to_bytes(N))))

        # SRP-6a safety check
        if self.u == 0:
            return None

        self.x = _gen_x(hash_class, self.s, self.I, self.p)
        self.v = pow(g, self.x, N)
        self.S = pow((self.B - k * self.v), (self.a + self.u * self.x), N)

        self.K = hash_class(_long_to_bytes(self.S)).digest()
        self.M = _calculate_M(hash_class, N, g, self.I, self.s, self.A, self.B, self.K)
        self.H_AMK = _calculate_H_AMK(hash_class, self.A, self.M, self.K)

        return self.M

    def verify_session(self, host_HAMK) -> None:
        if self.H_AMK == host_HAMK:
            self._authenticated = True


# ── server side ──────────────────────────────────────────────────────────────
def create_salted_verification_key(
    username,
    password,
    hash_alg=SHA1,
    ng_type=NG_2048,
    n_hex=None,
    g_hex=None,
    salt_len=4,
):
    """Return ``(salt, vkey)`` for a password — mirrors srp._pysrp exactly.

    ``v = g**x mod N`` where ``x = H(salt, H(username:password))``; the salt is
    ``salt_len`` random bytes. This is what the room stores in place of the
    password (SRP never sees the password on the wire)."""
    if ng_type == NG_CUSTOM and (n_hex is None or g_hex is None):
        raise ValueError("Both n_hex and g_hex are required when ng_type = NG_CUSTOM")
    hash_class = _hash_map[hash_alg]
    N, g = _get_ng(ng_type, n_hex, g_hex)
    _s = _long_to_bytes(_get_random(salt_len))
    _v = _long_to_bytes(pow(g, _gen_x(hash_class, _s, username, password), N))
    return _s, _v


class Verifier:
    """Server side of the SRP-6a exchange, matching srp._pysrp.Verifier's surface."""

    def __init__(
        self,
        username,
        bytes_s,
        bytes_v,
        bytes_A,
        hash_alg=SHA1,
        ng_type=NG_2048,
        n_hex=None,
        g_hex=None,
        bytes_b=None,
    ):
        if ng_type == NG_CUSTOM and (n_hex is None or g_hex is None):
            raise ValueError("Both n_hex and g_hex are required when ng_type = NG_CUSTOM")
        if bytes_b and len(bytes_b) != 32:
            raise ValueError("32 bytes required for bytes_b")

        self.A = _bytes_to_long(bytes_A)
        self.M = None
        self.K = None
        self.H_AMK = None
        self._authenticated = False
        self.I = username
        self.s = bytes_s
        self.v = _bytes_to_long(bytes_v)

        N, g = _get_ng(ng_type, n_hex, g_hex)
        hash_class = _hash_map[hash_alg]
        k = _bytes_to_long(_H(hash_class, N, g, width=len(_long_to_bytes(N))))

        self.hash_class = hash_class
        self.N = N
        self.g = g
        self.k = k

        # SRP-6a safety check: abort (no challenge) if A is a multiple of N.
        self.safety_failed = False
        if (self.A % N) == 0:
            self.safety_failed = True
            self.B = None
            return

        if bytes_b:
            self.b = _bytes_to_long(bytes_b)
        else:
            self.b = _get_random_of_length(32)
        self.B = (k * self.v + pow(g, self.b, N)) % N
        self.u = _bytes_to_long(_H(hash_class, self.A, self.B, width=len(_long_to_bytes(N))))
        self.S = pow(self.A * pow(self.v, self.u, N), self.b, N)
        self.K = hash_class(_long_to_bytes(self.S)).digest()
        self.M = _calculate_M(hash_class, N, g, self.I, self.s, self.A, self.B, self.K)
        self.H_AMK = _calculate_H_AMK(hash_class, self.A, self.M, self.K)

    def authenticated(self) -> bool:
        return self._authenticated

    def get_username(self):
        return self.I

    def get_ephemeral_secret(self) -> bytes:
        return _long_to_bytes(self.b)

    def get_session_key(self):
        return self.K if self._authenticated else None

    def get_challenge(self):
        """Return ``(salt, B_bytes)`` or ``(None, None)`` if the safety check failed."""
        if self.safety_failed:
            return None, None
        return (self.s, _long_to_bytes(self.B))

    def verify_session(self, user_M):
        """Check the client proof M; return H_AMK (bytes) on success, else None."""
        if not self.safety_failed and user_M == self.M:
            self._authenticated = True
            return self.H_AMK
        return None
