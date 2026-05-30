import srp, srp._pysrp as p, hashlib, binascii
srp.rfc5054_enable()
# force pure-python to match (and check active backend)
import srp as S
print("# backend:", S._mod.__name__)
H=hashlib.sha256
user=b"chat"; pw=b"labtest"
salt=bytes.fromhex("0a1b2c3d")             # fixed 4-byte salt
a=bytes.fromhex(("11"*256))                # fixed 256-byte a (high bit set via 0x11.. ok? need high bit) 
a=bytes([0x80])+bytes.fromhex("22"*255)    # ensure high bit set, 256 bytes
b=bytes([0x80])+bytes.fromhex("33"*255)
# verifier from known salt: replicate create_salted_verification_key internals with fixed salt
N,g=p.get_ng(p.NG_2048,None,None)
x=p.gen_x(H, salt, user, pw)
v=pow(g,x,N)
v_bytes=p.long_to_bytes(v)
usr=p.User(user,pw,hash_alg=p.SHA256,ng_type=p.NG_2048,bytes_a=a)
I,A=usr.start_authentication()
svr=p.Verifier(user,salt,v_bytes,A,hash_alg=p.SHA256,ng_type=p.NG_2048,bytes_b=b)
s2,B=svr.get_challenge()
M=usr.process_challenge(salt,B)
HAMK=svr.verify_session(M)
usr.verify_session(HAMK)
def hx(x): return binascii.hexlify(x).decode()
print("N_bits", N.bit_length())
print("salt", hx(salt))
print("A", hx(A))
print("B", hx(B))
print("M", hx(M))
print("HAMK", hx(HAMK))
print("K", hx(usr.K))
print("authok", usr.authenticated())
