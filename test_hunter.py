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


REPLY, HALT, CONFIGURE, SET_TX_ENABLED = 4, 8, 15, 26
ok = True
def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)

def dec(msg, snr=-5):
    ns["set_decparams"](msg, {"t_ms": 0, "snr": snr, "dt": 0.1, "df": 1500, "mode": "~"})
    ns["handle_decode"](msg, snr, CTX["tx"], None, {}, [0], 15000)

def fresh():
    st["target"] = None; st["cq"].clear(); st["msgs"].clear(); st["worked"] = set()
    ENG["callers"].clear(); ENG["holds"].clear()
    ENG.update(idle_until=0, cq_until=0, idle_n=0)
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
check("ws-hunter build -> Enable Tx over UDP", CTX["tx"].sent == [HALT, CONFIGURE, SET_TX_ENABLED])
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
