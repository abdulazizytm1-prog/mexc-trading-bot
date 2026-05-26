"""
MEXC POST final check — confirm which body style works now that secret is fixed.
Run: python test_order_raw.py
"""
import hmac, hashlib, time
import requests
from urllib.parse import urlencode
import sys
sys.path.insert(0, ".")
from config import API_KEY, API_SECRET, BASE_URL, RECV_WINDOW

def _sign(s: str) -> str:
    return hmac.new(API_SECRET.encode(), s.encode(), hashlib.sha256).hexdigest()

URL = f"{BASE_URL}/api/v3/order"
HDR = {"X-MEXC-APIKEY": API_KEY}
BPARAMS = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.000001"}

def go(label, **kwargs):
    try:
        resp = requests.post(URL, timeout=8, **kwargs)
        d    = resp.json()
        code = d.get("code", "OK")
        msg  = d.get("msg", "")
        ok   = "SIGNATURE OK" if code not in (700002, 700004, 700013) else "BAD AUTH"
        print(f"  [{ok}] {label:<52} code={code}  msg={msg[:55]}")
    except Exception as e:
        print(f"  [ERR]  {label:<52} {e}")

print(f"\nSecret loaded: {API_SECRET[:4]}...{API_SECRET[-4:]}\n")

# A) form-encoded body as dict (requests auto-CT)
ts = int(time.time() * 1000)
p = {**BPARAMS, "timestamp": ts, "recvWindow": RECV_WINDOW}
qs = urlencode(p); p["signature"] = _sign(qs)
go("data=dict (requests auto Content-Type)", headers=HDR, data=p)

# B) form-encoded body as string + explicit CT
ts = int(time.time() * 1000)
p = {**BPARAMS, "timestamp": ts, "recvWindow": RECV_WINDOW}
qs = urlencode(p); p["signature"] = _sign(qs)
go("data=urlencode str + explicit CT hdr",
   headers={**HDR, "Content-Type": "application/x-www-form-urlencoded"},
   data=urlencode(p))

# C) query string only (no body)
ts = int(time.time() * 1000)
p = {**BPARAMS, "timestamp": ts, "recvWindow": RECV_WINDOW}
qs = urlencode(p); p["signature"] = _sign(qs)
go("params= query string only (no body)", headers=HDR, params=p)

print()
