import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.WARNING)

from strategy import detect_kill_zone, generate_signal, candles_to_df
from mexc_api import MEXCSpotAPI
from datetime import datetime, timezone, timedelta

api = MEXCSpotAPI()
now = datetime.now(timezone.utc)
kz  = detect_kill_zone()
print(f"Current UTC : {now.strftime('%Y-%m-%d %H:%M')}  weekday={now.weekday()} (0=Mon)")
print(f"detect_kill_zone() = {repr(kz)}")
print()

# Simulate all scenarios — anchor: Tuesday = weekday 1
BASE_TUE = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)   # 26 May 2026 is a Tuesday

cases = [
    (0,  9,  "Mon 09:00 London hours"),
    (0,  14, "Mon 14:00 NY hours"),
    (0,  3,  "Mon 03:00 off-hours"),
    (1,  9,  "Tue 09:00 LONDON"),
    (1,  14, "Tue 14:00 NEW_YORK"),
    (1,  16, "Tue 16:00 LONDON_CLOSE"),
    (1,  3,  "Tue 03:00 off-hours"),
    (2,  8,  "Wed 08:00 LONDON"),
    (4,  9,  "Fri 09:00 FRIDAY_REDUCED"),
    (4,  3,  "Fri 03:00 off-hours"),
    (5,  9,  "Sat 09:00 weekend"),
    (6,  14, "Sun 14:00 weekend"),
]

print(f"{'Scenario':<30}  {'Result':<20}  Action")
print("-" * 65)
for wd, hr, label in cases:
    dt = BASE_TUE + timedelta(days=(wd - 1), hours=hr)
    r  = detect_kill_zone(dt)
    if r is None:
        action = "HARD BLOCK (weekend)"
    elif r in ("LONDON", "NEW_YORK", "LONDON_CLOSE"):
        action = "+1 score bonus"
    elif r == "FRIDAY_REDUCED":
        action = "scan (no bonus, half-size)"
    else:
        action = "scan (no bonus)"
    print(f"  {label:<28}  {repr(r):<20}  {action}")

# Live BTCUSDT test
print()
print("Live BTCUSDT generate_signal():")
klines_1h = api.get_klines("BTCUSDT", "60m", 100)
klines_4h = api.get_klines("BTCUSDT", "4h",  50)
df     = candles_to_df(klines_1h)
htf_df = candles_to_df(klines_4h)
sig = generate_signal("BTCUSDT", df, htf_df=htf_df, trade_type="daytrading")
if sig:
    print(f"  SIGNAL  score={sig.score}/10  zone={sig.zone_type}  kill_zone={sig.kill_zone}")
    print(f"  reason= {sig.reason}")
else:
    print("  No signal at this moment")
