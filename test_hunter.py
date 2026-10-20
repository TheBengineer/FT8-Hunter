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
_tmp = tempfile.mkdtemp()
ns["CONFIG_FILE"] = os.path.join(_tmp, "hunter_config.json")  # never the real settings
ns["SCAN_FILE"], ns["FREQ_FILE"] = os.path.join(_tmp, "scan.json"), os.path.join(_tmp, "freqs.json")
ns["SWR_FILE"] = os.path.join(_tmp, "swr.json")
ns["ORIGIN_FILE"] = os.path.join(_tmp, "origins.json")
ns["LB_FILE"], ns["LB_QUEUE_FILE"] = os.path.join(_tmp, "lb.json"), os.path.join(_tmp, "lbq.json")


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
    ENG.update(idle_until=0, cq_until=0, idle_n=0, qrm_until=0, qrm_n=0, arrived=None, tx_n=0, check_until=0, fresh_check=0)
    ENG["occ"].clear(); ENG["spec"].clear(); ENG["noise_hist"].clear(); st["udp"].pop("tx_df", None)
    ENG["spec_pending"].clear(); ENG["qrm_first"] = None; ENG["tx_iv"].clear(); ENG["tune_iv"].clear()
    ENG["active"].clear(); ENG["calls_to"].clear(); ENG["spec_hist"].clear(); ENG["rr73_again"].clear()
    CTX["tx"].sent.clear()

def recent_tm(par, back=0):
    """Start (UTC ms of day) of the last ended FT8 period of parity `par`,
    or `back` same-parity periods before it."""
    t = (ns["tod_ms"](time.time()) // 15000 - 1) * 15000
    if (t // 15000) % 2 != par:
        t -= 15000
    return (t - back * 30000) % 86400000

def ps(s):
    b = s.encode(); return struct.pack(">i", len(b)) + b


CTX["tx"] = FakeSock()
st.update(mycall="KR4MZM", running=True)
real_manual_on = ns["manual_on"]
# the engine tests below run without a ws-hunter revision; contest mode is
# forced then (tested on its own further down), so here only the setting counts
ns["manual_on"] = lambda: bool(st["config"].get("manual"))
real_heard_cycle = ns["heard_cycle"]  # most tests have no spectra: count a cycle as heard
ns["heard_cycle"] = lambda now: True
real_sleep = time.sleep
time.sleep = lambda s: None  # skip start_cq's wait for WS (shared time module)
st["config"]["call_cq"] = False

# --- WS files: Linux and macOS data folders
fs = ns["ws_files"]("ALL.TXT")
check("WS files looked for on Linux and macOS, WS first",
      fs[0].endswith("/.local/share/WS/ALL.TXT") and any("Library/Application Support/WS/ALL.TXT" in f for f in fs))

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
check("reply to hashed /AG call detected", t["resp"] and t["left"] == 15)
n = len(CTX["tx"].sent); ns["engine_tick"]()
check("no Reply re-sent once QSO is progressing", len(CTX["tx"].sent) == n)
dec("KR4MZM/AG <W1AW> RR73")
check("sign-off -> done, short release", t["done"] and t["left"] == 2)
ns["engine_tick"](); ns["engine_tick"]()
check("done QSO released and marked worked", st["target"] is None and ns["is_worked"]("W1AW"))

# --- sign-off 73s: Tx5 set to free text with our call, standard 73 kept as an option
class RawSock(FakeSock):
    def sendto(self, data, addr): super().sendto(data, addr); self.raw.append(data)
def free_texts(sock):
    out = []
    for d in sock.raw:
        if struct.unpack(">I", d[8:12])[0] == ns["FREE_TEXT"]:
            p = 12 + 4 + struct.unpack(">i", d[12:16])[0]
            n = struct.unpack(">i", d[p:p+4])[0]
            out.append((d[p+4:p+4+n].decode(), d[p+4+n]))
    return out
_sock, _addr = CTX["tx"], st.get("ws_addr")
CTX["tx"] = RawSock(); CTX["tx"].raw = []; st["ws_addr"] = ("127.0.0.1", 9)
_so = st["config"]["signoff"]
fresh(); st["config"]["signoff"] = ["GL73"]
dec("CQ W1AW FN31", snr=0); ns["engine_tick"]()
t = st["target"]
check("sign-off: Tx5 set to our call + text, 73 split off so WS logs it (not sent)", t["signoff"] == "KR4MZM GL 73"
      and free_texts(CTX["tx"]) == [("KR4MZM GL 73", 0)])
dec("<KR4MZM/AG> W1AW -10")
check("sign-off: set again on their reply", free_texts(CTX["tx"])[-1] == ("KR4MZM GL 73", 0) and len(free_texts(CTX["tx"])) == 2)
dec("KR4MZM/AG <W1AW> RR73"); ns["on_my_tx"]("KR4MZM GL 73")
check("sign-off: their RR73 completes the QSO; our free-text 73 follows", t["done"] and t["left"] == 2)
fresh(); CTX["tx"].raw.clear(); st["config"]["signoff"] = ["73"]
dec("CQ W1AW FN31", snr=0); ns["engine_tick"]()
check("sign-off: '73' leaves WS's standard message", st["target"]["signoff"] is None and not free_texts(CTX["tx"]))
st["config"]["signoff"] = ["MUCH TOO LONG 73"]
check("sign-off: over 13 characters with our call falls back to standard", ns["signoff_text"]() is None)

st["config"]["signoff"] = ["GL73", "TU73"]
check("sign-off: picked from the list", {ns["signoff_text"]() for _ in range(50)} == {"KR4MZM GL 73", "KR4MZM TU 73"})
st["config"]["signoff"] = ["GL 73", "TNX QSO GL 73"]
check("sign-off: our call always in it, lines over 6 characters skipped",
      {ns["signoff_text"]() for _ in range(50)} == {"KR4MZM GL 73"})
cat = ns["SIGNOFF_CATALOG"]
check("catalog: every line has a 73 word and fits after our call",
      all(ns["signoff_fix"](x) == x and ns["sign_msg"](x) for x in cat) and len(cat) >= 20)
st["config"]["signoff"] = _so; CTX["tx"] = _sock; st["ws_addr"] = _addr

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
check("their repeat keeps the QSO open", st["target"]["left"] == 15)
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

# --- compatibility mode: no ws-hunter commands; the TX light says when to Enable Tx
fresh(); st["udp"].update(revision="261013-hunter", version="3.0", mode="FT8", tx_enabled=False, watchdog=False)
check("ws-hunter: no TX light (the hunter enables TX itself)", ns["tx_light"]() is None and ns["ws_caps"]() == "hunter")
st["config"]["compat"] = True
check("compat: ws-hunter commands off even on the ws-hunter build", not ns["has_ws_hunter"]() and ns["ws_caps"]() == "configure")
check("compat: nothing to send -> light idle", ns["tx_light"]()["state"] == "idle")
dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
check("compat: a call goes out by Reply, never Enable Tx over UDP", st["target"]["call"] == "K9XYZ" and REPLY in CTX["tx"].sent
      and SET_TX_ENABLED not in CTX["tx"].sent)
ns["TX_LIGHT"]["want_t"] = time.time() - 5
check("compat: wants TX, Enable Tx off -> light says enable", ns["tx_light"]()["state"] == "enable"
      and "K9XYZ" in ns["tx_light"]()["msg"])
st["udp"]["tx_enabled"] = True
check("compat: Enable Tx on -> light ok", ns["tx_light"]()["state"] == "ok")
st["udp"]["watchdog"] = True
check("compat: TX watchdog tripped -> light says enable again", ns["tx_light"]()["state"] == "reenable")
st["udp"].update(watchdog=False, mode="JT65"); CTX["tx"].sent.clear(); ns["TX_LIGHT"]["mode_t"] = 0
check("compat: not on FT8 -> asks WSJT-X to switch (Configure) and the light says so",
      ns["tx_light"]()["state"] == "mode" and CTX["tx"].sent == [CONFIGURE])
st["udp"]["version"] = "1.9.1"
check("old WSJT-X (before 2.0): basic controls only", ns["ws_caps"]() == "basic")
st["udp"].update(mode="FT8", tx_enabled=False, revision="", version=""); st["config"]["compat"] = False
ns["TX_LIGHT"].update(want_t=0, mode_t=0); fresh()

# --- without ws-hunter, contest (manual) mode is forced: nothing starts on its own
ns["manual_on"] = real_manual_on
fresh(); st["udp"]["revision"] = ""; dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
check("no ws-hunter: contest mode forced, the CQ is only suggested",
      ns["manual_on"]() and st["target"] is None and ENG["step"]["step"] == "manual"
      and [x["call"] for x in ns["suggestions"]()] == ["K9XYZ"])
st["udp"]["revision"] = "261013-hunter"; ns["engine_tick"]()
check("ws-hunter, contest mode off: the hunter calls by itself", not ns["manual_on"]() and st["target"]["call"] == "K9XYZ")
fresh(); st["config"]["compat"] = True
check("compatibility mode on the ws-hunter build: contest mode forced too", ns["manual_on"]())
st["config"]["compat"] = False; st["udp"]["revision"] = ""
ns["manual_on"] = lambda: bool(st["config"].get("manual")); fresh()

# --- listen one cycle for interference: every N transmissions between QSOs, and after a timeout
fresh(); st["config"]["check_every"] = 10
for _ in range(10): ns["note_tx"](True); ns["note_tx"](False)
st["target"] = {"call": "K1QSO", "grid": "", "left": 9, "why": "CQ", "msg": "", "resp": True}
dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
check("interference check waits while in a QSO", st["target"]["call"] == "K1QSO" and not ENG["check_until"])
st["target"] = None; CTX["tx"].sent.clear(); ns["engine_tick"]()
check("after 10 TX between QSOs: halt, no new call, for one cycle",
      st["target"] is None and HALT in CTX["tx"].sent and ENG["step"]["step"] == "check"
      and ns["cyc"](1) < ENG["check_until"] - time.time() < ns["cyc"](2) and ENG["tx_n"] == 0)
ENG["check_until"] = time.time() - 1; ns["engine_tick"]()
check("...then hunting resumes", st["target"] and st["target"]["call"] == "K9XYZ")
st["target"]["left"] = 1; CTX["tx"].sent.clear(); ns["engine_tick"]()
check("calling timeout: listen one cycle too", st["target"] is None and ENG["check_until"] > time.time()
      and ENG["step"]["step"] == "check")
fresh(); dec(f"{st['mycall']} N5CAL EM15"); ENG["check_until"] = time.time() + 20; ns["engine_tick"]()
check("a station calling us is still answered during the check", st["target"] and st["target"]["call"] == "N5CAL")
fresh(); st["config"]["check_every"] = 0
for _ in range(30): ns["note_tx"](True); ns["note_tx"](False)
dec("CQ K9XYZ EN52", snr=5); ns["engine_tick"]()
check("check_every 0: never pauses", st["target"] and not ENG["check_until"])
st["config"]["check_every"] = 10; fresh()
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
        dec(f"W{i}AA K{i}BB -10", df=df, t_ms=recent_tm(1))
f = pick(1)
check("picks the only clear gap (1790-2060, between the stations at 1740 and 2060)",
      f is not None and 1790 + 15 <= f and f + 50 + 15 <= 2060)
check("even period unaffected by odd-period crowd", 1900 <= pick(0) <= 2050)
check("keeps current offset while clear", pick(1, current=1850) == 1850)
check("moves off an occupied current offset", pick(1, current=700) != 700)
for i, df in enumerate(range(1800, 2000, 40)):
    dec(f"W{i}CC K{i}DD -10", df=df, t_ms=recent_tm(1))
dec("K9LOUD W9LOUD -01", df=1700, t_ms=recent_tm(1))
f = pick(1, current=1710)
check("fully crowded: off a strong station onto a weaker one", f is not None and not 1650 < f < 1750)
check("...but no hopping between equally crowded spots", pick(1, current=1460) == 1460)
f = pick(1, current=800)
check("crowded but below the lowest offset -> least-bad spot inside the range", f is not None and 1300 <= f)
fresh(); dec("K1OLD W2LEFT -10", df=1900, t_ms=recent_tm(1, back=2))  # 3 odd periods ago
check("a frequency given up two cycles ago is free again", 1900 <= pick(1, current=1900) < 1950)
fresh(); dec("K1NOW W2STAY -10", df=1900, t_ms=recent_tm(1, back=1))  # the odd period before last
check("...but one used in the last two cycles is not", pick(1, current=1900) != 1900)
# we transmitted on the odd periods for the last three cycles: the last two
# odd periods we HEARD are older, and a station there still counts
def tx_in(tm):  # our transmission filling the period that starts at tm
    t0 = time.time() - (ns["tod_ms"](time.time()) - tm) / 1000
    ENG["tx_iv"].append([t0 + 0.2, t0 + 13])
fresh(); dec("K1OLD W2HERE -10", df=1900, t_ms=recent_tm(1, back=3))
for k in (2, 1, 0): tx_in(recent_tm(1, back=k))
check("transmitting for cycles doesn't make the frequency look free", pick(1, current=1900) != 1900)
fresh(); dec("K1OLD W2HERE -10", df=1900, t_ms=recent_tm(1, back=3))
check("...while heard-and-quiet cycles still free it", 1900 <= pick(1, current=1900) < 1950)
fresh(); dec("K1EDGE W2EDGE -18", df=1940, t_ms=recent_tm(1))  # brushes 10 Hz of [1900, 1950)
check("a weak station brushing our edge isn't worth a move", pick(1, current=1900) == 1900)
check("...one on top of us is", pick(1, current=1940) != 1940)
st["config"]["df_lo"] = 300
fresh(); check("lowest offset setting is honoured (300 -> mid-band)", 1400 <= pick(1) <= 1500)
st["config"]["df_lo"] = 1300

# answering a station on the even period moves us to a clear odd offset
fresh(); st["udp"]["revision"] = "260926-hunter"; st["udp"]["tx_df"] = 1500
dec("K1CQ W2BUSY -05", df=1480, t_ms=recent_tm(1))       # odd period traffic on our offset
dec("CQ K4PRK EM85", snr=-3, df=900, t_ms=recent_tm(0))       # even-period CQ: we answer on odd
ns["engine_tick"]()
s = CTX["tx"].sent
check("answer: hold + offset change before the Reply",
      s.index(SET_HOLD) < s.index(REPLY) and s.index(SET_TX_DF) < s.index(REPLY))
fresh(); st["udp"]["revision"] = ""; st["udp"]["tx_df"] = 1500
dec("CQ K4PRK EM85", snr=-3, df=900, t_ms=recent_tm(0)); ns["engine_tick"]()
check("stock WS: no control messages, suggestion only",
      CTX["tx"].sent == [REPLY] and ENG["suggest"]["par"] == 1)
fresh(); st["udp"]["revision"] = "260926-hunter"; st["udp"]["tx_df"] = 1500; st["config"]["df_lock"] = True
dec("K1CQ W2BUSY -05", df=1480, t_ms=recent_tm(1))       # busy on our locked offset
dec("CQ K4PRK EM85", snr=-3, df=900, t_ms=recent_tm(0)); ns["engine_tick"]()
s = CTX["tx"].sent
check("locked offset: hold sent, offset never moved, shown as busy",
      SET_HOLD in s and SET_TX_DF not in s and REPLY in s and ENG["suggest"]["locked"] and ENG["suggest"]["busy"])
st["config"]["df_lock"] = False
# no full cycle heard: listen one before moving the offset, then move
ns["heard_cycle"] = real_heard_cycle
def heard(par, back=0):
    ENG["spec_hist"].append({"t": time.time(), "t_ms": recent_tm(par, back), "tr_ms": 15000, "par": par,
                             "start": 0, "bin": 10, "rel": [0.0] * 300})
fresh(); st["udp"]["tx_df"] = 1500
dec("K1CQ W2BUSY -05", df=1480, t_ms=recent_tm(1))  # traffic on our offset (decoded, but no spectra)
dec("CQ K4PRK EM85", snr=-3, df=900, t_ms=recent_tm(0)); ns["engine_tick"]()
s = CTX["tx"].sent
check("no full cycle heard: listen first, no Reply, offset not moved",
      REPLY not in s and SET_TX_DF not in s and HALT in s and ENG["check_until"] > time.time()
      and ENG["step"]["step"] == "check")
heard(0); heard(1); check("both periods of the last cycle heard -> a full cycle", real_heard_cycle(time.time()))
ENG["check_until"] = 0; CTX["tx"].sent.clear(); ns["engine_tick"]()
s = CTX["tx"].sent
check("...then the offset moves and the call goes out", SET_TX_DF in s and REPLY in s and st["target"]["call"] == "K4PRK")
fresh(); heard(1, back=1); heard(0, back=1)
check("a cycle older than that is not", not real_heard_cycle(time.time()))
fresh(); st["udp"]["tx_df"] = 1500; ENG["fresh_check"] = time.time() - 40
dec("K1CQ W2BUSY -05", df=1480, t_ms=recent_tm(1))
dec("CQ K4PRK EM85", snr=-3, df=900, t_ms=recent_tm(0)); ns["engine_tick"]()
check("still nothing heard right after a listen (no spectra): move by what we have",
      SET_TX_DF in CTX["tx"].sent and REPLY in CTX["tx"].sent)
ns["heard_cycle"] = lambda now: True

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
fresh(); handle(spectrum(one_ft8, t_ms=recent_tm(1)))
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
fresh(); ns["note_tx"](True); ENG["tx_iv"][-1][0] -= 20  # on air for 20 s now
handle(spectrum(quiet, t_ms=ns["tod_ms"](time.time() - 10) // 15000 * 15000))
check("spectrum of a period we are transmitting in is ignored", not ENG["spec_pending"])
ns["note_tx"](False); ENG["tx_iv"].clear()
# regression: transmitting every other period, PTT up right at the start of
# ours and the drop reported late, threw away every receive period too
fresh(); tod0 = ns["tod_ms"](time.time()) // 30000 * 30000 - 60000  # even periods start here
base = time.time() - (ns["tod_ms"](time.time()) - tod0) / 1000
for k in (1, 3):                                   # our odd periods: 0.0 s to 14.6 s
    ENG["tx_iv"].append([base + k * 15 + 0.0, base + k * 15 + 14.6])
handle(spectrum(quiet, t_ms=tod0 + 30000))
check("receive period between two of our transmissions is kept", len(ENG["spec_hist"]) == 1)
handle(spectrum(quiet, t_ms=tod0 + 45000))
check("...but our own transmit period is not", len(ENG["spec_hist"]) == 1)
ENG["tx_iv"].clear()
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
T0 = (ns["tod_ms"](time.time()) // 30000 - 9) * 30000  # 18 periods ending just now, even first
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
handle(spectrum(noisy(), t_ms=T0 + 18 * 15000)); handle(spectrum(noisy(), t_ms=T0 + 19 * 15000))
handle(spectrum(noisy(), t_ms=T0 + 20 * 15000)); handle(spectrum(noisy(), t_ms=T0 + 21 * 15000))
check("...and free again once it has been gone two cycles", not any(1780 <= f <= 1860 for f in ENG["spec"][1]["hot"]))

# --- a QSO where they stop answering: 7 of our replies without an answer, then give up
fresh(); st["running"] = True
dec("CQ K7GONE DN31", snr=-5, t_ms=recent_tm(0)); ns["engine_tick"]()
dec("KR4MZM/AG K7GONE -12")                        # they answer: QSO under way
for i in range(14): ns["engine_tick"]()           # 7 of our periods, silence
check("still in the QSO through 7 unanswered replies", st["target"] and st["target"]["call"] == "K7GONE")
ns["engine_tick"]()
check("...then given up and held", st["target"] is None and ns["on_hold"]("K7GONE"))
fresh(); st["running"] = True
dec("CQ K7SLOW DN31", snr=-5, t_ms=recent_tm(0)); ns["engine_tick"]()
dec("KR4MZM/AG K7SLOW -12")
for i in range(10): ns["engine_tick"]()
dec("KR4MZM/AG K7SLOW RR73")                       # an answer resets the count
for i in range(2): ns["engine_tick"]()
check("their sign-off after 5 unanswered replies still completes the QSO",
      st["target"] is None and ns["is_worked"]("K7SLOW") and not ns["on_hold"]("K7SLOW"))
fresh(); st["running"] = True
dec("CQ K7MORE DN31", snr=-5, t_ms=recent_tm(0)); ns["engine_tick"]()
dec("KR4MZM/AG K7MORE -12")
for i in range(10): ns["engine_tick"]()
dec("KR4MZM/AG K7MORE R-10")
for i in range(10): ns["engine_tick"]()
check("each answer buys 7 more replies", st["target"] and st["target"]["call"] == "K7MORE")

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

# --- RRR: always answered with a 73
fresh(); st["running"] = True
ns["add_worked"]("KC3VT")
dec("KR4MZM KC3VT RRR", t_ms=15000)
check("logged station repeating RRR is queued for our 73 again",
      [c["call"] for c in ENG["callers"]] == ["KC3VT"] and ENG["callers"][0]["again"])
ns["engine_tick"]()
check("...and answered (Reply to their RRR: WS sends Tx5, the 73)",
      st["target"] and st["target"]["call"] == "KC3VT" and st["target"]["why"] == "73 again" and REPLY in CTX["tx"].sent)
fresh(); dec("CQ K3YTL FN21", snr=0); ns["engine_tick"](); dec("KR4MZM K3YTL -09")
ns["add_worked"]("KC3VT"); CTX["tx"].sent.clear()
dec("KR4MZM KC3VT RRR")
check("RRR again during another QSO: queued, the QSO in progress kept",
      st["target"]["call"] == "K3YTL" and [c["call"] for c in ENG["callers"]] == ["KC3VT"] and HALT not in CTX["tx"].sent)
fresh(); dec("CQ KF5DO EM34", snr=0); ns["engine_tick"](); dec("KR4MZM KF5DO -04")
t = st["target"]; CTX["tx"].sent.clear()
dec("KR4MZM KF5DO RRR")
check("first RRR: done, WS sends the 73 itself", t["done"] and REPLY not in CTX["tx"].sent)
ns["on_my_tx"]("KR4MZM HH 73"); dec("KR4MZM KF5DO RRR")
check("RRR again from our target: we send the 73 again", CTX["tx"].sent.count(REPLY) == 1)
dec("KR4MZM KF5DO RRR"); dec("KR4MZM KF5DO RRR"); dec("KR4MZM KF5DO RRR")
check("...at most 3 times", CTX["tx"].sent.count(REPLY) == 3)
fresh(); dec("CQ K3YTL FN21", snr=0); ns["engine_tick"](); dec("KR4MZM K3YTL -09")
t = st["target"]; t["signoff"] = "KR4MZM TY 73"; ns["on_my_tx"]("KR4MZM TY 73")
check("our free-text 73 alone doesn't end the QSO (it names no one)", not t.get("done"))
st["udp"]["dxcall"] = "KC3VT"; CTX["tx"].sent.clear(); ns["engine_tick"]()
check("WS switched DX Call mid-QSO: pointed back at our partner",
      st["target"] is t and REPLY in CTX["tx"].sent)
st["udp"]["dxcall"] = "K3YTL"; CTX["tx"].sent.clear(); ns["engine_tick"]()
check("...and left alone while WS is on them", REPLY not in CTX["tx"].sent)
st["udp"]["dxcall"] = ""

# --- real UDP packets through udp_listener
ns["udp_port"] = 22999; st["config"]["wrl_port"] = 22998
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

# --- online logbooks: WS's Logged ADIF goes to each logbook turned on
LB = ns["LB"]
ADIF = ("\n<adif_ver:5>3.1.0\n<programid:6>WSJT-X\n<EOH>\n<call:5>K7ABC <gridsquare:4>DN31 <mode:3>FT8 "
        "<qso_date:8>20261019 <time_on:6>140300 <band:3>20m <station_callsign:6>KR4MZM <EOR>\n")
rec = ns["adif_record"](ADIF)
check("ADIF record: header dropped, one line, ends <eor>",
      rec.startswith("<call:5>K7ABC") and rec.endswith("<EOR>") and "\n" not in rec)
check("ADIF field read by its length", ns["adif_field"](rec, "station_callsign") == "KR4MZM")
LB["queue"].clear(); LB["cfg"].clear()
send(12, ps(ADIF))
check("no logbook turned on: nothing queued", not LB["queue"])
LB["cfg"]["qrz"] = {"on": True, "key": ""}
ns["lb_enqueue"](ADIF)
check("turned on without its key: nothing queued", not LB["queue"])
LB["cfg"]["qrz"]["key"] = "ABCD-1234"
LB["wake"].set = lambda: None  # no worker thread here: lb_run is called by hand
send(12, ps(ADIF)); ns["lb_enqueue"](ADIF)
check("Logged ADIF packet queues the contact for QRZ, once",
      len(LB["queue"]) == 1 and LB["queue"][0]["call"] == "K7ABC")
posts, answers = [], []
def fake_post(url, data, timeout=20):
    posts.append(data); a = answers.pop(0)
    if isinstance(a, Exception): raise a
    return a
ns["lb_post"] = fake_post
answers.append(OSError("Network is unreachable")); ns["lb_run"]()
it = LB["queue"][0]
check("QRZ unreachable: kept, retried in a minute", it["tries"] == 1 and 50 < it["next"] - time.time() < 70)
ns["lb_run"]()
check("...and not before", len(posts) == 1)
answers.append((200, "RESULT=FAIL&REASON=invalid api key ABCD-1234&EXTENDED=")); ns["lb_run"](it["next"] + 1)
check("bad key: held for the operator, with QRZ's reason",
      it.get("held") and "api key" in it["err"] and posts[-1]["ADIF"] == rec and posts[-1]["ACTION"] == "INSERT")
ns["lb_run"](time.time() + 99999)
check("...held contacts aren't sent again by themselves", len(posts) == 2)
v = ns["lb_view"]()[0]
check("the page sees the key is set, never the key", v["fields"][0]["set"] and v["fields"][0]["value"] == "" and v["held"] == 1)
ns["lb_update"]("qrz", {"on": True, "fields": {"key": ""}})
check("Save with the key left blank keeps it and lets held contacts go",
      LB["cfg"]["qrz"]["key"] == "ABCD-1234" and not it.get("held"))
answers.append((200, "RESULT=OK&LOGID=130877825&LOGIDS=130877825&COUNT=1")); ns["lb_run"]()
check("QRZ OK: sent and off the queue", not LB["queue"] and LB["status"]["qrz"]["sent"] == 1)
ns["lb_enqueue"](ADIF); answers.append((200, "RESULT=FAIL&REASON=Unable to add QSO to database: duplicate&COUNT=0"))
ns["lb_run"]()
check("QRZ duplicate counts as done", not LB["queue"] and LB["status"]["qrz"]["dupes"] == 1)
check("logbook keys saved owner-only", os.stat(ns["LB_FILE"]).st_mode & 0o777 == 0o600)
answers.append((200, "RESULT=OK&CALLSIGN=KR4MZM&COUNT=812&BOOKID=1"))
check("QRZ test: the logbook's call and size", ns["qrz_test"]({"key": "x"}) == (True, "logbook KR4MZM, 812 contacts"))
LB["cfg"].clear(); LB["status"].clear()

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
tl = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/timeline"))["qsos"]
check("time bar: contacts of the last 24 h with time, band and mode, oldest first",
      [q["call"] for q in tl][-2:] == ["K1ABC", "K2ABC"] and tl[-1]["band"] == "40m" and tl[-1]["mode"] == "FT4"
      and all(a["t"] <= b["t"] for a, b in zip(tl, tl[1:])))

# --- manual (contest) mode: the hunter ranks, the operator starts each QSO
def mpost(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return json.load(e)
import urllib.error
fresh(); st["running"] = True; mpost("/api/config", {"manual": True})
dec("CQ K9XYZ EN52", snr=5); dec("CQ POTA K4PRK EM85", snr=-8); dec("JA1AAA VK2BBB RR73", snr=0)
dec(f"{st['mycall']} N5CAL EM15", snr=-12); ns["engine_tick"]()
check("manual: nothing is called on its own (not a caller, CQ, activator or tail-end)",
      st["target"] is None and not ENG["cq_until"] and REPLY not in CTX["tx"].sent and ENG["step"]["step"] == "manual")
sug = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/state"))["suggest"]
check("manual: suggestions in the hunter's order: caller, activator, CQ, tail-end",
      [(x["call"], x["why"]) for x in sug] == [("N5CAL", "called us"), ("K4PRK", "POTA"), ("K9XYZ", "CQ"), ("VK2BBB", "tail-end")])
r = mpost("/api/work", {"msg": "CQ POTA K4PRK EM85"})
check("manual: a tap starts the QSO", r.get("ok") and st["target"]["call"] == "K4PRK" and st["target"]["picked"]
      and REPLY in CTX["tx"].sent)
dec(f"{st['mycall']} W2NEW FN20", snr=-3)
check("manual: a new caller doesn't take over while we call the picked station", st["target"]["call"] == "K4PRK")
dec(f"{st['mycall']} K4PRK -10")
r = mpost("/api/work", {"msg": "CQ K9XYZ EN52"})
check("manual: no new pick in the middle of a QSO", "error" in r and st["target"]["call"] == "K4PRK")
CTX["tx"].sent.clear(); dec(f"{st['mycall']} K4PRK RR73"); ns["engine_tick"](); ns["engine_tick"]()
check("manual: the picked QSO is finished and released, nothing new started",
      st["target"] is None and ns["is_worked"]("K4PRK") and REPLY not in CTX["tx"].sent)
r = mpost("/api/work", {"msg": "CQ NOT1HEARD EN52"})
check("manual: a message not on offer is refused", "error" in r and st["target"] is None)
mpost("/api/config", {"manual": False}); ns["engine_tick"]()
check("manual off: the hunter picks again", st["target"] is not None)
fresh()
st["running"] = False

# --- radio via FLRig XML-RPC: meters polled, TX power set and clamped
from xmlrpc.server import SimpleXMLRPCServer
RADIO = {"power": 16, "ptt": 1, "swr": 40}
def cat(c):
    return "FE FE 70 E0 14 0A FD FE FE E0 70 14 0A %02X %02X FD" % divmod(ns["CIV"]["Xiegu-G90"][1][RADIO["power"]], 256)
fl = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=True)
fl.register_multicall_functions()
for name, f in {"rig.get_xcvr": lambda: "Xiegu-G90", "rig.get_maxpwr": lambda: 0,
                "rig.get_ptt": lambda: RADIO["ptt"], "rig.get_power": lambda: RADIO["power"],
                "rig.get_pwrmeter": lambda: "15", "rig.get_swrmeter": lambda: str(RADIO["swr"] if RADIO["ptt"] else 0), "rig.get_smeter": lambda: "0",
                "rig.set_power": lambda w: RADIO.update(power=w) or 0,
                "rig.cat_string": cat}.items():
    fl.register_function(f, name)
threading.Thread(target=fl.serve_forever, daemon=True).start()
st["config"]["flrig_url"] = f"http://127.0.0.1:{fl.server_address[1]}"
threading.Thread(target=ns["rig_poll"], daemon=True).start(); time.sleep(1.0)
R = ns["RIG"]
check("FLRig polled: rig, max power from the table, PTT, no S-meter",
      R["ok"] and R["xcvr"] == "Xiegu-G90" and R["max"] == 20 and R["ptt"] and not R.get("has_s"))
check("calling power (default 10 W) set on the radio", RADIO["power"] == 10 and R["want"] == 10)
def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))
st["udp"]["revision"] = "260926-hunter"; st["config"]["df_lock"] = False
post("/api/txdf", {"df": 1720})
check("waterfall move sets the offset and locks it",
      st["udp"]["tx_df"] == 1720 and st["config"]["df_lock"] and json.load(open(ns["CONFIG_FILE"]))["df_lock"])
post("/api/txdf", {"lock": False})
check("lock button unlocks without moving", not st["config"]["df_lock"] and st["udp"]["tx_df"] == 1720)
post("/api/rig/power", {"watts": 50})
check("TX power set through FLRig, clamped to the rig's max", RADIO["power"] == 20)
post("/api/rig/power", {"watts": 5})
check("TX power lowered", RADIO["power"] == 5)
check("slider sets and saves the calling power when not in a QSO",
      json.load(open(ns["CONFIG_FILE"]))["watts_call"] == 5 and st["config"]["watts_qso"] == 20)
RADIO["ptt"] = 0; time.sleep(2.5)
check("power setting read from the radio over CI-V (FLRig's get_power is 0 on a G90)", R["power"] == 5)
RADIO["power"] = 1                                  # something else turns it down
deadline = time.time() + 20
while RADIO["power"] != 5 and time.time() < deadline: time.sleep(0.5)
check("power changed behind our back is set back to the chosen power", RADIO["power"] == 5)
st["target"] = {"call": "K1PWR", "grid": "", "left": 9, "why": "CQ", "msg": "", "resp": False}
time.sleep(2.5)
check("still calling: calling power", RADIO["power"] == 5)
dec(f"{st['mycall']} K1PWR -07"); time.sleep(0.8)
check("they answered: QSO power at once", RADIO["power"] == 20)
st["target"] = None; time.sleep(2.5)
check("QSO over: back to calling power", RADIO["power"] == 5)
st["target"] = {"call": "K1PWR", "grid": "", "left": 9, "why": "called us", "msg": "", "resp": False}
post("/api/rig/power", {"watts": 15})
check("slider in a QSO sets the QSO power", st["config"]["watts_qso"] == 15 and st["config"]["watts_call"] == 5)
st["target"] = None
post("/api/config", {"watts_call": 99})
check("power settings clamped to the rig's max", st["config"]["watts_call"] == 20)
post("/api/config", {"watts_call": 5}); time.sleep(2.5)
check("G90 raw power values map to watts", ns["civ_watts"](0x17E, ns["CIV"]["Xiegu-G90"][1]) == 15
      and ns["civ_watts"](0x128, ns["CIV"]["Xiegu-G90"][1]) == 10 and ns["same_watts"](10, 11))
RADIO["ptt"] = 1; time.sleep(2.5)
check("output power shown while transmitting", R["out"] == 15)
SWR = ns["SWR"]; SWR["on"] -= 10  # on the air for a while
check("SWR meter shown live while transmitting", ns["swr_state"]()["live"] == 40)
RADIO["ptt"] = 0; time.sleep(2.5)
last = SWR["log"][-1] if SWR["log"] else {}
check("one SWR record per transmission: reading, band, power", last.get("raw") == 40 and last.get("watts") == 5
      and ns["swr_state"]()["live"] is None)
check("too few transmissions on a band and power to judge yet", ns["swr_state"]()["last"]["vs"]["level"] == "learning")
for k in range(4):
    SWR["log"].append({"id": k, "t": "", "secs": 13, "band": last["band"], "khz": 0, "mode": "FT8", "watts": 5, "raw": 40 + k})
SWR["log"].append({"id": 9, "t": "", "secs": 13, "band": last["band"], "khz": 0, "mode": "tune", "watts": 5, "raw": 100})
judge = ns["swr_judge"]
check("SWR reading against the usual for that band and power: normal", judge(41, last["band"], 5)["level"] == "normal")
check("...higher than usual", judge(52, last["band"], 5)["level"] == "higher")
check("...much higher (check the antenna); tunes don't count toward usual", judge(70, last["band"], 5)["level"] == "much higher"
      and judge(41, last["band"], 5)["n"] == 5)
check("other power settings and bands are judged on their own", judge(100, last["band"], 20)["level"] == "learning"
      and judge(41, "40m", 5)["level"] == "learning")
for k in range(3):
    SWR["log"].append({"id": 20 + k, "t": "", "secs": 13, "band": last["band"], "khz": 0, "mode": "FT8", "watts": 20, "raw": 100})
check("meter at the top of its scale at a power: not judged", judge(100, last["band"], 20)["level"] == "off scale")
n0 = len(ns["SWR"]["log"]); ns["SWR"]["on"] = None
ns["swr_poll"](True, -1, 1000.0); ns["swr_poll"](True, -1, 1002.0); ns["swr_poll"](False, 0, 1005.0)
check("FLRig's -1 (no reading) is not logged as an SWR reading", len(ns["SWR"]["log"]) == n0 and ns["RIG"]["swr_raw"] is None)
RADIO["ptt"] = 1; time.sleep(2.5)
post("/api/config", {"signoff": "gl73\ntu  73\n73\nTNX QSO GL 73\nTNX\ngl 73\ncul73"})
check("sign-off catalog from Settings: one per line, upper-cased, 73 split off, duplicates dropped; "
      "too long after our call or no 73 refused",
      st["config"]["signoff"] == ["GL 73", "TU 73", "73", "CUL 73"])
post("/api/config", {"df_lo": 1400})
saved = json.load(open(ns["CONFIG_FILE"]))
st["config"]["df_lo"] = 1300; ns["load_config"]()
check("lowest offset saved from Settings and loaded again after a restart",
      saved["df_lo"] == 1400 and st["config"]["df_lo"] == 1400)
post("/api/config", {"df_lo": 50}); lo1 = st["config"]["df_lo"]
post("/api/config", {"df_lo": 2690}); lo2 = st["config"]["df_lo"]
check("lowest offset kept between 200 Hz and 300 Hz under the top", lo1 == 200 and lo2 == 2400)

# --- band scan
SCAN = ns["SCAN"]
src = """      {14074000, Modes::FT8, IARURegions::ALL, "","", QDateTime(), QDateTime(), true},
      {14090000, Modes::FT8, IARURegions::ALL, "","", QDateTime(), QDateTime(), false}, // DXpedition
      {14084000, Modes::FT2, IARURegions::ALL, "","", QDateTime(), QDateTime(), true}, // provisional
      {3568000, Modes::FT4, IARURegions::R3, "","", QDateTime(), QDateTime(), false},
      {3575000, Modes::FT4, IARURegions::ALL, "","", QDateTime(), QDateTime(), true},
      {3578000, Modes::FT2, IARURegions::R3, "","", QDateTime(), QDateTime(), false},
      {5357000, Modes::FT8, IARURegions::ALL, "","", QDateTime(), QDateTime(), true},
      {5357000, Modes::FT2, IARURegions::ALL, "","", QDateTime(), QDateTime(), true},
      {50313000, Modes::FT8, IARURegions::ALL, "","", QDateTime(), QDateTime(), true},"""
check("WS frequency list: preferred entries, another region's only when alone, HF only",
      ns["parse_freq_list"](src, "R2") == [(3575.0, "FT4"), (3578.0, "FT2"), (5357.0, "FT2"), (5357.0, "FT8"),
                                           (14074.0, "FT8"), (14084.0, "FT2")])
SCAN["freqs"]["list"] = ns["FREQS_BUILTIN"]
pts = ns["scan_points"]({"60m", "20m"}, {"FT8", "FT4", "FT2"})
check("one stop per dial frequency (60m serves all three modes)",
      [(p["hz"], len(p["modes"])) for p in pts] == [(5357000, 3), (14074000, 1), (14080000, 1), (14084000, 1)])
check("Configure can switch WS's mode", b"FT2" in ns["pack_configure"]("WS", "", "", False, mode="FT2"))

random.seed(3)
shape = [(-8 if i < 25 else 0) - (max(0, i - 180) * 0.06) for i in range(300)]  # receiver passband
flat = lambda: [10 + shape[i] + random.gauss(0, 0.4) for i in range(300)]
met = lambda db, bw_=50: ns["scan_metrics"](db, 0, 10, bw_)
ft8 = flat()
for c in (80, 95, 120, 150, 170, 200, 230):
    for i in range(c, c + 5): ft8[i] += 6
lump = flat()
for i in range(60, 140): lump[i] += 8
cw = flat(); cw[150] += 15
f2 = flat()
for i in range(100, 117): f2[i] += 5
dense = flat()
for c in range(10, 270, 9):  # 20m FT8 on a busy evening: traffic everywhere
    for i in range(c, c + 6): dense[i] += 10
rows = [met(flat()), met(ft8), met(lump), met([x + 12 for x in flat()]), met(cw), met(f2, 167), met(dense)]
ns["scan_rate"](rows, rows)
check("scan: seven 50 Hz signals counted, the passband slope is not one",
      rows[1]["signals"] == 7 and rows[0]["signals"] == 0 and rows[1]["label"] == "active")
check("scan: a broad lump is wideband QRM, not signals (nor its edges)",
      rows[2]["signals"] == 0 and rows[2]["label"] == "QRM" and rows[0]["label"] == "quiet")
check("scan: a band 10+ dB noisier than the rest is noisy", rows[3]["label"] == "noisy" and rows[3]["noise"] >= 10)
check("scan: dense FT traffic is busy, not QRM", rows[6]["label"] == "busy" and not rows[6]["qrm"])
check("scan: a carrier is a carrier, an FT2 signal one signal",
      rows[4]["carriers"] and not rows[4]["signals"] and rows[5]["signals"] == 1)

# a whole scan: the spectrum of the period the rig retuned in is thrown away
fresh(); st["running"] = True; SCAN["rows"].clear()
st["udp"].update(mode="FT8", dial=14074000, revision="261013-hunter")
seen = []
def fake_mode(m, wait=8.0):
    seen.append(m); st["udp"]["mode"] = m; return True
def fake_dial(hz):
    st["udp"]["dial"] = hz; seen.append(hz)
    q, now_ms = SCAN["q"], ns["tod_ms"](time.time())
    if q:
        q.put((now_ms // 3750 * 3750, 3750, 0, 10, ft8))            # retuned part way: mixed bands
        for k in (1, 2):  # the next period may start before the retune has settled: one more
            q.put(((now_ms // 3750 + k) * 3750, 3750, 0, 10, ft8 if hz == 5357000 else flat()))
real = ns["ws_set_mode"], ns["ws_set_dial"]
ns["ws_set_mode"], ns["ws_set_dial"] = fake_mode, fake_dial
SCAN["q"] = None
ns["scan_run"](pts[:2])
ns["ws_set_mode"], ns["ws_set_dial"] = real
r60, r20 = SCAN["rows"]["5357000 FT4"], SCAN["rows"]["14074000 FT8"]
check("scan visits each stop in FT2, then puts WS back where it was",
      seen == ["FT2", 5357000, 14074000, "FT8", 14074000] and st["udp"]["mode"] == "FT8")
check("scan measures the clean period after each retune", r60["signals"] == 7 and r20["signals"] == 0)
check("hunter paused for the scan and resumed after", st["running"] and not SCAN["on"])
st["worked"] = {}
sc = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/scan"))
check("scan results served per mode, without the internal shape",
      len(sc["rows"]) == 4 and all("_shape" not in r for r in sc["rows"]) and sc["rows"][0]["mode"] == "FT2")
st["target"] = {"call": "K1QSO", "grid": "", "left": 9, "why": "called us", "msg": "", "resp": True}
try:
    post("/api/scan/start", {}); refused = False
except urllib.error.HTTPError as e:
    refused = e.code == 409
check("no scan in the middle of a QSO", refused and not SCAN["on"])
try:
    post("/api/scan/go", {"hz": 18100000, "mode": "FT8"}); refused = False
except urllib.error.HTTPError as e:
    refused = e.code == 409
check("no band change from the scan in the middle of a QSO", refused)
st["target"] = None
st["udp"]["revision"] = ""

# --- antenna tune on a new band: under FT8, then back; never where it can't confirm
class FakeRig:
    def __init__(self, follow=True): self.vfo, self.tuned_at, self.follow = 0, [], follow
    def get_vfo(self): return str(self.vfo)
    def tune(self): self.tuned_at.append(self.vfo)
    def cat_string(self, c): return "FE FE 70 E0 1C 01 FD FE FE E0 70 1C 01 01 FD"
rig = FakeRig()
fake_fl = type("FL", (), {"rig": rig})()
real_fl, real_dial = ns["flrig"], ns["ws_set_dial"]
ns["flrig"] = lambda: fake_fl
def dial_to(hz):
    st["udp"]["dial"] = hz
    if rig.follow: rig.vfo = hz
ns["ws_set_dial"] = dial_to
SCAN["freqs"]["list"] = ns["FREQS_BUILTIN"]
ns["RIG"]["xcvr"] = "Xiegu-G90"
fresh(); st["udp"].update(revision="261013-hunter", band="20m", dial=14080000, mode="FT4"); rig.vfo = 14080000
ENG["tuned_band"] = "40m"; st["config"]["tune_new_band"] = True
started = []
real_start = ns["start_tune"]; ns["start_tune"] = lambda: started.append(1)
ENG["arrived"] = time.time() - 5; st["running"] = True; ns["engine_tick"]()
check("new band: listen first, no tune needed for that", not started and ENG["step"]["step"] == "newband")
ENG["arrived"] = time.time() - 80; ns["engine_tick"]()
check("...then tune before the first transmission", started and ENG["step"]["step"] == "tuning")
ENG["arrived"] = None
ENG["tuned_band"] = "20m"; started.clear(); ns["engine_tick"]()
check("same band (a mode change): no tune", not started)
ns["start_tune"] = real_start
ENG["tuned_band"] = "40m"; ENG["tuning"] = "starting"; ns["atu_tune"]()
check("tune carrier 50 kHz from FT8 (General on 20m: above, 14.024 is Extra only), then back",
      rig.tuned_at == [14124000] and st["udp"]["dial"] == 14080000 and rig.vfo == 14080000)
tf = ns["tune_freq"]
st["config"]["license"] = "extra"
check("Extra: 50 kHz below FT8 on 20m and 40m", tf("20m")[0] == 14024000 and tf("40m")[0] == 7024000)
st["config"]["license"] = "general"
check("General on 10m: 50 kHz below", tf("10m")[0] == 28024000)
check("General on 40m: neither fits, nearest allowed spot inside the segment",
      tf("40m")[0] == 7122000 and "as far as General" in tf("40m")[1])
check("30m (50 kHz wide): stays inside the band", 10100000 < tf("30m")[0] < 10150000)
check("17m: above, below would be out of band", tf("17m")[0] == 18150000)
check("60m: no tune (channels)", tf("60m")[0] is None)
st["config"]["tune_away_khz"] = 10
check("never closer than 50 kHz", tf("10m")[0] == 28024000)
st["config"]["tune_away_khz"] = 50
check("...and the band counts as tuned", ENG["tuned_band"] == "20m" and not ENG["tuning"])
rig.follow, rig.tuned_at = False, []; ENG["tuned_band"] = "40m"; ENG["tuning"] = "starting"; ns["atu_tune"]()
check("rig not on the tune frequency: no tune carrier", rig.tuned_at == [] and not ENG["tuning"])
per = ns["period"](); tr = int(per * 1000); p0 = time.time() // per * per; t = ns["tod_ms"](p0)
ENG["tune_iv"].clear(); ENG["tune_iv"].append([p0 + 1.5, p0 + per - 1.5])
check("the period spent tuning is left out of the offset picker", t not in ns["heard_periods"]((t // tr) % 2, time.time() + 2 * tr / 1000))
check("...and a tune doesn't start the listen over", not ENG.get("arrived"))
ENG["tune_iv"].clear()
ns["flrig"], ns["ws_set_dial"] = real_fl, real_dial
# a new band: two full cycles (two even, two odd periods) heard before any transmission
fresh(); st["running"] = True; ENG["tuned_band"] = "20m"
st["udp"].update(band="17m", mode="FT8"); ns["set_band"]("20m")
dec("CQ K4NEW EM85", snr=-3, t_ms=recent_tm(0)); ns["engine_tick"]()
check("new band: listen, don't answer a CQ yet", ENG["step"]["step"] == "newband" and not st["target"])
w = ns["new_band_wait"](time.time())
check("...for two full cycles (four FT8 periods, the first whole one after arriving)", 60 <= w <= 75)
ENG["arrived"] = time.time() - 80; ns["engine_tick"]()
check("...then it hunts as usual", st["target"] and st["target"]["call"] == "K4NEW")
st["udp"]["mode"] = "FT4"; ENG["arrived"] = None; ns["set_mode"]("FT8")
check("a mode change on the same band doesn't start a listen", not ENG.get("arrived"))
ENG["arrived"] = None
ENG["tuned_band"] = None; st["udp"]["revision"] = ""; st["running"] = False
# WRL Desktop warning: is anything bound to the bridge port?
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); probe.bind(("127.0.0.1", 0))
st["config"]["wrl_port"] = probe.getsockname()[1]; ns["WRL"].update(on=None, t=0); ns["wrl_check"](time.time())
check("WRL listening on its port: no warning", ns["WRL"]["on"] is True)
probe.close(); ns["WRL"]["t"] = 0; ns["wrl_check"](time.time())
check("nothing on the port: WRL is off, and the log says so", ns["WRL"]["on"] is False and "isn't running" in st["log"][0]["msg"])
# each logged QSO records how it started and the hunter's mode
O = ns["ORIGIN"]; O["qsos"].clear(); O["secs"].clear(); O["recent"].clear(); st["config"]["cq_first"] = True
ENG["cq_until"] = time.time() + 60; ns["note_origin"]("K1CQA", "called us"); ENG["cq_until"] = 0
ns["record_origin"]("K1CQA", "20m", "FT8")
check("a caller during our CQ run: answered our CQ, in CQ-first mode", O["qsos"][-1]["how"] == "cq" and O["qsos"][-1]["mode"] == "cq_first")
st["config"]["cq_first"] = False; ns["note_origin"]("W2HNT", "CQ"); ns["record_origin"]("W2HNT", "20m", "FT8")
ns["note_origin"]("N3TAL", "called us"); ns["record_origin"]("N3TAL", "20m", "FT8")
ns["record_origin"]("K4HND", "20m", "FT8")
check("we answered a CQ / they called us / worked by hand in WS",
      [x["how"] for x in O["qsos"][1:]] == ["hunt", "called us", "by hand"] and O["qsos"][1]["mode"] == "hunt")
day = time.strftime("%Y-%m-%d", time.gmtime())
st["running"] = True; O["last"] = time.time() - 30; ns["tick_origins"](time.time()); st["running"] = False
check("running time is counted for the current mode", 29 <= sum(O["secs"][day].values()) <= 31)
O["secs"][day] = {"hunt": 1800}
r = ns["origin_stats"]()
check("QSOs per hour by mode, from the time spent in it", r["rate"].get("hunt") == 4.0 and r["how"]["cq"] == 1)
# call sign and grid: Settings first, else what WS reports; no call, no start
saved = dict(st["config"]), st["mycall"], st["mygrid"]
st["config"].update(mycall="", mygrid=""); st["udp"].update(de_call="", de_grid=""); ns["set_identity"]()
try:
    post("/api/start", {}); code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("no call sign anywhere: the hunter won't start", code == 409 and not st["running"] and st["mycall"] == "")
st["udp"].update(de_call="w1aw", de_grid="FN31pr"); ns["set_identity"]()
check("empty Settings: WS's DE call and grid are used", st["mycall"] == "W1AW" and st["mygrid"] == "FN31pr")
post("/api/config", {"mycall": "kr4mzm/ag", "mygrid": "em84"})
check("Settings' call and grid win over WS's", st["mycall"] == "KR4MZM/AG" and st["mygrid"] == "EM84")
post("/api/config", {"mycall": "NOT A CALL", "mygrid": "ZZ99"})
check("a bad call or grid isn't saved (falls back to WS's)", st["config"]["mycall"] == "" and st["mygrid"] == "FN31pr")
post("/api/config", {"udp_port": 80, "swr_high": 2.0, "swr_bad": 1.1})
check("ports and SWR levels kept sane", st["config"]["udp_port"] == 1024 and st["config"]["swr_bad"] >= 2.0)
st["config"].clear(); st["config"].update(saved[0]); st["mycall"], st["mygrid"] = saved[1], saved[2]; st["running"] = False
fl.shutdown(); srv.shutdown()

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
