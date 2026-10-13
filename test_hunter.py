#!/usr/bin/env python3
"""Offline tests for the ft8_server.py targeting engine: feeds real-format
decodes and UDP packets, no WS or radio needed.  Run: python3 test_hunter.py"""
import os, socket, struct, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "ft8_server.py")).read()
src = src[:src.index("ap = argparse.ArgumentParser()")]  # load without starting servers
ns = {"__name__": "hunter", "__file__": os.path.join(HERE, "ft8_server.py")}
exec(src, ns)
st, ENG, CTX = ns["state"], ns["ENG"], ns["CTX"]


class FakeSock:
    """Records the message type of every packet the hunter sends to WS."""
    def __init__(self): self.sent = []
    def sendto(self, data, addr): self.sent.append(struct.unpack(">I", data[8:12])[0])


REPLY, HALT, CONFIGURE = 4, 8, 15
SET_TX_PERIOD, SET_TX_DF, SET_HOLD, SET_TX_ENABLED = 19, 24, 25, 26
ok = True
def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)

def dec(msg, snr=-5, df=1500, t_ms=0):
    ns["set_decparams"](msg, {"t_ms": t_ms, "snr": snr, "dt": 0.1, "df": df, "mode": "~"})
    ns["handle_decode"](msg, snr, CTX["tx"], None, {}, [0], 15000)

def fresh():
    st["target"] = None; st["cq"].clear(); st["msgs"].clear(); st["worked"] = set()
    ENG["callers"].clear(); ENG["holds"].clear()
    ENG.update(idle_until=0, cq_until=0, idle_n=0, qrm_until=0, qrm_n=0)
    ENG["occ"].clear(); ENG["spec"].clear(); ENG["noise_hist"].clear(); st["udp"].pop("tx_df", None)
    CTX["tx"].sent.clear()

def ps(s):
    b = s.encode(); return struct.pack(">i", len(b)) + b


CTX["tx"] = FakeSock()
st.update(mycall="KR4MZM", running=True)
ns["arm_tx"] = lambda only_if_off=False: "clicked (test)"
real_sleep = time.sleep
time.sleep = lambda s: None  # skip start_cq's wait for WS (shared time module)
st["config"]["call_cq"] = False

# --- callsign helpers
check("base_call strips hash + suffix", ns["base_call"]("<KR4MZM/AG>") == "KR4MZM")
check("base_call prefix form", ns["base_call"]("VE3/K1ABC") == "K1ABC")
check("addressing: called station first",
      ns["addressing"]("KR4MZM/AG <NF3R> 73")[:2] == ("KR4MZM/AG", "NF3R"))

# --- a full QSO: CQ POTA answered, reply via hashed /AG call, sign-off
fresh()
dec("CQ POTA W1AW FN31", snr=0); ns["engine_tick"]()
t = st["target"]
check("CQ POTA station targeted", t and t["call"] == "W1AW" and t["msg"] == "CQ POTA W1AW FN31")
dec("<KR4MZM/AG> W1AW -10")
check("reply to hashed /AG call detected", t["resp"] and t["left"] == 99)
n = len(CTX["tx"].sent); ns["engine_tick"]()
check("no Reply re-sent once QSO is progressing", len(CTX["tx"].sent) == n)
dec("KR4MZM/AG <W1AW> RR73")
check("sign-off -> done, short release", t["done"] and t["left"] == 2)
ns["engine_tick"](); ns["engine_tick"]()
check("done QSO released and marked worked", st["target"] is None and ns["is_worked"]("W1AW"))

# --- callers
fresh()
dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
t2 = st["target"]; dec("<KR4MZM/AG> K9XYZ -03")
dec("KR4MZM DL1ABC JO62")
check("caller queued", [c for c, _ in ENG["callers"]] == ["DL1ABC"])
check("in-progress QSO not dropped for caller", st["target"] is t2)
fresh(); dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
dec("KR4MZM DL1ABC JO62")
check("unanswered target dropped for caller", st["target"] is None)
fresh(); ENG["idle_until"] = time.time() + 60
dec("KR4MZM DL1ABC JO62"); ns["engine_tick"]()
check("caller answered during a listen rest", st["target"] and st["target"]["call"] == "DL1ABC")

# --- tail-end targets the sender
fresh(); dec("JA1AAA VK2BBB RR73", snr=3); ns["engine_tick"]()
check("tail-end targets sender VK2BBB", st["target"] and st["target"]["call"] == "VK2BBB")

# --- worked checks
st["worked"] = {"N0CALL"}
check("worked check ignores /P suffix", ns["is_worked"]("N0CALL/P"))
check("own call counts as worked", ns["is_worked"]("<KR4MZM/AG>"))

# --- activation priority
fresh()
dec("CQ K5LOUD EM12", snr=10); dec("CQ SOTA W7SUM DN31", snr=-15); dec("CQ POTA K4PRK EM85", snr=-8)
ns["engine_tick"]()
check("strongest activation beats louder plain CQ",
      st["target"]["call"] == "K4PRK" and st["target"]["why"] == "POTA")
fresh(); st["config"]["special_first"] = False
dec("CQ K5LOUD EM12", snr=10); dec("CQ POTA K4PRK EM85", snr=-8); ns["engine_tick"]()
check("special_first off -> plain strongest", st["target"]["call"] == "K5LOUD")
st["config"]["special_first"] = True
fresh(); dec("CQ EU DL9ABC JO40", snr=12); dec("CQ K5LOUD EM12", snr=-2); ns["engine_tick"]()
check("region-directed CQ EU skipped", st["target"]["call"] == "K5LOUD")
check("CQ EU still listed with its tag", st["cq"]["DL9ABC"]["tag"] == "EU")
fresh(); dec("CQ POTA K4PRK EM85", snr=-8); dec("KR4MZM N5CAL EM15", snr=-20); ns["engine_tick"]()
check("station calling me outranks activation", st["target"]["call"] == "N5CAL")
fresh(); dec("CQ DX JA1XYZ PM95")
check("CQ DX parsed", st["cq"]["JA1XYZ"]["tag"] == "DX" and st["cq"]["JA1XYZ"]["grid"] == "PM95")
dec("CQ W9NOG"); check("CQ without grid parsed", "W9NOG" in st["cq"])

# --- calling CQ as the last resort
st["config"].update(call_cq=True, cq_secs=120, idle_secs=60)
fresh(); ns["engine_tick"]()
check("nothing to answer -> calls CQ (halt, configure, stock click)",
      ENG["cq_until"] > time.time() and CTX["tx"].sent[:2] == [HALT, CONFIGURE])
st["udp"]["revision"] = "260926-hunter"
fresh(); ns["engine_tick"]()
s = CTX["tx"].sent
check("ws-hunter build -> period, hold, offset, then CQ + Enable Tx over UDP",
      s[0] == HALT and s[1] == SET_TX_PERIOD and SET_HOLD in s and SET_TX_DF in s
      and s[-2:] == [CONFIGURE, SET_TX_ENABLED])
dec("JA1AAA VK2BBB RR73"); ns["engine_tick"]()
check("tail-end traffic does not interrupt our CQ", st["target"] is None and ENG["cq_until"])
dec("CQ POTA K4PRK EM85"); ns["engine_tick"]()
check("new activation interrupts our CQ", st["target"]["call"] == "K4PRK" and not ENG["cq_until"])
fresh(); ns["engine_tick"](); dec("KR4MZM N5CAL EM15"); ns["engine_tick"]()
check("caller answering our CQ becomes the target", st["target"]["call"] == "N5CAL")
fresh(); ns["engine_tick"](); ENG["cq_until"] = time.time() - 1; CTX["tx"].sent.clear()
ns["engine_tick"]()
check("CQ run over -> halt and listen", CTX["tx"].sent == [HALT] and ENG["idle_until"] > time.time())
st["config"]["call_cq"] = False
fresh(); ns["engine_tick"]()
check("call_cq off -> no CQ", not ENG["cq_until"] and not CTX["tx"].sent)
st["udp"]["revision"] = ""

# --- frequency picker
fresh()
pick = ns["pick_tx_df"]
f = pick(1)
check("empty band -> mid-band offset", f is not None and 1400 <= f <= 1500)
for i, df in enumerate(range(300, 2700, 80)):  # crowd the odd period except one hole
    if not 1800 <= df < 2000:
        dec(f"W{i}AA K{i}BB -10", df=df, t_ms=15000)
f = pick(1)
check("picks the only clear gap (1800-2000)", f is not None and 1800 <= f and f + 50 <= 2000)
check("even period unaffected by odd-period crowd", 1400 <= pick(0) <= 1500)
check("keeps current offset while clear", pick(1, current=1850) == 1850)
check("moves off an occupied current offset", pick(1, current=700) != 700)
for i, df in enumerate(range(1800, 2000, 40)):
    dec(f"W{i}CC K{i}DD -10", df=df, t_ms=15000)
check("fully crowded period -> no pick", pick(1) is None)

# answering a station on the even period moves us to a clear odd offset
fresh(); st["udp"]["revision"] = "260926-hunter"; st["udp"]["tx_df"] = 1500
dec("K1CQ W2BUSY -05", df=1480, t_ms=15000)       # odd period traffic on our offset
dec("CQ K4PRK EM85", snr=-3, df=900, t_ms=0)       # even-period CQ: we answer on odd
ns["engine_tick"]()
s = CTX["tx"].sent
check("answer: hold + offset change before the Reply",
      s.index(SET_HOLD) < s.index(REPLY) and s.index(SET_TX_DF) < s.index(REPLY))
fresh(); st["udp"]["revision"] = ""; st["udp"]["tx_df"] = 1500
dec("CQ K4PRK EM85", snr=-3, df=900, t_ms=0); ns["engine_tick"]()
check("stock WS: no control messages, suggestion only",
      CTX["tx"].sent == [REPLY] and ENG["suggest"]["par"] == 1)

# --- spectrum + someone else on frequency
def spectrum(levels, t_ms=15000, floor=-20.0, step=0.25):
    body = struct.pack(">IIII", t_ms, 15000, 0, 10) + struct.pack(">dd", floor, step)
    body += struct.pack(">I", len(levels)) + bytes(levels)
    return struct.pack(">III", 0xADBCCBDA, 2, 27) + ps("WS") + body
def handle(pkt):
    ns["handle_spectrum"](pkt, 12 + 4 + 2)
quiet = [20] * 300                                  # flat noise
one_ft8 = quiet[:]; one_ft8[150:155] = [80] * 5     # one 50 Hz signal at 1500 Hz
fresh(); handle(spectrum(one_ft8))
check("spectrum marks hot bins", ENG["spec"][1]["hot"] == [1500, 1510, 1520, 1530, 1540])
f = pick(1, current=1510)
check("undecoded signal in spectrum moves the pick", f != 1510)
ssb = quiet[:]; ssb[30:280] = [90] * 250            # 300-2800 Hz voice signal fills the passband
fresh()
for _ in range(4): handle(spectrum(quiet))           # normal band first: learns the noise floor
handle(spectrum(ssb)); check("one wide cycle alone: no backoff", ENG["qrm_until"] == 0)
handle(spectrum(ssb))
check("two wide cycles -> back off", ENG["qrm_until"] > time.time())
ENG["cq_until"] = 0; dec("CQ K4PRK EM85", snr=-3); ns["engine_tick"]()
check("no new calls during backoff", st["target"] is None)
fresh(); dec("K1AA K2BB -10", df=1000, t_ms=15000); dec("K3CC K4DD -10", df=1100, t_ms=15000)
busy_ft8 = quiet[:]
for i in range(95, 125): busy_ft8[i] = 90           # 1000-1250 Hz: explained by decodes
handle(spectrum(busy_ft8)); handle(spectrum(busy_ft8))
check("wide energy explained by FT8 decodes: no backoff", ENG["qrm_until"] == 0)
st["udp"]["revision"] = ""

# --- real UDP packets through udp_listener
ns["udp_port"] = 22999; st["fwd_port"] = 22998
time.sleep = real_sleep
threading.Thread(target=ns["udp_listener"], daemon=True).start(); time.sleep(0.3)
tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
def send(mtype, body):
    tx.sendto(struct.pack(">III", 0xADBCCBDA, 2, mtype) + ps("WS") + body, ("127.0.0.1", 22999))
    time.sleep(0.2)
send(0, struct.pack(">I", 3) + ps("3.2.1") + ps("260926-hunter"))
check("Heartbeat revision detects ws-hunter", ns["has_ws_hunter"]())
for spec, extra in ((1, b""), (2, struct.pack(">i", -18000))):
    fresh(); st["target"] = {"call": "W1AW", "left": 99}
    send(5, struct.pack(">qIB", 2461327, 52_000_000, spec) + extra + ps("W1AW") + ps("FN31"))
    check(f"QSO Logged (timespec {spec}) marks worked + clears target",
          "W1AW" in st["worked"] and st["target"] is None)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
