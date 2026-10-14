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
import tempfile
ns["CONFIG_FILE"] = os.path.join(tempfile.mkdtemp(), "hunter_config.json")  # never the real settings


class FakeSock:
    """Records the message type of every packet the hunter sends to WS."""
    def __init__(self): self.sent = []
    def sendto(self, data, addr): self.sent.append(struct.unpack(">I", data[8:12])[0])


REPLY, HALT, CONFIGURE = 4, 8, 15
SET_TX_PERIOD, SET_TX_DF, SET_HOLD, SET_TX_ENABLED, CLEAR_DX = 19, 24, 25, 26, 28
ok = True
def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)

def dec(msg, snr=-5, df=1500, t_ms=0):
    ns["set_decparams"](msg, {"t_ms": t_ms, "snr": snr, "dt": 0.1, "df": df, "mode": "~"})
    ns["handle_decode"](msg, snr, CTX["tx"], None, {}, [0], 15000)

def fresh():
    st["target"] = None; st["cq"].clear(); st["msgs"].clear(); st["worked"] = {}; ENG["held"].clear()
    ENG["callers"].clear(); ENG["holds"].clear()
    ENG.update(idle_until=0, cq_until=0, idle_n=0, qrm_until=0, qrm_n=0)
    ENG["occ"].clear(); ENG["spec"].clear(); ENG["noise_hist"].clear(); st["udp"].pop("tx_df", None)
    ENG["spec_pending"].clear(); ENG["qrm_first"] = None; ENG["tx_iv"].clear()
    ENG["active"].clear(); ENG["calls_to"].clear(); ENG["spec_hist"].clear(); ENG["rr73_again"].clear()
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
check("caller queued", [c["call"] for c in ENG["callers"]] == ["DL1ABC"])
check("in-progress QSO not dropped for caller", st["target"] is t2)
fresh(); dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
dec("KR4MZM DL1ABC JO62")
check("unanswered target dropped for caller", st["target"] is None)
fresh(); ENG["idle_until"] = time.time() + 60
dec("KR4MZM DL1ABC JO62"); ns["engine_tick"]()
check("caller answered during a listen rest", st["target"] and st["target"]["call"] == "DL1ABC")

# --- several stations answer our CQ: work them all, in order
fresh(); st["config"]["call_cq"] = False
dec("KR4MZM/AG W1AA FN42", snr=-5); dec("KR4MZM/AG W2BB FN31", snr=0); dec("KR4MZM/AG W3CC EM73", snr=-12)
check("all three callers queued in order", [c["call"] for c in ENG["callers"]] == ["W1AA", "W2BB", "W3CC"])
dec("KR4MZM/AG W2BB FN31", snr=2)
check("repeat call keeps its place", [c["call"] for c in ENG["callers"]] == ["W1AA", "W2BB", "W3CC"])
ns["engine_tick"]()
check("first caller answered", st["target"]["call"] == "W1AA" and "2 more waiting" in ENG["step"]["detail"])
dec("KR4MZM/AG W1AA R-08")                       # their R-report: QSO under way
ns["on_my_tx"]("W1AA KR4MZM/AG RR73")             # we send RR73 (from ALL.TXT)
check("our RR73 marks the QSO done", st["target"]["done"] and st["target"]["left"] == 2)
dec("KR4MZM/AG W1AA R-08")                       # they repeat: didn't copy our RR73
check("their repeat keeps the QSO open", st["target"]["left"] == 99)
ns["on_my_tx"]("W1AA KR4MZM/AG RR73")
CTX["tx"].sent.clear(); ns["engine_tick"](); ns["engine_tick"]()
check("done -> next caller answered without a Halt",
      st["target"]["call"] == "W2BB" and HALT not in CTX["tx"].sent and ns["is_worked"]("W1AA"))
ENG["callers"][0]["t"] -= 200                     # W3CC gave up long ago
st["target"]["left"] = 1; ns["engine_tick"]()     # W2BB times out
check("caller not heard for 6 periods is dropped", not ENG["callers"] and st["target"] is None)

# --- regression: YS1CH answered in the period we timed out, and was ignored
fresh(); st["config"]["call_cq"] = False
now_par = ns["parity"](ns["tod_ms"](time.time()))
dec("CQ YS1CH EK54", snr=-5, t_ms=now_par * 15000)     # their CQ is in the current period's parity
ns["engine_tick"]()
check("starting in their period allows an extra period", st["target"]["left"] == 7)
st["target"] = None; ns["hold"]("YS1CH")                # tries ran out, held
dec("CQ EA8UP IL18", snr=-3); ns["engine_tick"]()        # moved on to the next CQ
check("moved on to EA8UP", st["target"]["call"] == "EA8UP")
dec("<KR4MZM/AG> YS1CH -12")                              # YS1CH's answer arrives late
check("late reply lifts the hold and goes first in line",
      not ns["on_hold"]("YS1CH") and ENG["callers"][0]["call"] == "YS1CH")
check("...and replaces the unanswered EA8UP target", st["target"] is None)
ns["engine_tick"]()
check("YS1CH answered", st["target"]["call"] == "YS1CH" and st["target"]["msg"] == "<KR4MZM/AG> YS1CH -12")

# --- tail-end targets the sender
fresh(); dec("JA1AAA VK2BBB RR73", snr=3); ns["engine_tick"]()
check("tail-end targets sender VK2BBB", st["target"] and st["target"]["call"] == "VK2BBB")

# --- worked checks
st["worked"] = {("N0CALL", None, None): None}
check("worked check ignores /P suffix", ns["is_worked"]("N0CALL/P"))
check("own call counts as worked", ns["is_worked"]("<KR4MZM/AG>"))

# --- contacts and holds count per band and mode
fresh(); st["udp"].update(band="20m", mode="FT8")
ns["add_worked"]("W9XYZ")
check("worked on 20m FT8", ns["is_worked"]("W9XYZ"))
st["udp"]["mode"] = "FT4"
check("same station not worked yet on 20m FT4", not ns["is_worked"]("W9XYZ"))
st["udp"].update(band="40m", mode="FT8")
check("same station not worked yet on 40m FT8", not ns["is_worked"]("W9XYZ"))
ns["hold"]("W9XYZ")
check("hold on 40m FT8", ns["on_hold"]("W9XYZ"))
st["udp"]["mode"] = "FT4"
check("hold doesn't carry to 40m FT4", not ns["on_hold"]("W9XYZ"))
st["udp"].update(band="20m", mode="FT8")
check("hold doesn't carry to 20m FT8", not ns["on_hold"]("W9XYZ"))
dec("CQ W9XYZ EN52"); st["udp"]["band"] = "40m"; dec("CQ W9XYZ EN52")
check("CQ from a station held on this band/mode is skipped",
      ns["cq_skip"](st["cq"]["W9XYZ"], time.time()) == "on hold")
import tempfile, datetime
home = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
os.makedirs(os.path.join(home, ".local/share/WS"))
today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
with open(os.path.join(home, ".local/share/WS/wsjtx_log.adi"), "w") as f:
    f.write(f"<call:5>K1ABC <qso_date:8>{today} <time_on:6>134502 <band:3>20m <mode:3>FT8 <eor>"
            f"<call:5>K2ABC <qso_date:8>{today} <freq:9>7.075500 <mode:4>MFSK <submode:3>FT4 <eor>"
            f"<call:5>K3ABC <qso_date:8>19990101 <band:3>20m <mode:3>FT8 <eor>")
old_home = os.environ["HOME"]; os.environ["HOME"] = home
w, _ = ns["load_worked"](); os.environ["HOME"] = old_home
check("ADIF: band, freq and FT4 submode read; old days ignored",
      set(w) == {("K1ABC", "20m", "FT8"), ("K2ABC", "40m", "FT4")})
check("ADIF time_on kept for the worked list",
      time.strftime("%H:%M", time.gmtime(w[("K1ABC", "20m", "FT8")])) == "13:45")
st["worked"] = w; st["udp"].update(band="20m", mode="FT4")
check("ADIF 20m FT8 contact doesn't block 20m FT4", not ns["is_worked"]("K1ABC"))
st["udp"]["mode"] = "FT8"
check("ADIF 20m FT8 contact blocks 20m FT8", ns["is_worked"]("K1ABC"))
fresh(); st["udp"]["band"] = None

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
st["udp"]["revision"] = "261013-hunter"
fresh(); ns["engine_tick"]()
s = CTX["tx"].sent
check("ClearDX build -> CQ selected with ClearDX, never Configure, then Enable Tx",
      s[-2:] == [CLEAR_DX, SET_TX_ENABLED] and CONFIGURE not in s)
check("revision gate: 261013 build has ClearDX, 260926 does not",
      ns["has_ws_hunter"]("261013") and (st["udp"].update(revision="260926-hunter")
      or not ns["has_ws_hunter"]("261013")) and ns["has_ws_hunter"]())
st["udp"]["revision"] = "261013-hunter"
# WS left on the last station (Tx1) during our CQ run: re-select CQ, twice at most
fresh(); ns["engine_tick"](); ENG["cq_start"] = time.time() - 10
st["udp"].update(tx_enabled=True, tx_msg="<XQ3SK> KR4MZM/AG", tx_t=time.time()); CTX["tx"].sent.clear()
ns["engine_tick"]()
check("CQ run with WS not on CQ -> ClearDX + Enable Tx again", CTX["tx"].sent == [CLEAR_DX, SET_TX_ENABLED])
for _ in range(3):
    ENG["cq_start"] = time.time() - 10; ns["engine_tick"]()
check("re-selecting CQ gives up after two tries", CTX["tx"].sent.count(CLEAR_DX) == 2)
fresh(); ns["engine_tick"](); ENG["cq_start"] = time.time() - 10
st["udp"].update(tx_enabled=True, tx_msg="CQ KR4MZM/AG"); CTX["tx"].sent.clear(); ns["engine_tick"]()
check("CQ run with WS on CQ and armed -> left alone", CTX["tx"].sent == [])
st["udp"].update(tx_enabled=False, tx_msg="", tx_t=0)
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
# --- CQ first mode: run our own CQ; answer callers and activators only
st["config"]["cq_first"] = True
fresh(); dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
check("CQ first: a plain CQ is not answered, we call CQ instead (even with call_cq off)",
      st["target"] is None and ENG["cq_until"] > time.time())
dec("JA1AAA VK2BBB RR73"); ns["engine_tick"]()
check("CQ first: no tail-ending", st["target"] is None)
dec("KR4MZM N5CAL EM15"); ns["engine_tick"]()
check("CQ first: a caller is answered", st["target"] and st["target"]["call"] == "N5CAL")
fresh(); ns["engine_tick"](); dec("CQ POTA K4PRK EM85"); ns["engine_tick"]()
check("CQ first: an activator still interrupts our CQ", st["target"] and st["target"]["call"] == "K4PRK")
st["config"]["special_first"] = False
fresh(); ns["engine_tick"](); dec("CQ POTA K4PRK EM85"); ns["engine_tick"]()
check("CQ first with activations-first off: activators skipped too", st["target"] is None and ENG["cq_until"])
st["config"].update(special_first=True, cq_first=False)
st["udp"]["revision"] = ""

# --- politeness / success strategies
ns["load_cty"]()
fresh(); dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
their_period_now = ns["parity"](ns["tod_ms"](time.time())) == 0   # CQ above was decoded in period 0
check("tries count transmissions (3 tries = 6 periods, +1 if starting in their period)",
      st["target"]["left"] == (7 if their_period_now else 6))
CTX["tx"].sent.clear(); dec("N0OTH K9XYZ -12")
check("target answers someone else -> stop calling, no hold",
      st["target"] is None and HALT in CTX["tx"].sent and not ns["on_hold"]("K9XYZ"))
fresh(); dec("CQ K9XYZ EN52", snr=5); dec("N0OTH K9XYZ -12"); ns["engine_tick"]()
check("station in a QSO since its CQ is skipped", st["target"] is None)
dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
check("...and called again once it CQs again", st["target"] and st["target"]["call"] == "K9XYZ")
fresh(); dec("CQ OLD1ABC EN52", snr=5); st["cq"]["OLD1ABC"]["seen"] -= 40; ns["engine_tick"]()
check("CQ older than 35 s skipped", st["target"] is None)
fresh(); dec("CQ W9WEAK EN52", snr=-24); ns["engine_tick"]()
check("CQ below min SNR skipped", st["target"] is None)
fresh(); dec("JA1AAA VK2BBB -10", snr=8); ns["engine_tick"]()
check("no tail-ending a QSO in progress", st["target"] is None)
fresh(); dec("A1AA B1BB 73", snr=10); dec("C1CC D1DD RR73", snr=-10); ns["engine_tick"]()
check("tail-end prefers RR73 (runner about to be free)", st["target"]["call"] == "D1DD")
fresh(); dec("CQ DX W5ABC EM12", snr=10); ns["engine_tick"]()
check("CQ DX from same continent (NA) not answered", st["target"] is None)
dec("CQ DX DL1ABC JO62", snr=-10); ns["engine_tick"]()
check("CQ DX from another continent answered", st["target"]["call"] == "DL1ABC")
check("country lookup handles prefix/ and hashed calls",
      ns["dxcc"]("VE3/K1ABC")[0] == "Canada" and ns["dxcc"]("<KR4MZM/AG>")[1] == "NA")
fresh(); dec("CQ K1PILE FN42", snr=0); dec("CQ K2QUIET FN42", snr=-6)
for c in ("N1AA", "N2BB", "N3CC"):
    dec(f"K1PILE {c} FN42")
ns["engine_tick"]()
check("3-deep pileup ranks below a quieter clear CQ", st["target"]["call"] == "K2QUIET")

# --- live decision-flow step + CQ skip reasons (shown in the UI)
fresh(); dec("CQ POTA K4PRK EM85", snr=-6); ns["engine_tick"]()
check("step: pounce on activator", ENG["step"]["step"] == "activator" and "K4PRK" in ENG["step"]["detail"])
ns["engine_tick"]()
check("step: then working the target", ENG["step"]["step"] == "target" and "tries left" in ENG["step"]["detail"])
st["running"] = False; ns["engine_tick"](); st["running"] = True
check("step: stopped", ENG["step"]["step"] == "stopped")
fresh(); dec("CQ K1HELD FN42"); ns["hold"]("K1HELD"); dec("CQ K9BUSY EN52"); dec("N0OTH K9BUSY -10")
dec("CQ EU DL9ABC JO40"); dec("CQ W9WEAK EN50", snr=-25)
skip = {c: ns["cq_skip"](e) for c, e in st["cq"].items()}
check("skip reasons: hold / in QSO / region / weak",
      skip == {"K1HELD": "on hold", "K9BUSY": "in QSO", "DL9ABC": "CQ EU", "W9WEAK": "weak"})

# --- frequency picker
fresh()
pick = ns["pick_tx_df"]
f = pick(1)
check("default lowest offset is 1300 Hz", st["config"]["df_lo"] == 1300)
check("empty band -> middle of the allowed 1300-2700 range", f is not None and 1900 <= f <= 2050)
check("a clear offset below the lowest allowed is moved up", pick(1, current=900) >= 1300)
for i, df in enumerate(range(300, 2700, 80)):  # crowd the odd period except one hole
    if not 1800 <= df < 2000:
        dec(f"W{i}AA K{i}BB -10", df=df, t_ms=15000)
f = pick(1)
check("picks the only clear gap (1800-2000)", f is not None and 1800 <= f and f + 50 <= 2000)
check("even period unaffected by odd-period crowd", 1900 <= pick(0) <= 2050)
check("keeps current offset while clear", pick(1, current=1850) == 1850)
check("moves off an occupied current offset", pick(1, current=700) != 700)
for i, df in enumerate(range(1800, 2000, 40)):
    dec(f"W{i}CC K{i}DD -10", df=df, t_ms=15000)
check("fully crowded period -> no pick", pick(1) is None)
f = pick(1, current=800)
check("crowded but below the lowest offset -> least-bad spot inside the range", f is not None and 1300 <= f)
st["config"]["df_lo"] = 300
fresh(); check("lowest offset setting is honoured (300 -> mid-band)", 1400 <= pick(1) <= 1500)
st["config"]["df_lo"] = 1300

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
def spectrum(levels, t_ms=15000, floor=-20.0, step=0.25, tr=15000):
    body = struct.pack(">IIII", t_ms, tr, 0, 10) + struct.pack(">dd", floor, step)
    body += struct.pack(">I", len(levels)) + bytes(levels)
    return struct.pack(">III", 0xADBCCBDA, 2, 27) + ps("WS") + body
def handle(pkt):
    ns["handle_spectrum"](pkt, 12 + 4 + 2)
def settle():  # pretend the decodes for every pending spectrum are in
    ns["check_qrm"](time.time() + 100)
quiet = [20] * 300                                  # flat noise
one_ft8 = quiet[:]; one_ft8[150:155] = [80] * 5     # one 50 Hz signal at 1500 Hz
fresh(); handle(spectrum(one_ft8))
check("spectrum marks hot bins", ENG["spec"][1]["hot"] == [1500, 1510, 1520, 1530, 1540])
f = pick(1, current=1510)
check("undecoded signal in spectrum moves the pick", f != 1510)
ssb = quiet[:]; ssb[30:280] = [90] * 250            # 300-2800 Hz voice signal fills the passband
fresh()
for i in range(2): handle(spectrum(ssb, t_ms=i * 15000))
settle(); check("no verdict before the noise floor is learned", ENG["qrm_until"] == 0)
fresh()
for i in range(8): handle(spectrum(quiet, t_ms=i * 15000))   # normal band: learn the floor
handle(spectrum(ssb, t_ms=120000)); handle(spectrum(ssb, t_ms=135000))
settle(); check("15 s of wide signal: not yet", ENG["qrm_until"] == 0)
handle(spectrum(ssb, t_ms=150000))
check("verdict waits for the period's decodes", ENG["qrm_until"] == 0)
settle(); check("30 s of wide undecoded signal -> back off", ENG["qrm_until"] > time.time())
ENG["cq_until"] = 0; dec("CQ K4PRK EM85", snr=-3); ns["engine_tick"]()
check("no new calls during backoff", st["target"] is None)
dec("KR4MZM/AG W5ANS EM12"); ns["engine_tick"]()
check("a station calling us is answered even during the backoff", st["target"]["call"] == "W5ANS")
# regression: busy FT2 band right after a mode change, decodes arriving after
# the spectrum, was flagged as "2160 Hz of undecoded signal"
fresh(); st["udp"]["mode"] = "FT2"
for i in range(8): handle(spectrum(quiet, t_ms=i * 3750, tr=3750))
busy_ft2 = quiet[:]
dfs = list(range(400, 2600, 180))
for df in dfs:
    for b in range(df // 10, (df + 167) // 10): busy_ft2[b] = 90
for i in range(8, 20):
    tm = i * 3750
    handle(spectrum(busy_ft2, t_ms=tm, tr=3750))
    for k, df in enumerate(dfs):                     # decodes for that period arrive afterwards
        dec(f"W{k}AA K{k}BB -10", df=df, t_ms=tm)
settle(); check("busy FT2 band whose decodes arrive late: no backoff", ENG["qrm_until"] == 0)
st["udp"]["mode"] = "FT8"
# regression: our own transmit periods (receiver muted, ~45 dB lower) were
# taken as the noise floor, so the real band looked like one wide signal
fresh()
for i in range(12):                                # band ~-15 dB, muted TX periods ~-65 dB
    handle(spectrum(quiet, t_ms=i * 15000, floor=-20.0 if i % 2 else -70.0))
check("muted (transmit) periods kept out of the noise floor once it is known",
      sum(1 for x in ENG["noise_hist"] if x < -40) == 2 and len(ENG["noise_hist"]) == 8)
for i in range(12, 16): handle(spectrum(quiet, t_ms=i * 15000))
settle(); check("normal band after our transmissions: no backoff", ENG["qrm_until"] == 0)
fresh(); ns["note_tx"](True)
handle(spectrum(quiet, t_ms=ns["tod_ms"](time.time()) // 15000 * 15000))
check("spectrum of a period we are transmitting in is ignored", not ENG["spec_pending"])
ns["note_tx"](False); ENG["tx_iv"].clear()
st["udp"]["revision"] = ""

# --- weak signals: a learned per-bin baseline, so a passband slope isn't a
# signal but a persistent -20 dB station (~+2 dB in its bins) is
import random
random.seed(1)
def noisy(extra=None):
    lv = [int(40 + 16 * (300 - i) / 300 + random.gauss(0, 2)) for i in range(300)]  # 4 dB slope, 0.5 dB noise
    for b, d in (extra or {}).items():
        lv[b] += d
    return lv
fresh()
T0 = (ns["tod_ms"](time.time()) // 30000 - 19) * 30000  # 18 periods ending just now, even first
for i in range(10): handle(spectrum(noisy(), t_ms=T0 + i * 15000))
check("passband slope and bin noise: nothing hot", ENG["spec"][1]["hot"] == [] and ENG["spec"][0]["hot"] == [])
weak = {b: 8 for b in range(180, 185)}               # +2 dB over 50 Hz at 1800 Hz, odd periods only
for i in range(10, 18): handle(spectrum(noisy(weak if i % 2 else None), t_ms=T0 + i * 15000))
hot = ENG["spec"][1]["hot"]
check("persistent weak signal found on its parity", hot and min(hot) >= 1780 and max(hot) <= 1860)
check("...and not on the other parity", ENG["spec"][0]["hot"] == [])
check("picker avoids the weak signal", not 1750 < pick(1, current=1800) < 1850)
wf = ns["waterfall"]()
row = next(r for r in wf["rows"] if "ex" in r and r["par"] == 1)
ex = bytes.fromhex(row["ex"])
check("waterfall rows carry excess over the baseline",
      len(wf["rows"]) == 40 and len(ex) == 300 and (ex[182] - 20) / 4 > 1 and abs((ex[100] - 20) / 4) < 2)
check("waterfall shows the weak signal as occupied", any(a <= 1800 < b for a, b in wf["occupied"][1]))

# --- a station we just logged that repeats its R-report never got our RR73
fresh(); st["running"] = True
ns["add_worked"]("KE0N")
dec("<KR4MZM/AG> KE0N R-15", t_ms=15000)
check("logged station repeating R-report is queued for RR73 again",
      [c["call"] for c in ENG["callers"]] == ["KE0N"] and ENG["callers"][0]["again"])
ns["engine_tick"]()
check("...and answered once, despite being in the log",
      st["target"] and st["target"]["call"] == "KE0N" and st["target"].get("again") and REPLY in CTX["tx"].sent)
ns["engine_tick"]()
check("...without being released as 'in the log' first", st["target"] and st["target"]["call"] == "KE0N")
fresh(); ns["add_worked"]("W1OLD"); st["worked"][("W1OLD", ns["band"](), ns["mode"]())] -= 600
dec("<KR4MZM/AG> W1OLD R-10")
check("not for a contact logged long ago", not ENG["callers"])
fresh(); ns["add_worked"]("W1GRD"); dec("KR4MZM/AG W1GRD EM10")
check("not for a fresh call (grid) from a logged station", not ENG["callers"])
fresh(); ns["add_worked"]("K1REP"); ENG["rr73_again"]["K1REP"] = 3
dec("KR4MZM/AG K1REP R-05")
check("at most 3 extra RR73s", not ENG["callers"])

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
          ns["is_worked"]("W1AW") and st["target"] is None)
fresh(); st["udp"].update(band="20m", mode="FT8")
send(5, struct.pack(">qIB", 2461327, 52_000_000, 1) + ps("K7ABC") + ps("DN31")
     + struct.pack(">Q", 7074000) + ps("FT4"))
check("QSO Logged records its own band and mode (40m FT4)",
      ("K7ABC", "40m", "FT4") in st["worked"] and not ns["is_worked"]("K7ABC"))
st["udp"]["band"] = None

# --- FT4 / FT2: everything follows the mode WS reports in Status
def status(mode_name, tx_df=1500):
    body = struct.pack(">Q", 14074000) + ps(mode_name) + ps("") + ps("-10") + ps(mode_name)
    body += bytes([0, 0, 0]) + struct.pack(">II", 1500, tx_df)
    send(1, body)
body = struct.pack(">Q", 14074000) + ps("FT8") + ps("XQ3SK") + ps("-10") + ps("FT8")
body += bytes([1, 0, 0]) + struct.pack(">II", 1500, 1500) + ps("KR4MZM/AG") + ps("FM18") + ps("")
body += bytes([1]) + ps("") + bytes([0, 0]) + struct.pack(">II", 20, 15) + ps("Default") + ps("<XQ3SK> KR4MZM/AG")
send(1, body)
check("Status: Tx message, watchdog and Enable Tx parsed",
      st["udp"]["tx_msg"] == "<XQ3SK> KR4MZM/AG" and st["udp"]["watchdog"] and st["udp"]["tx_enabled"])
fresh(); dec("CQ K9XYZ EN52", snr=5)
status("FT8"); check("FT8: 15 s periods", ns["period"]() == 15.0 and ns["parity"](15000) == 1)
status("FT4")
check("FT4: 7.5 s periods, odd at 7.5 s", ns["period"]() == 7.5 and ns["parity"](7500) == 1 and ns["parity"](15000) == 0)
check("mode change clears old-mode CQs", not st["cq"])
check("FT4 signal width used by the picker", ns["bw"]() == 83)
dec("CQ K9XYZ EN52", snr=5); st["cq"]["K9XYZ"]["seen"] -= 22; ns["engine_tick"]()
check("FT4: a CQ 22 s old (3 periods) is stale", st["target"] is None)
status("FT2")
check("FT2: 3.75 s periods", ns["period"]() == 3.75 and ns["parity"](3750) == 1)
dec("CQ K9XYZ EN52", snr=5); st["cq"]["K9XYZ"]["seen"] -= 10; ns["engine_tick"]()
check("FT2: a CQ 10 s old is still fresh", st["target"] and st["target"]["call"] == "K9XYZ")
status("FT8")
check("back to FT8 releases the FT2 target", st["target"] is None and ns["period"]() == 15.0)

# --- WRL Desktop bridge: WS packets forwarded to WRL, WRL's replies relayed to WS
wrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); wrl.bind(("127.0.0.1", 22998)); wrl.settimeout(2)
ws = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); ws.bind(("127.0.0.1", 0)); ws.settimeout(2)
ws.sendto(struct.pack(">III", 0xADBCCBDA, 2, 0) + ps("WS") + struct.pack(">I", 3) + ps("3.2.1") + ps("x"),
          ("127.0.0.1", 22999))  # WS heartbeat: hunter learns WS's address
try:
    pkt, fwd_addr = wrl.recvfrom(8192)
    check("WS packet forwarded to WRL", struct.unpack(">I", pkt[8:12])[0] == 0)
    halt = struct.pack(">III", 0xADBCCBDA, 2, 8) + ps("WS") + b"\x01"
    wrl.sendto(halt, fwd_addr)  # WRL answers the address it heard from
    back, _ = ws.recvfrom(8192)
    check("WRL reply relayed to WS", back == halt)
except socket.timeout:
    check("WRL bridge round trip", False)

# --- globe: positions from grids heard, else the country's centre
fresh(); ns["GRIDS"].clear(); ns["PAIRS"].clear(); st["mygrid"] = "EM84"; st["udp"].update(band="20m", mode="FT8")
dec("CQ JA1XYZ PM95"); dec("W1AW K9ABC EN52"); dec("K9ABC W1AW RR73")
check("grid learned from a CQ and from a reply, not from RR73",
      ns["GRIDS"].get("JA1XYZ") == "PM95" and ns["GRIDS"].get("K9ABC") == "EN52" and "W1AW" not in ns["GRIDS"])
ns["CTY"]["prefix"]["W"] = ("United States", "NA", 37.53, -91.67)
check("no grid: country centre", ns["locate"]("W1AW") == (37.53, -91.67))
ns["add_worked"]("JA1XYZ"); dec("KR4MZM/AG W1AW -10")
st["target"] = {"call": "K9ABC", "left": 5}
g = ns["globe"]()
dec("<...> K9ABC R-05")
check("traffic pairs keep both ends and skip hashed calls",
      ns["PAIRS"][frozenset(("K9ABC", "W1AW"))]["b"] == "K9ABC" and not any("." in p["a"] + p["b"] for p in ns["PAIRS"].values()))
ns["PAIRS"][frozenset(("K9ABC", "JA1XYZ"))] = {"a": "JA1XYZ", "b": "K9ABC", "t": time.time() - 400}
check("traffic older than the fade time is dropped", not any(x["from"] == "JA1XYZ" for x in ns["globe"]()["traffic"]))
ns["RECENT"][("VK2OLD", "20m", "FT8")] = time.time() - 3600 * 5  # yesterday UTC, 5 h ago
ns["RECENT"][("ZL1GONE", "20m", "FT8")] = time.time() - 3600 * 30
ns["CTY"]["prefix"]["VK"] = ("Australia", "OC", -23.7, 132.3); ns["CTY"]["prefix"]["ZL"] = ("New Zealand", "OC", -41.8, 174.0)
calls = [w["call"] for w in ns["globe"]()["worked"]]
check("globe keeps contacts from the last 24 h past UTC midnight, not older", "VK2OLD" in calls and "ZL1GONE" not in calls)
check("globe: me, contacts, target and traffic located",
      g["me"] and "JA1XYZ" in [w["call"] for w in g["worked"]] and g["target"]["call"] == "K9ABC" and g["target"]["ll"]
      and any({x["from"], x["to"]} == {"K9ABC", "W1AW"} for x in g["traffic"])
      and any(x["tome"] and x["b"] == g["me"] for x in g["traffic"]))
check("globe: a target we are only calling is not a QSO yet", not g["target"]["resp"])
st["target"] = {"call": "K9ABC", "left": 5, "why": "called us"}
check("globe: a station that called us is a QSO (orange) from the start", ns["globe"]()["target"]["resp"])
ENG["callers"].clear(); ENG["callers"].append({"call": "W1AW", "msg": "KR4MZM W1AW FN31", "t": time.time()})
ENG["callers"].append({"call": "K9ABC", "msg": "KR4MZM K9ABC EN52", "t": time.time()})
check("globe: others answering us while in a QSO are listed (not the target)",
      [c["call"] for c in ns["globe"]()["callers"]] == ["W1AW"])
st["target"] = None; ENG["callers"].pop()
check("globe: a single station answering us is not shown as 'also answering'", ns["globe"]()["callers"] == [])
ENG["callers"].clear()
# operating WS by hand: its DX Call is the target while we are transmitting to it
st["udp"].update(dxcall="DL1ABC", tx_enabled=True); ns["note_grid"]("DL1ABC", "JO62")
g = ns["globe"]()["target"]
check("globe: manual operation shows WS's DX Call as the target", g and g["call"] == "DL1ABC" and g["ll"] and not g["resp"])
dec("KR4MZM/AG DL1ABC -07")
check("globe: ...a QSO once they answer us", ns["globe"]()["target"]["resp"])
st["udp"]["tx_enabled"] = False
check("globe: ...and gone once Tx is off and we haven't transmitted for a minute", ns["globe"]()["target"] is None)
ENG["tx_iv"].append([time.time() - 20, time.time() - 5])
check("globe: ...but kept just after our last transmission", ns["globe"]()["target"]["call"] == "DL1ABC")
ENG["tx_iv"].clear(); st["udp"].update(dxcall="", tx_enabled=False)

# --- worked / ignored list in /api/state: time, band, mode, kind
import json, urllib.request
from http.server import ThreadingHTTPServer
fresh(); st["udp"].update(band="20m", mode="FT8")
ns["add_worked"]("K1ABC"); ns["add_worked"]("K2ABC", "40m", "FT4")
ns["hold"]("W9NOP"); ns["hold"]("W8LATE"); ns["unhold"]("W8LATE")
srv = ThreadingHTTPServer(("127.0.0.1", 0), ns["H"])
threading.Thread(target=srv.serve_forever, daemon=True).start()
api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/state"))
book = {e["call"]: e for e in api["book"]}
check("book lists contacts on every band/mode with time",
      book["K2ABC"]["band"] == "40m" and book["K2ABC"]["mode"] == "FT4"
      and book["K1ABC"]["kind"] == "worked" and book["K1ABC"]["t"])
check("book lists a station we gave up on as ignored, with its hold",
      book["W9NOP"]["kind"] == "ignored" and book["W9NOP"]["band"] == "20m" and book["W9NOP"]["hold_s"] > 0)
check("a station that answered after its hold isn't listed as ignored", "W8LATE" not in book)
check("worked count is for this band/mode only", api["worked"] == ["K1ABC"])

# --- radio via FLRig XML-RPC: meters polled, TX power set and clamped
from xmlrpc.server import SimpleXMLRPCServer
RADIO = {"power": 16, "ptt": 1}
fl = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=True)
fl.register_multicall_functions()
for name, f in {"rig.get_xcvr": lambda: "Xiegu-G90", "rig.get_maxpwr": lambda: 0,
                "rig.get_ptt": lambda: RADIO["ptt"], "rig.get_power": lambda: RADIO["power"],
                "rig.get_pwrmeter": lambda: "15", "rig.get_swrmeter": lambda: "12", "rig.get_smeter": lambda: "0",
                "rig.set_power": lambda w: RADIO.update(power=w) or 0}.items():
    fl.register_function(f, name)
threading.Thread(target=fl.serve_forever, daemon=True).start()
ns["RIG"]["url"] = f"http://127.0.0.1:{fl.server_address[1]}"
threading.Thread(target=ns["rig_poll"], daemon=True).start(); time.sleep(1.0)
R = ns["RIG"]
check("FLRig polled: rig, max power from the table, PTT, no S-meter",
      R["ok"] and R["xcvr"] == "Xiegu-G90" and R["max"] == 20 and R["power"] == 16 and R["ptt"]
      and not R.get("has_s"))
def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))
post("/api/rig/power", {"watts": 50})
check("TX power set through FLRig, clamped to the rig's max", RADIO["power"] == 20)
post("/api/rig/power", {"watts": 5})
check("TX power lowered", RADIO["power"] == 5)
post("/api/config", {"df_lo": 1400})
saved = json.load(open(ns["CONFIG_FILE"]))
st["config"]["df_lo"] = 1300; ns["load_config"]()
check("lowest offset saved from Settings and loaded again after a restart",
      saved["df_lo"] == 1400 and st["config"]["df_lo"] == 1400)
post("/api/config", {"df_lo": 50}); lo1 = st["config"]["df_lo"]
post("/api/config", {"df_lo": 2690}); lo2 = st["config"]["df_lo"]
check("lowest offset kept between 200 Hz and 300 Hz under the top", lo1 == 200 and lo2 == 2400)
fl.shutdown(); srv.shutdown()

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
