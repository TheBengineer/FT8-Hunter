#!/usr/bin/env python3
"""FT8 hunter daemon: UDP watcher + stdlib JSON API + serves webapp. No deps.
Run: python3 ft8_server.py   (call sign, grid, ports and the rest: Settings in the UI;
     --mycall/--mygrid/--port/--http/--flrig, when given, are saved into those settings)
API: GET /api/state -> {running, target, cq[], answering[], worked[], log[], config, stats}
     POST /api/start|stop|call|work|skip|config   Webapp: GET /
     GET|POST /api/logbooks (+ /test, /retry): online logbook uploads
"""
import argparse, json, socket, struct, sys, threading, time, re, os, math, random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
from urllib.parse import urlparse

VERSION = "1.1.4"  # a release's tag is v + this (the build refuses a tag that doesn't match)

# The page ships beside this script (or inside a packaged build); the hunter's
# own files (settings, logs) live beside the script too, or for a packaged
# build in the user's app-data folder, so they outlast each run.
FROZEN = bool(getattr(sys, "frozen", False))
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
def _data_dir():
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "FT8-Hunter")
    os.makedirs(d, exist_ok=True)
    return d
DATA_DIR = _data_dir()
if sys.stdout is None or sys.stderr is None:  # a windowed build has no console: log to a file
    sys.stdout = sys.stderr = open(os.path.join(DATA_DIR, "hunter.log"), "a", buffering=1, encoding="utf-8")

SIGNOFF_CATALOG = [  # each fits after our call: 6 characters at most
    "GL 73", "TU 73", "HH 73", "FB 73", "VY 73", "OM 73", "CU 73", "TY 73", "SK 73",
    "GM 73", "GA 73", "GE 73", "GN 73", "GD 73", "TNX 73", "TKS 73", "CUL 73",
    "73 GL", "73 TU", "73 OM", "73 FB", "73 CUL", "73 TNX",
]
state = {
    "running": False, "mycall": "", "mygrid": "",
    "target": None, "cq": {}, "answering": {},
    "worked": {}, "log": deque(maxlen=50),
    "msgs": deque(maxlen=80), "mytx": deque(maxlen=20),
    "config": {"mycall": "", "mygrid": "",  # yours, no default: empty = what WS reports (its Settings -> General)
               "udp_port": 2237, "http_port": 8080,  # WS UDP server / web UI (take effect on restart)
               "flrig_url": "http://127.0.0.1:12345",  # FLRig XML-RPC (power + meters)
               "wrl_port": 2238,  # WS packets rebroadcast here for WRL Desktop (restart)
               "ws_dir": "",
               "wrl_warn": True,
               "psk_remote": True,  # look at the DX's end of the band (PSK Reporter) when picking a Tx offset
               "desync_stop": True,  # stop the hunter (and WS's TX) when the radio and WS are out of step
               "silent_min": 5,  # ...or nothing decoded for this long while hunting (0 = don't check)  # warn when nothing (WRL Desktop) is listening on wrl_port  # WS / WSJT-X data folder (ALL.TXT, wsjtx_log.adi); empty = look in the usual places
               "traffic_secs": 300,  # globe: other stations' traffic lines fade out over this long
               "tune_wait": 20,  # s the antenna tuner may take
               "new_band_cycles": 2,  # full cycles heard on a new band before transmitting
               "swr_high": 1.2, "swr_bad": 1.5,  # SWR reading this many times the usual: higher / much higher
               "max_cycles": 3, "cooldown": 2, "cq_only": True,
               "cq_tries": 3, "tail_tries": 2, "qso_tries": 7, "hold_min": 10,
               "idle_every": 4, "idle_secs": 60, "special_first": True,
               "call_cq": True, "cq_secs": 120, "cq_first": False,
               "manual": False,
               "compat": False,  # plain WSJT-X/WS UDP only (no ws-hunter commands): a light asks you to Enable Tx  # contest mode: the hunter ranks, the operator taps to start each QSO
               "auto_df": True, "df_lock": False, "df_lo": 1300, "df_hi": 2700,  # df_lo: rig feeds back below 1300 Hz
               "qrm_detect": True, "qrm_backoff_min": 5, "min_snr": -20,
               "check_every": 10,  # transmissions between one-cycle listens for interference (0 = never)
               "watts_call": 10, "watts_qso": 20,
               # our final 73, one per QSO at random ("73" = standard). FT8 free text
               # is 13 characters and our call goes first, so 6 are left for the text
               "signoff": list(SIGNOFF_CATALOG),
               "tune_new_band": True, "tune_away_khz": 50,  # ATU tune at least this far from FT8 (below if allowed)
               "license": "",  # US class: where a tune carrier may go (see PRIV); unset = General's limits
               "scan_modes": ["FT8", "FT4", "FT2"],  # band scan: what to visit
               "scan_bands": ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m"],  # TX power held on the radio: calling / once they answer
               "update_check": True,  # look for a new release twice a day (installs only when asked)
               "update_skip": ""},  # a version the operator said no to: not offered again
    "stats": {"cq_heard": 0, "calls_made": 0},
}
# "CQ [TAG] CALL [GRID]"; TAG is DX, TEST, an activity (POTA...) or a
# directed region (NA, EU...). Calls always contain a digit; tags never do.
CQ_RE = re.compile(r"^CQ\s+(?:([A-Z]{1,4})\s+)?([A-Z0-9/]*\d[A-Z0-9/]*)(?:\s+([A-Z]{2}\d{2}(?:\d{2})?))?", re.I)
# portable activations: worth pouncing on first (parks, summits, flora &
# fauna, islands, beaches, lighthouses)
SPECIAL = {"POTA", "SOTA", "WWFF", "IOTA", "BOTA", "LOTA"}
ANSWERABLE = {"", "DX", "TEST"} | SPECIAL  # other tags are directed at a region
SIGNOFF = {"RR73", "73", "RRR"}
udp_port = 2237

def log(msg):
    state["log"].appendleft({"t": time.strftime("%H:%M:%S"), "msg": msg})

_logged_errs = set()
def log_err(where, e):
    """Log each distinct parse error once instead of swallowing it."""
    key = f"{where}: {type(e).__name__}: {e}"
    if key not in _logged_errs:
        _logged_errs.add(key); log(key)

def base_call(tok):
    """'<KR4MZM/AG>' -> 'KR4MZM', 'VE3/K1ABC' -> 'K1ABC': strip hash brackets
    and any portable prefix/suffix, keeping the longest part with a digit."""
    t = tok.strip("<>").upper()
    parts = [p for p in t.split("/") if p] or [t]
    return max(parts, key=lambda p: (any(c.isdigit() for c in p), len(p)))

def same_call(a, b):
    return bool(a) and bool(b) and base_call(a) == base_call(b)

def is_me(tok):
    return same_call(tok, state["mycall"])

def addressing(msg):
    """Standard FT8 message 'TO FROM ...' -> (to, from, rest); the station
    being called comes FIRST. Hash brackets are stripped."""
    parts = msg.upper().split()
    if len(parts) < 2 or parts[0] in ("CQ", "QRZ", "DE"):
        return "", "", parts
    return parts[0].strip("<>"), parts[1].strip("<>"), parts[2:]

def pack_str(s):
    b = s.encode(); return struct.pack(">i", len(b)) + b

def pack_reply(id_, t_ms, snr, dt, df, msg, mode="~"):
    """Type 4 Reply. Per spec WS acts ONLY if every field exactly matches a
    prior decode, so all params must come from the real decode.
    Wire order: magic, schema, TYPE, id, ... (type comes first!)."""
    pkt = struct.pack(">II", 0xADBCCBDA, 2) + struct.pack(">I", 4) + pack_str(id_)
    pkt += struct.pack(">I", t_ms) + struct.pack(">i", snr)
    pkt += struct.pack(">d", dt) + struct.pack(">I", df)
    pkt += pack_str(mode)  # decoded-line marker as sent by WS (NOT "FT8")
    pkt += pack_str(msg) + struct.pack(">B", 0) + struct.pack(">B", 0)
    return pkt

def pack_configure(id_, dx_call=" ", dx_grid=" ", generate=True, mode=""):
    """Type 15 Configure: blank dx_call + generate=True resets WS message
    stack to its default CQ (empty string is ignored upstream, so send a space).
    A mode ("FT2") switches WS to it. Other fields = no-change sentinels."""
    M = 0xFFFFFFFF
    pkt = struct.pack(">II", 0xADBCCBDA, 2) + struct.pack(">I", 15) + pack_str(id_)
    pkt += pack_str(mode) + struct.pack(">I", M) + pack_str("")
    pkt += struct.pack(">B", 0) + struct.pack(">I", M) + struct.pack(">I", M)
    pkt += pack_str(dx_call) + pack_str(dx_grid) + struct.pack(">B", 1 if generate else 0)
    return pkt

def pack_halt(id_):
    return struct.pack(">II", 0xADBCCBDA, 2) + struct.pack(">I", 8) + pack_str(id_)

def pack_replay(id_):
    return struct.pack(">II", 0xADBCCBDA, 2) + struct.pack(">I", 7) + pack_str(id_)

# ws-hunter build only (see TheBengineer/ws-hunter, Network/NetworkMessage.hpp)
SET_TX_PERIOD, SET_TX_DF, SET_HOLD_TX_FREQ, SET_TX_ENABLED, SPECTRUM, CLEAR_DX = 19, 24, 25, 26, 27, 28
def pack_set_bool(mtype, id_, value):
    """ws-hunter station-control message carrying one bool."""
    return struct.pack(">III", 0xADBCCBDA, 2, mtype) + pack_str(id_) + struct.pack(">B", 1 if value else 0)

def pack_set_u32(mtype, id_, value):
    return struct.pack(">III", 0xADBCCBDA, 2, mtype) + pack_str(id_) + struct.pack(">I", value)

def has_ws_hunter(since="260926"):
    """True when WS reports a ws-hunter build in its Heartbeat revision, from
    the `since` build (yymmdd) on. ClearDX arrived in 261013."""
    if state["config"].get("compat"):
        return False  # compatibility mode: behave as with any WSJT-X
    m = re.match(r"(\d{6})-hunter", state["udp"].get("revision", ""))
    return bool(m) and m.group(1) >= since

def ws_caps():
    """What WS / WSJT-X can be told over UDP: 'hunter' (the ws-hunter build:
    everything), 'configure' (WSJT-X 2.0 on: Reply, Halt Tx, Free Text and
    Configure for the mode and the CQ message) or 'basic' (older: Reply and
    Halt Tx). Nothing but ws-hunter can turn Enable Tx on: the TX light asks."""
    if has_ws_hunter():
        return "hunter"
    m = re.match(r"\s*(\d+)\.(\d+)", state["udp"].get("version", ""))
    return "basic" if m and (int(m.group(1)), int(m.group(2))) < (2, 0) else "configure"

FREE_TEXT = 9  # standard WSJT-X UDP: set the Tx5 message text
def pack_free_text(id_, text, send=False):
    return struct.pack(">III", 0xADBCCBDA, 2, FREE_TEXT) + pack_str(id_) + pack_str(text) + struct.pack(">B", 1 if send else 0)

FREE_TEXT_RE = re.compile(r"[A-Z0-9 +\-./?]{1,13}")  # what an FT8 free-text message can carry
def signoff_text():
    """Our final transmission for this QSO: our call and a text from the
    sign-off catalog picked at random, e.g. 'KR4MZM GL 73'. Our call is always
    in it (station ID at the end of the contact); FT8 free text holds 13
    characters, so theirs can't fit and the text gets 6. None means WS's
    standard 'THEM US 73'."""
    fits = [sign_msg(x) for x in state["config"].get("signoff") or []]
    fits = [m for m in fits if m is not False]
    return random.choice(fits) if fits else None

def sign_msg(text):
    """'GL73' -> 'KR4MZM GL 73'; None for the standard 73; False if unusable."""
    f = signoff_fix(text)
    if not f:
        return False
    if f == "73":
        return None
    msg = f"{base_call(state['mycall'])} {f}"
    return msg if FREE_TEXT_RE.fullmatch(msg) else False

def signoff_fix(text):
    """WS logs the QSO when it sends a 73, but only if '73' is a word of its
    own: 'GL73' -> 'GL 73'. None if there is no 73 to split off."""
    text = " ".join(str(text).upper().split())
    text = re.sub(r"(?<=[A-Z])(R?R?73)\b", r" \1", text)
    return text if re.search(r"\b(RR)?73\b", text) else None

def push_signoff(t):
    """Put the target's sign-off in WS's Tx5. WS rewrites Tx5 only when the DX
    call changes, so this holds for the QSO; it is re-sent on each of their
    replies in case anything regenerated it."""
    if t and t.get("signoff") and state.get("ws_addr"):
        CTX["tx"].sendto(pack_free_text(state["ws_id"], t["signoff"]), ws_dest())

def pack_id_only(mtype, id_):
    return struct.pack(">III", 0xADBCCBDA, 2, mtype) + pack_str(id_)

def select_cq():
    """Point WS at its CQ message. ClearDX (ws-hunter 261013+) empties DX Call
    and selects Tx6; older builds only get Configure, which regenerates the
    messages but leaves the last DX call (and its Tx1) in place."""
    if has_ws_hunter("261013"):
        CTX["tx"].sendto(pack_id_only(CLEAR_DX, state["ws_id"]), ws_dest())
    else:
        CTX["tx"].sendto(pack_configure(state["ws_id"]), ws_dest())

def enable_tx():
    """Enable Tx: a UDP command on the ws-hunter build. Any other WS/WSJT-X
    has no such command: the TX light (tx_light) asks the operator."""
    if has_ws_hunter():
        CTX["tx"].sendto(pack_set_bool(SET_TX_ENABLED, state["ws_id"], True), ws_dest())
        return "Enable Tx via UDP"
    return "TX light on: click Enable Tx in WSJT-X"

def manual_on():
    """Contest (manual) mode: nothing new starts without the operator's
    press. Always on without ws-hunter: there the hunter can't enable TX or
    pick a clear offset, so the operator drives and the hunter suggests."""
    return bool(state["config"].get("manual")) or not has_ws_hunter()

TX_LIGHT = {"want_t": 0, "mode_t": 0}
def tx_light(now=None):
    """Without ws-hunter (compatibility mode, or a stock WSJT-X) the hunter
    can choose whom to call (Reply) and stop TX (Halt Tx) but not turn Enable
    Tx on. This light says what the operator should do in WSJT-X:
    'enable' (it wants to transmit and Enable Tx is off), 'reenable' (the TX
    watchdog stopped it), 'mode' (not on FT8/FT4/FT2), 'ok' or 'idle'.
    With ws-hunter it is None: the hunter does it all itself."""
    if has_ws_hunter():
        return None
    now = now or time.time()
    u, t = state["udp"], state["target"]
    if u.get("mode") and u["mode"] not in MODES:
        # best effort: WSJT-X 2.0 on switches mode on a Configure (once a minute)
        if state["running"] and ws_caps() == "configure" and now - TX_LIGHT["mode_t"] > 60:
            TX_LIGHT["mode_t"] = now
            CTX["tx"].sendto(pack_configure(state["ws_id"], "", "", False, mode="FT8"), ws_dest())
            log(f"WSJT-X is on {u['mode']}: asked it to switch to FT8")
        return {"state": "mode", "msg": f"Set WSJT-X to FT8 (it is on {u['mode']})"}
    want = state["running"] and not ENG.get("tuning") and (
        bool(t and not t.get("done")) or now < ENG["cq_until"])
    if not want:
        TX_LIGHT["want_t"] = 0
        return {"state": "idle", "msg": "Nothing to send: Enable Tx can stay as it is"}
    TX_LIGHT["want_t"] = TX_LIGHT["want_t"] or now
    what = f"calling {t['call']}" if t and not t.get("resp") else f"in QSO with {t['call']}" if t else "calling CQ"
    if u.get("watchdog"):
        return {"state": "reenable", "msg": f"TX watchdog stopped WSJT-X: click Enable Tx again ({what})"}
    if u.get("tx_enabled"):
        return {"state": "ok", "msg": f"TX enabled ({what})"}
    if now - TX_LIGHT["want_t"] < 2:  # WSJT-X may enable it itself on a Reply (double-click sets Tx enable)
        return {"state": "ok", "msg": what}
    return {"state": "enable", "msg": f"Click Enable Tx in WSJT-X ({what})"}

def alltxt_time_ms(tstr):
    # "261009_221230" -> ms since midnight (decode times fall on whole seconds)
    hh, mm, ss = int(tstr[7:9]), int(tstr[9:11]), int(tstr[11:13])
    return ((hh * 60 + mm) * 60 + ss) * 1000

def udp_holder(port=2237):
    """Who holds local UDP port? Returns process name or 'free' ('?' without
    Linux's /proc, e.g. on macOS)."""
    if not os.path.isdir("/proc/net"):
        return "?"
    want = f"{port:04X}"
    inodes = set()
    for f in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            for ln in open(f).readlines()[1:]:
                c = ln.split()
                if c[1].split(":")[1] == want:
                    inodes.add(c[9])
        except FileNotFoundError:
            pass
    if not inodes:
        return "free"
    holders = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{pid}/exe").split("/")[-1]
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    if os.readlink(f"/proc/{pid}/fd/{fd}")[8:-1] in inodes:
                        holders.add(exe); break
                except OSError:
                    pass
        except OSError:
            pass
    return ",".join(sorted(holders)) if holders else "unknown"

def grid_to_latlon(g):
    g = g.upper()
    if len(g) < 4: return None
    lon = (ord(g[0]) - 65) * 20 - 180 + (ord(g[2]) - 48) * 2 + 1
    lat = (ord(g[1]) - 65) * 10 - 90 + (ord(g[3]) - 48) + 0.5
    if len(g) >= 6:
        lon += (ord(g[4]) - 65) * 5 / 60 - 2.5 / 60
        lat += (ord(g[5]) - 65) * 2.5 / 60 - 1.25 / 60
    return (lat, lon)

def km(a, b):
    if not a or not b: return None
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return round(2 * 6371 * math.asin(math.sqrt(h)))

CTY = {"exact": {}, "prefix": {}}  # call/prefix -> (country, continent, lat, lon)
GRIDS = {}  # base call -> grid, learned from decodes and the log (for the globe)
PAIRS = {}  # frozenset of two base calls -> {"a", "b", "t"}: who is working whom (globe)
RECENT = {}  # (call, band, mode) -> epoch time, last 24 h: the globe keeps them past UTC midnight
GRID_RE = re.compile(r"^[A-R]{2}\d{2}([A-X]{2})?$")
MYCALL_RE = re.compile(r"([A-Z0-9]{1,4}/)?[A-Z0-9]*\d[A-Z0-9]*[A-Z](/[A-Z0-9]{1,4})?")  # VE3/K1ABC, KR4MZM/AG
CTY_PATHS = ("~/ws-hunter-app/share/ws/cty.dat", "~/ws/usr/share/ws/cty.dat",
             "~/ws-wide/usr/share/ws/cty.dat", "/usr/share/wsjtx/cty.dat",
             # macOS: inside the app bundle
             "~/ws-hunter-app/ws.app/Contents/Resources/ws/cty.dat",
             "/Applications/ws.app/Contents/Resources/ws/cty.dat",
             "/Applications/wsjtx.app/Contents/Resources/wsjtx/cty.dat",
             # Windows: the ws-hunter zip unpacked beside us, or the usual installs
             os.path.join(RES_DIR, "..", "ws-hunter", "share", "ws", "cty.dat"),
             "C:/WSJT/ws/share/ws/cty.dat", "C:/WSJT/wsjtx/share/wsjtx/cty.dat",
             # last: the copy a packaged hunter carries
             os.path.join(RES_DIR, "cty.dat"))
# Where WS / WSJT-X keep ALL.TXT and wsjtx_log.adi, WS first: Linux, macOS, Windows
WS_DIRS = ("~/.local/share/WS", "~/.local/share/WSJT-X",
           "~/Library/Application Support/WS", "~/Library/Application Support/WSJT-X",
           "~/AppData/Local/WS", "~/AppData/Local/WSJT-X")
def ws_files(name, extra=()):
    """That file in each WS / WSJT-X data folder, expanded (existing or not)."""
    own = (state["config"]["ws_dir"],) if state["config"]["ws_dir"] else ()
    return [os.path.expanduser(os.path.join(d, name)) for d in own + WS_DIRS + tuple(extra)]

def load_cty():
    """Parse the country file WS ships (AD1C cty.dat format)."""
    for p in map(os.path.expanduser, CTY_PATHS):
        if not os.path.exists(p):
            continue
        for chunk in open(p, errors="ignore").read().split(";"):
            head, _, body = chunk.strip().partition("\n")
            f = [x.strip() for x in head.split(":")]
            if len(f) < 8:
                continue
            for tok in body.replace("\n", "").split(","):
                m = re.match(r"\s*(=?)([A-Z0-9/]+)", tok)
                if not m:
                    continue
                c = re.search(r"\{(\w\w)\}", tok)  # per-prefix continent override
                # cty.dat longitudes are positive west
                (CTY["exact"] if m.group(1) else CTY["prefix"])[m.group(2)] = (
                    f[0], c.group(1) if c else f[3], float(f[4]), -float(f[5]))
        log(f"country file: {p} ({len(CTY['prefix'])} prefixes)")
        return

def dxcc(call):
    """(country, continent) for a call, or None. 'VE3/K1ABC' is looked up as VE3."""
    c = call.strip("<>").upper()
    if c in CTY["exact"]:
        return CTY["exact"][c]
    parts, base = c.split("/"), base_call(c)
    key = parts[0] if base in parts and parts.index(base) > 0 else base
    for n in range(len(key), 0, -1):
        if key[:n] in CTY["prefix"]:
            return CTY["prefix"][key[:n]]
    return None

def note_grid(call, grid):
    if call and grid and grid.upper() != "RR73" and GRID_RE.match(grid.upper()):
        GRIDS[base_call(call)] = grid.upper()

def locate(call):
    """(lat, lon) for a call: its grid if we've seen one, else its country's centre."""
    g = GRIDS.get(base_call(call))
    if g:
        return grid_to_latlon(g)
    d = dxcc(call)
    return (d[2], d[3]) if d and len(d) > 3 else None

def dx_ok(e):
    """CQ DX is for stations on another continent; answering from the same
    one is a classic faux pas. Unknown calls fall back to distance."""
    if e.get("tag") != "DX":
        return True
    them, me = dxcc(e["call"]), dxcc(state["mycall"])
    if them and me:
        return them[1] != me[1]
    return (e.get("km") or 0) > 3000

def load_worked():
    """Only TODAY's contacts count (UTC date, as ADIF records qso_date), each
    as (call, band, mode) so a station can be worked again on another band or
    mode. Returns ({(call, band, mode): epoch time}, {path: mtime})."""
    import datetime
    utc = datetime.datetime.now(datetime.timezone.utc)
    today, yesterday = utc.strftime("%Y%m%d"), (utc - datetime.timedelta(days=1)).strftime("%Y%m%d")
    paths = ws_files("wsjtx_log.adi", ("~/.wsjtx",))
    w, mtimes = {}, {}
    for p in paths:
        if not os.path.exists(p):
            continue
        mtimes[p] = os.path.getmtime(p)
        txt = open(p, errors="ignore").read()
        # split into records, keep calls with qso_date == today
        for rec in txt.split("<eor"):
            md = re.search(r"<qso_date:\d+>(\d{8})", rec)
            if md and md.group(1) != today and md.group(1) != yesterday:
                continue
            mc = re.search(r"<call:\d+>([A-Za-z0-9/]+)", rec)
            if not mc:
                continue
            mb = re.search(r"<band:\d+>(\w+)", rec, re.I)
            mf = re.search(r"<freq:\d+>([\d.]+)", rec, re.I)
            # WSJT-X logs FT4 as MODE MFSK, SUBMODE FT4
            mm = (re.search(r"<submode:\d+>(\w+)", rec, re.I)
                  or re.search(r"<mode:\d+>(\w+)", rec, re.I))
            band_ = mb.group(1).lower() if mb else band_of(float(mf.group(1)) * 1e6) if mf else None
            # when the QSO ended (logged): a station repeating its R-report
            # soon after needs our RR73 again (rr73_again)
            mt = (re.search(r"<time_off:\d+>(\d{4,6})", rec, re.I)
                  or re.search(r"<time_on:\d+>(\d{4,6})", rec, re.I))
            dt_ = re.search(r"<qso_date_off:\d+>(\d{8})", rec, re.I) if mt and "time_off" in mt.group(0) else None
            ts = None
            if md and mt:
                ts = datetime.datetime.strptime((dt_ or md).group(1) + mt.group(1)[:4], "%Y%m%d%H%M").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            key = (mc.group(1).upper(), band_, mm.group(1).upper() if mm else None)
            if ts and time.time() - ts < 86400:
                RECENT[key] = ts
            if md and md.group(1) != today:
                continue  # yesterday's: only for the globe
            w[key] = ts
            mg = re.search(r"<gridsquare:\d+>(\w+)", rec, re.I)
            if mg:
                note_grid(mc.group(1), mg.group(1)[:4])
        log(f"worked today: {len(w)} total ({p})")
    return w, mtimes

def watcher():
    state["worked"], state["adi_mtime"] = load_worked()
    load_cty()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    CTX.update(tx=tx, myll=grid_to_latlon(state["mygrid"]) if state["mygrid"] else None)
    threading.Thread(target=udp_listener, daemon=True).start()
    # Reply TX still goes via UDP to WS
    threading.Thread(target=tail_alltxt, daemon=True).start()
    threading.Thread(target=cycle_ticker, daemon=True).start()

def cycle_ticker():
    """Engine tick at each T/R period boundary of the current mode (drives
    targeting even if ALL.TXT stalls)."""
    while True:
        per = period()
        nxt = (time.time() // per + 1) * per
        time.sleep(max(0.2, nxt - time.time()))
        CTX["cycle"][0] += 1
        try:
            engine_tick()
        except Exception as e:
            log(f"tick err: {e}")

CTX = {"tx": None, "myll": None, "seen": {}, "cycle": [0]}
# Targeting engine: holds {call: until_ts}, callers queue [{call, msg, t}], parity {0:n,1:n},
# idle_until ts (listen-only rest), busy_cycles (consecutive cycles with a target)
ENG = {"holds": {}, "held": {}, "callers": deque(maxlen=20), "parity": {0: 0, 1: 0},
       "idle_until": 0, "busy": 0, "idle_n": 0,
       "tx_n": 0, "check_until": 0,  # transmissions since the last interference check; pause ends
       "cq_until": 0,  # calling our own CQ until this time (0 = not calling)
       "occ": deque(maxlen=800),  # recent decodes {t, par, df}: who is where
       "spec": {},                # parity -> latest ws-hunter spectrum summary
       "noise_hist": deque(maxlen=40),  # per-period noise estimates (dB)
       "spec_pending": deque(maxlen=20),  # spectra waiting for their decodes
       "spec_log": deque(maxlen=20),      # evaluated spectra, for /api/debug/spectrum
       "spec_hist": deque(maxlen=60),     # recent spectra relative to their own floor: waterfall + weak signals
       "qrm_first": None,
       "tx_iv": deque(maxlen=50),          # our transmissions [on, off] (epoch s)
       "tune_iv": deque(maxlen=5),         # antenna tunes [on, off]: off the dial, nothing heard
       "qrm_until": 0, "qrm_n": 0, "qrm_why": "",
       "active": {},    # base call -> last time heard sending to someone (0 = signed off)
       "rr73_again": {},  # base call -> RR73s re-sent after the QSO was logged
       "calls_to": {}}  # base call -> {caller base call: last time}: their pileup

state["ws_id"] = "WSJT-X"   # learned from WS broadcasts when heard
state["ws_addr"] = None    # (ip, ephemeral port) WSJT-X listens on — learned from its packets
state["udp"] = {"holder": "?", "last_rx": 0, "dxcall": "", "tx_enabled": False,
                 "transmitting": False, "rx_count": 0}
state["decparams"] = {}  # msg -> {t_ms, snr, dt, df}

def wrl_relay(fwd):
    """Other half of the WRL Desktop bridge: anything WRL sends back to the
    socket we forward from (Heartbeats, Replies, Halt...) goes on to WS, as
    if WRL were talking to WS directly."""
    while True:
        try:
            data, _ = fwd.recvfrom(8192)
            if not state.get("ws_addr") or data[:4] != struct.pack(">I", 0xADBCCBDA):
                continue  # until WS is known, 2237 would just loop it back to WRL
            fwd.sendto(data, state["ws_addr"])
            rb = state["udp"].setdefault("wrl_by_type", {})
            mtype = struct.unpack(">I", data[8:12])[0]
            rb[mtype] = rb.get(mtype, 0) + 1
        except ConnectionResetError:
            continue  # Windows: an earlier forward found nothing on the WRL port (WRL closed)
        except Exception as e:
            log_err("WRL relay", e)
            time.sleep(1)

WRL = {"on": None, "t": 0}
def wrl_listening():
    """Is something (WRL Desktop) bound to the bridge port? If we can bind
    it ourselves, nothing is: forwarded packets, logged QSOs among them, are lost."""
    p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        p.bind(("127.0.0.1", state["config"]["wrl_port"]))
        return False
    except OSError:
        return True
    finally:
        p.close()

def wrl_check(now):
    """Every 10 s: note WRL Desktop coming and going, in the log too."""
    if now - WRL["t"] < 10:
        return
    WRL["t"] = now
    on = wrl_listening()
    if on != WRL["on"] and state["config"].get("wrl_warn", True):
        log(f"WRL Desktop is listening on {state['config']['wrl_port']}" if on else
            f"⚠️ WRL Desktop isn't running (nothing on port {state['config']['wrl_port']}): QSOs logged now won't reach it")
    WRL["on"] = on

# The radio out of step with WS: FLRig stops answering (the G90's CAT
# stalls), WS's dial goes to a frequency off the ham bands (30.000 MHz when
# it loses the rig) or away from the radio's VFO, or nothing is decoded. WS
# would go on transmitting blind, so the hunter stops and WS's TX is halted.
DESYNC = {"why": "", "t": 0, "since": {}, "stopped": False}
DESYNC_SECS = 15  # a sign must last this long (a band change moves the dial and the VFO a moment apart)
def desync_signs(now):
    """What is out of step right now, or ''."""
    if SCAN["on"] or ENG.get("tuning"):
        return ""  # moving the radio on purpose
    u, c = state["udp"], state["config"]
    dial, fs = u.get("dial"), RIG.get("fail_since")
    if dial and not band_of(dial):
        return f"WS's dial is {dial / 1e6:.3f} MHz, off the ham bands: it has lost the radio"
    if fs and now - fs >= DESYNC_SECS:
        return f"FLRig hasn't answered for {round(now - fs)} s ({RIG.get('err') or 'no reply'})"
    vfo = RIG.get("vfo") if RIG.get("ok") and now - RIG.get("t", 0) < 10 else None
    if dial and vfo and abs(vfo - dial) > 5000:
        return f"the radio is on {vfo / 1e6:.3f} MHz but WS's dial says {dial / 1e6:.3f}"
    last = max(u.get("last_decode", 0), max((m["seen"] for m in list(state["msgs"])[:1]), default=0))
    if c.get("silent_min") and state["running"] and now - u.get("last_rx", 0) < 10 \
            and now - max(last, DESYNC.get("started", 0)) > c["silent_min"] * 60:
        return f"nothing decoded for {c['silent_min']} min: no audio from the radio?"
    return ""

def desync_check(now):
    """Each period: a sign that lasts DESYNC_SECS stops the hunter and halts WS's TX."""
    why = desync_signs(now)
    key = why.split(":")[0][:20] if why else ""
    DESYNC["since"] = {key: DESYNC["since"].get(key, now)} if why else {}
    if why and now - DESYNC["since"][key] >= (DESYNC_SECS if why.startswith("the radio is on") else 0):
        if not DESYNC["why"]:
            DESYNC.update(why=why, t=now)
            stop = state["config"].get("desync_stop", True)
            log(f"⚠️ radio out of sync: {why}" + (" — hunter stopped, TX halted" if stop and state["running"] else ""))
            if stop:
                was = state["running"]
                state["running"] = False; state["target"] = None; ENG["cq_until"] = 0; ENG["callers"].clear()
                DESYNC["stopped"] = was or DESYNC["stopped"]
                if CTX["tx"] is not None:
                    halt_ws()
                    CTX["tx"].sendto(pack_set_bool(SET_TX_ENABLED, state["ws_id"], False), ws_dest())
                at("stopped", "radio out of sync")
        else:
            DESYNC["why"] = why  # keep the count of seconds current
    elif not why and DESYNC["why"]:
        log("radio back in sync with WS" + (": press Start to resume" if DESYNC["stopped"] else ""))
        DESYNC.update(why="", t=0)

def udp_listener():
    """Passive REUSEPORT listener: learns WS id, tracks Status/TX state."""
    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fwd.bind(("127.0.0.1", 0))  # fixed source port, so WRL's answers come back here
    threading.Thread(target=wrl_relay, args=(fwd,), daemon=True).start()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # on Windows this alone shares the port
    if hasattr(socket, "SO_REUSEPORT"):  # Linux, macOS; Windows has none
        try:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        rx.bind(("0.0.0.0", udp_port))
    except OSError as e:
        log(f"UDP listen failed: {e}"); return
    rx.settimeout(2.0)
    while True:
        try:
            data, addr = rx.recvfrom(8192)
        except socket.timeout:
            continue
        try:
            if struct.unpack(">II", data[:8])[0] != 0xADBCCBDA:
                continue
            # passthrough: rebroadcast WS packets so WRL Desktop can listen on fwd_port
            try:
                if state.get("ws_addr") and addr[0] == state["ws_addr"][0]:
                    fwd.sendto(data, ("127.0.0.1", state["config"]["wrl_port"]))
                elif not state.get("ws_addr"):
                    fwd.sendto(data, ("127.0.0.1", state["config"]["wrl_port"]))
            except Exception:
                pass
            off = 8  # magic(4) + schema(4)
            mtype = struct.unpack(">I", data[off:off+4])[0]; off += 4
            idlen = struct.unpack(">i", data[off:off+4])[0]; off += 4
            wid = data[off:off+max(idlen, 0)].decode("utf-8", "errors"); off += max(idlen, 0)
            bt = state["udp"].setdefault("by_type", {})
            bt[mtype] = bt.get(mtype, 0) + 1
            # Replies go to WSJT-X's EPHEMERAL port (it never binds 2237 itself)
            if mtype in (0, 1, 2, 5, 10, 12):
                state["ws_addr"] = (addr[0], addr[1])
            if mtype == 0:  # Heartbeat: max schema u32, version, revision
                try:
                    p = off + 4
                    vs = []
                    for _ in range(2):
                        ln = struct.unpack(">i", data[p:p+4])[0]; p += 4
                        vs.append(data[p:p+max(ln, 0)].decode("utf-8", "errors")); p += max(ln, 0)
                    if vs[1] != state["udp"].get("revision"):
                        state["udp"]["version"], state["udp"]["revision"] = vs
                        log(f"WS {vs[0]} {vs[1]}" + (" (ws-hunter controls)" if has_ws_hunter() else ""))
                except Exception as e:
                    log_err("Heartbeat", e)
            if "WS" in wid.upper() or "JT" in wid.upper():
                state["ws_id"] = wid
            state["udp"]["last_rx"] = time.time()
            state["udp"]["rx_count"] += 1
            if mtype in (1, 2):  # Status / Decode = genuine WS traffic
                state["udp"]["ws_rx"] = state["udp"].get("ws_rx", 0) + 1
                state["udp"]["last_rx"] = time.time()
            if mtype == 5:  # QSO Logged: contact done -> worked + release target
                try:
                    # Date&Time Off is a QDateTime: qint64 Julian day, quint32 ms,
                    # quint8 timespec (+ qint32 offset when timespec == 2)
                    q5 = off + 8 + 4
                    spec = data[q5]; q5 += 1
                    if spec == 2:
                        q5 += 4
                    ln = struct.unpack(">i", data[q5:q5+4])[0]; q5 += 4
                    dxcall = data[q5:q5+max(ln, 0)].decode("utf-8", "errors").strip().upper()
                    q5 += max(ln, 0)
                    lband = lmode = None
                    try:  # DX grid, Tx frequency (dial Hz), Mode
                        ln = struct.unpack(">i", data[q5:q5+4])[0]; q5 += 4
                        note_grid(dxcall, data[q5:q5+max(ln, 0)].decode("utf-8", "errors")[:4]); q5 += max(ln, 0)
                        lband = band_of(struct.unpack(">Q", data[q5:q5+8])[0]); q5 += 8
                        ln = struct.unpack(">i", data[q5:q5+4])[0]; q5 += 4
                        lmode = data[q5:q5+max(ln, 0)].decode("utf-8", "errors").strip().upper() or None
                    except struct.error:
                        pass  # short packet: assume the current band and mode
                    if dxcall:
                        add_worked(dxcall, lband, lmode)
                        record_origin(dxcall, lband, lmode)
                        t = state["target"]
                        if t and same_call(t["call"], dxcall):
                            log(f"logged QSO with {dxcall} — nice!")
                            state["target"] = None
                        else:
                            log(f"logged QSO with {dxcall}")
                except Exception as e:
                    log_err("QSO Logged parse", e)
            if mtype == 12:  # Logged ADIF: the contact as WS logged it, for the online logbooks
                try:
                    lb_enqueue(read_qstr(data, off)[0])
                except Exception as e:
                    log_err("Logged ADIF", e)
            if mtype == 1:  # Status body: DialFreq u64, Mode, DXcall, Report,
                try:           # TxMode, TxEnabled?, Transmitting?, ...
                    state["udp"]["dial"] = struct.unpack(">Q", data[off:off+8])[0]
                    set_band(band_of(state["udp"]["dial"]))
                    p = off + 8  # dialfreq u64
                    strs = []
                    for _ in range(4):  # Mode, DXcall, Report, TxMode
                        ln = struct.unpack(">i", data[p:p+4])[0]; p += 4
                        if ln < 0 or ln > 64:
                            break
                        strs.append(data[p:p+ln].decode("utf-8", "errors")); p += ln
                    if strs:
                        set_mode(strs[0])
                    if len(strs) >= 2:
                        state["udp"]["dxcall"] = strs[1]
                    if len(strs) == 4 and p + 2 <= len(data):
                        state["udp"]["tx_enabled"] = bool(data[p]); p += 1
                        txing = bool(data[p]); p += 1
                        note_tx(txing)
                        state["udp"]["transmitting"] = txing
                        state["udp"]["tx_t"] = time.time()
                        if p + 9 <= len(data):  # Decoding bool, Rx DF u32, Tx DF u32
                            state["udp"]["rx_df"], state["udp"]["tx_df"] = struct.unpack(">II", data[p+1:p+9])
                            # DE call, DE grid, DX grid, Tx watchdog, Sub-mode, Fast mode,
                            # Special op u8, Freq tol u32, T/R u32, Config name, Tx message
                            try:  # absent from older senders
                                q = p + 9
                                de_call, q = read_qstr(data, q)
                                de_grid, q = read_qstr(data, q)
                                _, q = read_qstr(data, q)  # DX grid
                                if (de_call, de_grid) != (state["udp"].get("de_call"), state["udp"].get("de_grid")):
                                    state["udp"]["de_call"], state["udp"]["de_grid"] = de_call, de_grid
                                    set_identity()
                                wd = bool(data[q]); q += 1
                                _, q = read_qstr(data, q)
                                q += 1 + 1 + 4 + 4
                                _, q = read_qstr(data, q)
                                state["udp"]["tx_msg"], _ = read_qstr(data, q)
                                state["udp"]["watchdog"] = wd
                            except (struct.error, ValueError, IndexError):
                                pass
                except Exception as e:
                    log_err("Status", e)
            if mtype == SPECTRUM:
                try:
                    if SCAN["on"]:
                        scan_spectrum(data, off)
                    else:
                        handle_spectrum(data, off)
                except Exception as e:
                    log_err("Spectrum", e)
            if mtype == 2 and CTX["tx"] is not None:  # live Decode: exact params!
                try:
                    p = off + 1  # New flag (off already past type+id)
                    t_ms = struct.unpack(">I", data[p:p+4])[0]; p += 4
                    dsnr = struct.unpack(">i", data[p:p+4])[0]; p += 4
                    ddt = struct.unpack(">d", data[p:p+8])[0]; p += 8
                    ddf = struct.unpack(">I", data[p:p+4])[0]; p += 4
                    strs2, q = [], p
                    for _ in range(2):
                        ln = struct.unpack(">i", data[q:q+4])[0]; q += 4
                        if ln < 0 or ln > 100:
                            break
                        strs2.append(data[q:q+ln].decode("utf-8", "errors")); q += ln
                    if len(strs2) == 2:
                        dmode, dmsg = strs2
                        set_decparams(dmsg, {"t_ms": t_ms, "snr": dsnr,
                            "dt": ddt, "df": ddf, "mode": dmode})
                        state["udp"]["last_decode"] = time.time()
                        if t_ms in SCAN["by_tm"]:
                            SCAN["by_tm"][t_ms]["decodes"] += 1  # FT2 heard while scanning
                        handle_decode(dmsg, dsnr, CTX["tx"], CTX["myll"],
                                      CTX["seen"], CTX["cycle"], t_ms)
                except Exception as e:
                    log_err("Decode", e)
        except Exception as e:
            log_err("UDP packet", e)

def set_decparams(msg, params):
    """Latest exact decode params per message. Re-inserting moves the entry to
    the end, so trimming drops the stalest messages, not active CQs."""
    d = state["decparams"]
    d.pop(msg, None)
    d[msg] = params
    ENG["occ"].append({"t": time.time(), "tm": params["t_ms"], "par": parity(params["t_ms"]),
                       "df": params["df"], "snr": params.get("snr"), "msg": msg})
    if len(d) > 500:
        state["decparams"] = dict(list(d.items())[-300:])

ALL_RE = re.compile(r"^(\d{6}_\d{6})\s+[\d.]+\s+Rx\s+\S+\s+([+-]?\d+)\s+([\d.]+)\s+(\d+)\s+(.*\S)\s*$")
TX_RE = re.compile(r"^(\d{6}_\d{6})\s+[\d.]+\s+Tx\s+\S+\s+([+-]?\d+)\s+([\d.]+)\s+(\d+)\s+(.*\S)\s*$")

def tail_alltxt():
    myll = CTX["myll"]
    seen, cycle = CTX["seen"], CTX["cycle"]
    tx = CTX["tx"]
    p = None
    for cand in ws_files("ALL.TXT"):
        if os.path.exists(cand):
            p = cand; break
    if not p:
        log("no ALL.TXT found (backup feed off, UDP feed active)"); return
    log(f"backup feed: {p}")
    try:
        fh = open(p, errors="ignore")
        fh.seek(0, 2)
    except FileNotFoundError:
        log("ALL.TXT not found"); return
    buf = ""
    while True:
        chunk = fh.readline()
        if not chunk:
            time.sleep(1); continue
        buf += chunk
        if not buf.endswith("\n"):
            continue  # WS is mid-write; wait for the rest of the line
        line, buf = buf.strip(), ""
        try:
            m = ALL_RE.match(line)
            if not m:
                tm = TX_RE.match(line)
                if tm:  # our own transmissions only appear here
                    state["mytx"].appendleft({"msg": tm.group(5).strip(),
                        "t": time.strftime("%H:%M:%S"), "seen": time.time()})
                    on_my_tx(tm.group(5).strip())
                continue
            # Decodes are a backup feed only: while UDP decodes flow, the same
            # decode would otherwise be handled twice.
            if time.time() - state["udp"].get("last_decode", 0) < 30:
                continue
            tstr, snr, dt, audio, msg = m.group(1), int(m.group(2)), float(m.group(3)), int(m.group(4)), m.group(5).strip()
            set_decparams(msg, {"t_ms": alltxt_time_ms(tstr), "snr": snr, "dt": dt, "df": audio})
            handle_decode(msg, snr, tx, myll, seen, cycle)
        except Exception as e:
            log_err("ALL.TXT", e)

def read_qstr(data, p):
    """QDataStream QByteArray/utf8 string at p: (text, next offset). Null is ''."""
    ln = struct.unpack(">I", data[p:p+4])[0]; p += 4
    if ln == 0xFFFFFFFF:
        return "", p
    if p + ln > len(data):
        raise ValueError("string past end")
    return data[p:p+ln].decode("utf-8", "replace"), p + ln

CONFIG_FILE = os.path.join(DATA_DIR, "hunter_config.json")
def save_config():
    """Settings changed in the UI survive a restart."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(state["config"], f, indent=1)
    except OSError as e:
        log(f"settings not saved: {e}")

def load_config():
    try:
        saved = json.load(open(CONFIG_FILE))
    except (OSError, ValueError):
        return
    state["config"].update({k: v for k, v in saved.items() if k in state["config"]})

def set_identity():
    """Our call and grid: the Settings ones, else what WS reports (DE call / grid)."""
    c, u = state["config"], state["udp"]
    call = (c["mycall"] or u.get("de_call") or "").strip().upper()
    grid = (c["mygrid"] or u.get("de_grid") or "").strip()
    grid = grid[:2].upper() + grid[2:4] + grid[4:6].lower()
    if (call, grid) != (state["mycall"], state["mygrid"]):
        state["mycall"], state["mygrid"] = call, grid
        CTX["myll"] = grid_to_latlon(grid) if grid else None
        if call:
            log(f"station: {call} {grid}" + ("" if c["mycall"] else " (from WS)"))

def ws_dest():
    return state["ws_addr"] or ("127.0.0.1", udp_port)

def halt_ws():
    try:
        CTX["tx"].sendto(pack_halt(state["ws_id"]), ws_dest())
    except Exception:
        pass

# Contacts and holds count per band and mode: a station worked (or ignoring
# us) on 20m FT8 can still be worked on 20m FT4 or 40m FT8.
BANDS = [("160m", 1.8, 2.0), ("80m", 3.5, 4.0), ("60m", 5.06, 5.45), ("40m", 7.0, 7.3),
         ("30m", 10.1, 10.15), ("20m", 14.0, 14.35), ("17m", 18.068, 18.168),
         ("15m", 21.0, 21.45), ("12m", 24.89, 24.99), ("10m", 28.0, 29.7),
         ("6m", 50.0, 54.0), ("4m", 70.0, 71.0), ("2m", 144.0, 148.0), ("70cm", 420.0, 450.0)]

def band_of(hz):
    mhz = hz / 1e6
    return next((b for b, lo, hi in BANDS if lo <= mhz <= hi), None)

def band_of_name(name):
    return any(b == name for b, lo, hi in BANDS)

def band():
    return state["udp"].get("band")

def slot_key(call):
    return (base_call(call), band(), mode())

def on_hold(call):
    return ENG["holds"].get(slot_key(call), 0) > time.time()

def hold(call):
    """Hold a call for hold_min minutes on this band and mode; returns the minutes."""
    mins = state["config"].get("hold_min", 10)
    ENG["holds"][slot_key(call)] = time.time() + mins * 60
    ENG["held"][slot_key(call)] = (call, time.time())  # for the worked/ignored list
    return mins

def unhold(call):
    ENG["holds"].pop(slot_key(call), None)
    ENG["held"].pop(slot_key(call), None)  # they answered after all

def add_worked(call, band_=None, mode_=None):
    key = (call.upper(), band_ or band(), mode_ or mode())
    state["worked"][key] = RECENT[key] = time.time()

def is_worked(call):
    """Worked today on this band and mode. A log entry missing its band or
    mode (or no band known yet) matches any."""
    b, cb, cm = base_call(call), band(), mode()
    return is_me(call) or any(base_call(w) == b and (not wb or not cb or wb == cb)
                              and (not wm or wm == cm)
                              for w, wb, wm in list(state["worked"]))

def prune(now):
    """Drop stale entries so long sessions don't grow without bound."""
    for d in (state["cq"], state["answering"]):
        for k in [k for k, e in list(d.items()) if now - e["seen"] > 600]:
            d.pop(k, None)
    for k in [k for k, p in list(PAIRS.items()) if now - p["t"] > state["config"]["traffic_secs"]]:
        PAIRS.pop(k, None)
    for c in [c for c, until in list(ENG["holds"].items()) if until <= now]:
        ENG["holds"].pop(c, None)
    for c in [c for c, ts in list(ENG["active"].items()) if now - ts > 600]:
        ENG["active"].pop(c, None)
    for c, callers in list(ENG["calls_to"].items()):
        if all(now - ts > 60 for ts in list(callers.values())):
            ENG["calls_to"].pop(c, None)

# T/R period (s) and signal width above its DF (Hz) per mode
MODES = {"FT8": (15.0, 50), "FT4": (7.5, 83), "FT2": (3.75, 167)}
GUARD = 15  # clearance each side of a signal (Hz)

def mode():
    m = state["udp"].get("mode", "FT8")
    return m if m in MODES else "FT8"

def period():
    """Current T/R period in seconds (15 FT8, 7.5 FT4, 3.75 FT2)."""
    return MODES[mode()][0]

def bw():
    return MODES[mode()][1]

def cyc(n):
    """n T/R periods in seconds: timing rules scale with the mode."""
    return n * period()

def parity(t_ms):
    """0 = even (first) period of the minute, 1 = odd."""
    return int(t_ms // (period() * 1000)) % 2

def set_mode(new):
    """WS changed mode: decodes, CQs and occupancy from the old mode no longer
    apply (a Reply must match a decode in the current mode)."""
    old = state["udp"].get("mode")
    state["udp"]["mode"] = new
    if old is None or old == new:
        return
    new_slot()
    if not SCAN["on"]:
        log(f"mode {old} -> {new}: {period()} s periods")

def set_band(new):
    """WS moved to another band: same reset as a mode change."""
    old = state["udp"].get("band")
    state["udp"]["band"] = new
    if ENG.get("tuned_band") is None and new:
        ENG["tuned_band"] = new  # at startup the band we are on is taken as tuned
    if old is None or old == new or new is None:
        return
    new_slot()
    if not SCAN["on"]:
        ENG["arrived"] = time.time()  # listen before transmitting here (new_band_wait)
        log(f"band {old} -> {new}")

def new_slot():
    """Decodes, CQs and occupancy from the old band/mode no longer apply.
    Holds and contacts are kept: they are per band and mode already."""
    if state["target"]:
        halt_ws()
    state["target"] = None
    for k in ("cq", "answering", "decparams"):
        state[k].clear()
    state["msgs"].clear()
    for k in ("callers", "occ", "noise_hist", "spec_pending", "spec_hist"):
        ENG[k].clear()
    for k in ("spec", "active", "calls_to"):
        ENG[k].clear()
    ENG["cq_until"] = 0

RECENT_PERIODS = 2  # same-parity periods the offset picker looks back
LOOK_BACK = 20       # how far back (same-parity periods) it looks for ones we heard
def heard_periods(par, now, n=RECENT_PERIODS):
    """Starts (UTC ms of day) of the last n ended periods of parity `par` we
    listened in, newest first. Periods we transmitted in are skipped, not
    counted as empty: on the parity we transmit on, the last ones we heard
    may be minutes old, but they are all we know about who is there."""
    tr = int(period() * 1000)
    t = (tod_ms(now) // tr - 1) * tr  # the newest ended period
    if (t // tr) % 2 != par:
        t -= tr
    out = []
    for k in range(LOOK_BACK):
        s = (t - 2 * k * tr) % 86400000
        if not overlaps_tx(s, tr):
            out.append(s)
            if len(out) == n:
                break
    return out

def occupied(par, now):
    """Occupied audio intervals [lo, hi) on one T/R parity, from the last two
    periods of it we heard (someone who left a frequency before that has
    given it up): decodes plus, on the ws-hunter build, spectrum bins above
    the quiet level."""
    heard = set(heard_periods(par, now))
    iv = [(o["df"] - GUARD, o["df"] + bw() + GUARD) for o in list(ENG["occ"])
          if o["par"] == par and o.get("tm") in heard]
    ex = spec_excess(par, now, periods=heard)
    if ex:
        iv += [(f - GUARD, f + ex[3] + GUARD) for f in weak_hot(par, now, ex)]
    return iv

# Offset picker cost. Replaying 40 min of a busy 20 m band: moves 42% ->
# 22% of periods, picks stepped on 27% -> 22%. A decode overlapping us in
# full costs 1 (up to 2 if strong); the newest heard period counts in full,
# each older one 0.35 as much. Undecoded signals (weak_hot: persistent or
# strong, from the two newest periods) cost 0.7 per 50 Hz.
PICK_PERIODS, PICK_DECAY, PICK_ENERGY, PICK_GUARD = 3, 0.35, 0.7, 0.2
PICK_HYST = 0.6  # keep the current offset unless the best is this much better
PICK_HYST_NEW = 0.2  # ...or this much, starting a new contact (nobody is listening for us there yet)

def tx_cost(par, now, dx=None):
    """Cost of transmitting at each offset on parity `par`, from the last
    PICK_PERIODS periods of it we heard: overlap with decodes (by strength,
    the newest weighted most) and with undecoded signals; a little for
    sitting inside someone's guard band. With `dx`, also what's busy at
    their end (remote_sigs)."""
    cfg, w = state["config"], bw()
    lo, hi = cfg.get("df_lo", 1300) - 100, cfg.get("df_hi", 2700) + 100
    heard = heard_periods(par, now, PICK_PERIODS)
    wt = {t: PICK_DECAY ** k for k, t in enumerate(heard)}
    sigs = []  # (lo Hz, hi Hz, weight)
    for o in list(ENG["occ"]):
        if o["par"] == par and o.get("tm") in wt:
            snr = o["snr"] if o.get("snr") is not None else -15
            sigs.append((o["df"], o["df"] + w, wt[o["tm"]] * (1 + max(0, snr + 20) / 10)))
    ex = spec_excess(par, now, periods=set(heard[:2]))
    if ex:
        sigs += [(f, f + ex[3], PICK_ENERGY) for f in weak_hot(par, now, ex) if lo <= f < hi]
    sigs += remote_sigs(par, now, dx)
    def cost(f):
        c = 0.0
        for a, b, k in sigs:
            ov = min(b, f + w) - max(a, f)
            if ov > 0:
                c += k * ov / w
            elif ov > -GUARD:
                c += k * PICK_GUARD * (GUARD + ov) / GUARD
        return c
    return cost

def pick_tx_df(par, current=None, dx=None):
    """Least crowded Tx offset on parity `par` (tx_cost), nearest the current
    one among equals. Keeps `current` unless the best is clearly better
    (PICK_HYST): a faint trace or a decode brushing our edge isn't worth a
    move. Starting a new contact (dx["new"]) the bar is lower, PICK_HYST_NEW,
    so one signal at the DX's end is reason enough. Even a crowded band gets the least bad spot; below df_lo (the rig
    feeds back) or above df_hi we always move into the range."""
    cfg = state["config"]
    lo, hi = cfg.get("df_lo", 1300), cfg.get("df_hi", 2700)
    cost = tx_cost(par, time.time(), dx)
    cands = range(lo, hi - bw() + 1, 10)
    if not cands:
        return current
    c = {f: round(cost(f), 3) for f in cands}
    near = current if current is not None else (lo + hi - bw()) / 2  # nothing heard: mid-range
    best = min(cands, key=lambda f: (c[f], abs(f - near)))
    hyst = PICK_HYST_NEW if dx and dx.get("new") else PICK_HYST
    if current is not None and lo <= current <= hi - bw() and cost(current) <= c[best] + hyst:
        return current
    return best

def heard_cycle(now):
    """Have we just heard a full cycle: a spectrum (heard, not transmitted
    in) of each parity among the last three ended periods? The newest one's
    spectrum may still be on its way, so the one a cycle before stands in."""
    tr = int(period() * 1000)
    last = (tod_ms(now) // tr - 1) * tr
    want = {(last - k * tr) % 86400000 for k in range(3)}
    return {h["par"] for h in list(ENG["spec_hist"]) if h["t_ms"] in want} >= {0, 1}

# The DX's end of the band: what receivers near the DX decoded in the period
# we transmit in, from PSK Reporter. One query per dial and mode every
# PSK_EVERY s (their limit is one per 5 min), only while the hunter runs.
# A signal heard near the DX costs PSK_W of one heard here (it's minutes
# old, and only decoded signals are reported); the DX's own offset costs
# PSK_DX_W on our parity, where callers who follow the DX pile up.
PSK = {"key": None, "t": 0, "reports": [], "err": "", "busy": False}
PSK_URL = "https://retrieve.pskreporter.info/query"
PSK_EVERY, PSK_KEEP, PSK_NEAR_KM = 300, 900, 1000
PSK_W, PSK_DX_W = 0.5, 0.5

def psk_parse(text, dial, md):
    """Reception reports -> [{rx, rll, tx, sll, df, par, t, snr}]. A report's
    time is its period's start or when it was decoded, at the end; the
    period is clear either way, and reports timed in between are dropped."""
    import xml.etree.ElementTree as ET
    tr, out = MODES[md][0], []
    for e in ET.fromstring(text).iter("receptionReport"):
        a = e.attrib
        try:
            t, df = int(a["flowStartSeconds"]), int(a["frequency"]) - dial
        except (KeyError, ValueError):
            continue
        k = int(t // tr)
        r = t - k * tr
        rll, sll = (next((grid_to_latlon(g) for g in (a.get(k, "")[:6].upper(), a.get(k, "")[:4].upper())
                          if GRID_RE.match(g)), None) for k in ("receiverLocator", "senderLocator"))
        if not (100 <= df <= 3100 and rll) or 0.15 * tr + 0.75 < r < 0.7 * tr:
            continue
        snr = a.get("sNR", "")
        out.append({"rx": a.get("receiverCallsign", ""), "rll": rll, "tx": a.get("senderCallsign", ""),
                    "sll": sll, "df": df, "par": k % 2,
                    "t": t, "snr": int(snr) if snr.lstrip("-").isdigit() else None})
    return out

def psk_fetch(key, dial, md):
    import urllib.request
    url = f"{PSK_URL}?frange={dial}-{dial + 3000}&mode={md}&flowStartSeconds=-{PSK_KEEP}&rronly=1&noactive=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FT8-Hunter"})
        with urllib.request.urlopen(req, timeout=60) as r:
            reps = psk_parse(r.read().decode("utf-8", "ignore"), dial, md)
        if PSK["key"] == key:
            PSK.update(reports=reps, err="")
    except Exception as e:
        PSK["err"] = str(e)[:120]
    finally:
        PSK["busy"] = False

def psk_tick(now):
    """Fetch PSK Reporter's view of this dial and mode when it's due."""
    if not (state["config"].get("psk_remote") and state["running"]) or SCAN["on"] or PSK["busy"]:
        return
    dial, md = state["udp"].get("dial"), mode()
    if not dial or not band_of(dial) or md not in MODES:
        return
    key = (int(dial), md)
    if now - PSK["t"] < (PSK_EVERY if key == PSK["key"] else 60):  # a new band: soon, never hammering
        return
    if key != PSK["key"]:
        PSK["reports"] = []
    PSK.update(key=key, t=now, busy=True)
    threading.Thread(target=psk_fetch, args=(key, int(dial), md), daemon=True).start()

def psk_near(dx, par, now):
    """Receivers within PSK_NEAR_KM of the DX, and the newest report of each
    other station they decoded on parity `par` (not us, not the DX)."""
    sigs, rxs = {}, {}
    ll = (dx or {}).get("ll")
    if not ll or (PSK["key"] and PSK["key"][1] != mode()):
        return [], {}
    for r in list(PSK["reports"]):
        if now - r["t"] > PSK_KEEP or (km(ll, r["rll"]) or 1e9) > PSK_NEAR_KM:
            continue
        rxs[r["rx"]] = r["rll"]
        if r["par"] != par or is_me(r["tx"]) or same_call(r["tx"], dx["call"]):
            continue
        if r["tx"] not in sigs or r["t"] > sigs[r["tx"]]["t"]:
            sigs[r["tx"]] = r
    return list(sigs.values()), rxs

def remote_sigs(par, now, dx):
    """(lo Hz, hi Hz, weight) for the offset picker from the DX's end."""
    if not (dx and state["config"].get("psk_remote")):
        return []
    w = bw()
    near, _ = psk_near(dx, par, now)
    out = [(r["df"], r["df"] + w, PSK_W * (1 if now - r["t"] < PSK_EVERY else 0.5)) for r in near]
    if dx.get("df") is not None:
        out.append((dx["df"], dx["df"] + w, PSK_DX_W))
    return out

def remote_fresh(dx, now):
    """Is there a recent picture of the DX's end (receivers near them)?"""
    if not (dx and state["config"].get("psk_remote") and PSK["t"] and now - PSK["t"] < 2 * PSK_EVERY):
        return False
    return bool(psk_near(dx, 0, now)[1])

def remote_note(f, par, now, dx):
    """For the log: is offset f clear at the DX's end on parity `par`?"""
    if not remote_fresh(dx, now):
        return ""
    near, rxs = psk_near(dx, par, now)
    w = bw()
    busy = [r["tx"] for r in near if r["df"] < f + w + GUARD and r["df"] + w + GUARD > f]
    on_dx = dx.get("df") is not None and dx["df"] < f + w + GUARD and dx["df"] + w + GUARD > f
    what = (f"busy near {dx['call']} ({', '.join(busy[:3])})" if busy else f"clear near {dx['call']}")
    return f"; {what}, {len(rxs)} rx there" + ("; on their own offset" if on_dx else "")

def remote_view(now):
    """The DX's end for the waterfall and globe: who is near them and what
    they hear on our Tx parity, or None without a target."""
    dx, t = ENG.get("dx"), state["target"]
    if not (dx and t and same_call(dx["call"], t["call"]) and state["config"].get("psk_remote")):
        return None
    par = ENG.get("suggest", {}).get("par")
    near, rxs = psk_near(dx, par, now) if par is not None else ([], {})
    w = bw()
    return {"call": dx["call"], "ll": dx.get("ll"), "df": dx.get("df"), "par": par, "km": PSK_NEAR_KM,
            "rx": list(rxs.values())[:200], "n_rx": len(rxs),
            "sigs": [{"call": r["tx"], "ll": r["sll"], "df": r["df"], "snr": r["snr"], "age": round(now - r["t"])}
                     for r in sorted(near, key=lambda r: r["df"])],
            "occupied": merged([(r["df"] - GUARD, r["df"] + w + GUARD) for r in near]),
            "age": round(now - PSK["t"]) if PSK["t"] else None, "err": PSK["err"]}

def tune_tx(par, can_wait=False, dx=None):
    """Before transmitting on parity `par`: hold Tx freq (so a Reply can't drag
    us onto the DX) and move to a clear offset. Needs the ws-hunter build;
    on stock WS the pick is only shown in the UI. A move is only made after
    a full cycle heard: otherwise, with can_wait, listen a cycle first and
    return False (transmit nothing yet); without it (someone is calling us),
    stay where we are this time."""
    now = time.time()
    cur = state["udp"].get("tx_df")
    ENG["dx"] = dx
    ENG.pop("picked", None)
    f = pick_tx_df(par, cur, dx)
    lock = bool(state["config"].get("df_lock")) and cur is not None
    ENG["suggest"] = {"par": par, "df": f, "locked": lock, "busy": lock and f != cur}
    if not (has_ws_hunter() and state["config"].get("auto_df", True)):
        return True
    move = f and f != cur and not lock  # locked: the operator chose this offset, keep it
    # The full-cycle rule guards picks made on stale spectra; a fresh look at
    # the DX's end is new information enough to move on.
    if move and cur is not None and not heard_cycle(now) and not remote_fresh(dx, now):
        if can_wait and now - ENG.get("fresh_check", 0) > cyc(6):  # once: the spectra may not come at all
            ENG["fresh_check"] = now
            start_check(now, f"before moving off {cur} Hz")
            return False
        if now - ENG.get("fresh_check", 0) > cyc(6):
            move = False  # nothing heard lately to pick by
    tx, wid = CTX["tx"], state["ws_id"]
    tx.sendto(pack_set_bool(SET_HOLD_TX_FREQ, wid, True), ws_dest())
    ENG["picked"] = (f if move else cur, par)
    if move:
        tx.sendto(pack_set_u32(SET_TX_DF, wid, f), ws_dest())
        log(f"Tx offset {cur} -> {f} Hz ({'even' if par == 0 else 'odd'}{remote_note(f, par, now, dx)})")
    return True

def tod_ms(ts):
    """UTC time of day in ms, the clock WS uses for period start times."""
    return int(ts * 1000) % 86400000

def note_tx(txing):
    """Record our transmit intervals from Status, so spectra of periods we
    transmitted in (the rig mutes its receive audio) are never judged."""
    now, iv = time.time(), ENG["tx_iv"]
    if txing and not (iv and iv[-1][1] is None):
        iv.append([now, None])
        ENG["tx_n"] += 1
    elif not txing and iv and iv[-1][1] is None:
        iv[-1][1] = now

def overlaps_tx(t_ms, tr_ms, margin_ms=1000):
    """Did any of our transmissions reach more than margin_ms into this
    period? Not padded outward: PTT comes up right at the start of our next
    period and Status reports the drop late, so padding would count every
    receive period between two of ours as transmitted (no waterfall)."""
    end = t_ms + tr_ms
    for on, off in list(ENG["tx_iv"]) + list(ENG["tune_iv"]):
        a, b = tod_ms(on), tod_ms(off if off else time.time())
        if a < end - margin_ms and b > t_ms + margin_ms:
            return True
    return False

def handle_spectrum(data, off):
    """ws-hunter SpectrumSummary: period QTime ms, T/R ms, start Hz, bin Hz,
    floor dB, step dB, levels. Marks hot bins for the offset picker right
    away; the busy-frequency check waits for that period's decodes."""
    t_ms, tr_ms, start, bin_hz, db = parse_spectrum(data, off)
    freqs = [start + i * bin_hz for i in range(len(db))]
    band = sorted(d for f, d in zip(freqs, db) if 200 <= f < 2800)
    if not band or overlaps_tx(t_ms, tr_ms):
        return  # we transmitted: the receiver was muted, this is not the band
    p10 = band[len(band) // 10]
    if len(ENG["noise_hist"]) >= 4 and p10 < sorted(ENG["noise_hist"])[len(ENG["noise_hist"]) // 2] - 15:
        return  # far below the usual floor: muted audio (TX we missed, or RX off)
    # Noise floor from history, not this period alone: a voice signal can
    # fill the whole passband, so this period's own floor would be the signal.
    ENG["noise_hist"].append(p10)
    hist = sorted(ENG["noise_hist"])
    noise = hist[len(hist) // 4]
    now, par = time.time(), int(t_ms // max(tr_ms, 1)) % 2
    # each bin relative to this period's own floor, so the waterfall and the
    # weak-signal test don't care about band noise or gain changes
    ENG["spec_hist"].append({"t": now, "t_ms": t_ms, "tr_ms": tr_ms, "par": par, "start": start,
                             "bin": bin_hz, "rel": [d - p10 for d in db]})
    ENG["spec"][par] = {"t": now, "bin": bin_hz, "hot": weak_hot(par, now), "noise": noise}
    ENG["spec_pending"].append({"t": now, "t_ms": t_ms, "tr_ms": tr_ms, "par": par, "bin": bin_hz,
                                "freqs": freqs, "db": db, "noise": noise,
                                "warm": len(ENG["noise_hist"]) >= 8})
    check_qrm(now)

def spec_baseline(tr_ms, nbins):
    """Each bin's usual quiet level (relative to the period floor): a low
    percentile over recent periods of both parities. It holds the receiver's
    passband slope and steady birdies; stations come and go, so they don't
    pull it up. None until enough periods are in."""
    rows = [h["rel"] for h in list(ENG["spec_hist"]) if h["tr_ms"] == tr_ms and len(h["rel"]) == nbins]
    if len(rows) < 8:
        return None
    k = len(rows) // 4
    return [sorted(col)[k] for col in zip(*rows)]

def spec_excess(par, now, n=RECENT_PERIODS, periods=None):
    """(latest, average) excess over the baseline in dB per bin, for the last
    n spectra of parity `par` (none older than that, or those of `periods`),
    with their bin layout."""
    hist = [h for h in list(ENG["spec_hist"]) if h["par"] == par
            and (h["t_ms"] in periods if periods is not None else now - h["t"] < cyc(2 * n) + 5)]
    if not hist:
        return None
    last = hist[-1]
    hist = [h for h in hist[-n:] if h["tr_ms"] == last["tr_ms"] and len(h["rel"]) == len(last["rel"])]
    base = spec_baseline(last["tr_ms"], len(last["rel"]))
    exs = [[r - b for r, b in zip(h["rel"], base or [0.0] * len(h["rel"]))] for h in hist]
    avg = [sum(c) / len(c) for c in zip(*exs)]
    return exs[-1], avg, last["start"], last["bin"], base is not None

WEAK_DB, NEW_DB, STRONG_DB = 1.2, 3.0, 8.0  # see weak_hot
def weak_hot(par, now, ex=None):
    """Bins with someone transmitting on parity `par`, decodable or not.
    Bin noise is ~0.5 dB about its baseline, so a run of 3+ bins (30 Hz)
    that is up 1.2 dB on average over the last two periods, or 3 dB in the
    latest, is a signal (a -20 dB FT8 signal is ~+2 dB); so is any single
    bin 8 dB up. Catches stations too weak to decode here who would still
    be on top of us at the far end."""
    ex = ex or spec_excess(par, now)
    if not ex:
        return []
    last, avg, start, bin_hz, learned = ex
    if not learned:  # the passband slope would look like signals: strong ones only
        return [start + i * bin_hz for i, l in enumerate(last) if l >= STRONG_DB and 200 <= start + i * bin_hz < 3000]
    hot, run = [], []
    for i, (l, a) in enumerate(zip(last, avg)):
        if l >= NEW_DB or a >= WEAK_DB:
            run.append(i)
            continue
        if len(run) >= 3:
            hot += run
        run = []
    if len(run) >= 3:
        hot += run
    hot = set(hot) | {i for i, l in enumerate(last) if l >= STRONG_DB}
    return [start + i * bin_hz for i in sorted(hot) if 200 <= start + i * bin_hz < 3000]

def merged(iv):
    """Sorted, overlapping [lo, hi) intervals joined."""
    out = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out

def waterfall(rows=40):
    """The last `rows` T/R periods for the web UI, newest first: each bin's
    excess over its usual quiet level (byte = 20 + 4 * dB, as hex), or a
    gap marked as our transmission or no data; the decodes in them; what
    the offset picker counts as occupied on each parity; and our offsets."""
    now = time.time()
    tr = int(period() * 1000)
    by_t = {h["t_ms"]: h for h in list(ENG["spec_hist"]) if h["tr_ms"] == tr}
    nb = len(next(iter(by_t.values()))["rel"]) if by_t else 0
    base = (spec_baseline(tr, nb) or [0.0] * nb) if nb else []
    last = (tod_ms(now) // tr - 1) * tr  # the newest period that has ended
    out = []
    for k in range(rows):
        t = (last - k * tr) % 86400000
        h = by_t.get(t)
        if h:
            ex = bytes(max(0, min(255, round(20 + 4 * (r - b)))) for r, b in zip(h["rel"], base))
            out.append({"t_ms": t, "par": h["par"], "ex": ex.hex()})
        else:
            out.append({"t_ms": t, "par": int(t // tr) % 2, "tx": overlaps_tx(t, tr, 0)})
    ts = {r["t_ms"] for r in out}
    decs = [{"tm": o["tm"], "df": o["df"], "snr": o.get("snr"), "msg": o.get("msg", "")}
            for o in list(ENG["occ"]) if o.get("tm") in ts]
    h0 = next(iter(by_t.values()), None)
    cfg, sug = state["config"], ENG.get("suggest", {})
    return {"tr_ms": tr, "bw": bw(), "start": h0["start"] if h0 else 0, "bin": h0["bin"] if h0 else 10,
            "rows": out, "decodes": decs,
            "occupied": {p: merged(occupied(p, now)) for p in (0, 1)},
            "tx_df": state["udp"].get("tx_df"), "tx_par": sug.get("par"), "suggest": sug.get("df"),
            "df_lo": cfg.get("df_lo", 1300), "df_hi": cfg.get("df_hi", 2700),
            "learned": len(by_t) >= 8, "ws_hunter": has_ws_hunter(),
            "df_lock": bool(state["config"].get("df_lock")), "remote": remote_view(now)}

def check_qrm(now=None):
    """Busy-frequency check for spectra whose decodes are in by now (decoding
    finishes after the period ends): strong bins that no decode from that
    period, or the same-parity period before it, explains. A run of 400 Hz
    or more, persisting ~25 s, means someone else (SSB, RTTY...) is there."""
    now = now or time.time()
    settle = max(period(), 5)
    while ENG["spec_pending"] and now - ENG["spec_pending"][0]["t"] >= settle:
        sp = ENG["spec_pending"].popleft()
        tr = sp["tr_ms"]
        dec_iv = [(o["df"] - GUARD, o["df"] + bw() + GUARD) for o in list(ENG["occ"])
                  if 0 <= (sp["t_ms"] - o.get("tm", -1)) % 86400000 <= 2 * tr
                  and o["par"] == sp["par"]]
        run = best = 0
        for f, d in zip(sp["freqs"], sp["db"]):
            strong = d > sp["noise"] + 12 and not any(a <= f < b for a, b in dec_iv)
            run = run + sp["bin"] if strong else 0
            best = max(best, run)
        ENG["spec_log"].append({"t_ms": sp["t_ms"], "par": sp["par"], "noise": round(sp["noise"], 1),
                                "warm": sp["warm"], "explained": len(dec_iv), "wide_hz": best,
                                "db": [round(d, 1) for d in sp["db"]]})
        if not (state["config"].get("qrm_detect", True) and sp["warm"]):
            continue
        if best < 400:
            ENG["qrm_n"], ENG["qrm_first"] = 0, None
            continue
        ENG["qrm_n"] += 1
        if ENG["qrm_first"] is None:
            ENG["qrm_first"] = sp["t_ms"]
        span = (sp["t_ms"] - ENG["qrm_first"]) % 86400000
        if ENG["qrm_n"] >= 2 and span >= 25000 and now >= ENG["qrm_until"]:
            mins = state["config"].get("qrm_backoff_min", 5)
            ENG["qrm_until"] = now + mins * 60
            ENG["qrm_why"] = f"{best} Hz of undecoded signal"
            log(f"non-FT8 activity on frequency ({best} Hz wide) — no new calls for {mins}m")
            if ENG["cq_until"]:
                ENG["cq_until"] = 0; halt_ws()

def rr73_again(call, rest, now):
    """A station we just logged repeating its R-report never copied our RR73,
    and one repeating RRR never copied our 73: without another one the QSO may
    be missing from their log. Worth answering within 4 minutes of the
    contact, at most 3 more times."""
    if not (rest and (rest[-1] == "RRR" or re.fullmatch(r"R[+-]\d\d", rest[-1]))):
        return False
    here = (band(), mode())
    ts = max((t for (c, b, m), t in list(state["worked"].items()) if (b, m) == here and same_call(c, call)),
             default=0)
    return now - (ts or 0) < 240 and ENG["rr73_again"].get(base_call(call), 0) < 3

def fresh_callers(now=None):
    """Queued callers still worth answering, oldest first. Drops anyone worked,
    held, or not heard for 6 periods (they have given up)."""
    now = now or time.time()
    for c in list(ENG["callers"]):
        if (is_worked(c["call"]) and not c.get("again")) or on_hold(c["call"]) or now - c["t"] > cyc(6):
            ENG["callers"].remove(c)
    return list(ENG["callers"])

def on_my_tx(msg):
    """Our own transmission (from ALL.TXT). Once we send RR73/73 to the target
    the QSO is complete on our side: release it two periods later unless they
    repeat (didn't copy), so the next caller is not kept waiting."""
    t = state["target"]
    to, frm, rest = addressing(msg)
    # (a free-text sign-off, 'KR4MZM GL 73', names no one: their RRR/RR73 marks those done)
    if t and rest and rest[-1] in ("RR73", "73") and same_call(to, t["call"]):
        t["done"] = True
        t["left"] = min(t["left"], 2)

def at(step, detail=""):
    """Record where the engine stopped this period (shown live in the UI's
    decision flow). Steps follow engine_tick's order; see STEPS in the UI."""
    ENG["step"] = {"step": step, "detail": detail, "t": time.time()}

def cq_skip(e, now=None):
    """Why a CQ won't be answered right now; '' means it is worth answering.
    The engine and the UI share this, so the badges match what it does."""
    now = now or time.time()
    if e.get("tag", "") not in ANSWERABLE:
        return f"CQ {e['tag']}"  # aimed at a region
    if is_worked(e["call"]):
        return "worked"
    if on_hold(e["call"]):
        return "on hold"
    if not dx_ok(e):
        return "DX only"  # CQ DX from our own continent
    if now - e["seen"] >= cyc(2) + 5:
        return "stale"
    if in_qso(e):
        return "in QSO"
    if e["snr"] < state["config"].get("min_snr", -20):
        return "weak"
    if not e.get("msg"):
        return "no decode"
    return ""

# Where each QSO came from (how it started, and the hunter's mode then), and
# how long the hunter ran in each mode: QSOs per hour by mode, measured
ORIGIN_FILE = os.path.join(DATA_DIR, "qso_origins.json")
ORIGIN = {"qsos": [], "secs": {}, "last": 0, "saved": 0, "recent": {},
          "on": [], "on_since": 0}  # on: [start, end, band, fmode] while the hunter runs; since: live tracking began
def hunt_mode():
    """The hunter's mode right now: contest, cq_first or hunt (standard automatic)."""
    return "contest" if manual_on() else "cq_first" if state["config"].get("cq_first") else "hunt"

def note_origin(call, why):
    """At acquire: how this QSO is starting. 'cq' = they answered our CQ."""
    now = time.time()
    if why == "called us":
        cq_on = now < ENG["cq_until"] or any(m["msg"].startswith("CQ ") and now - m["seen"] < 90
                                             for m in list(state["mytx"])[:4])
        how = "cq" if cq_on else "called us"
    else:
        how = ("again" if why.endswith("again") else "tail-end" if why == "tail-end"
               else "manual" if why == "manual" else "hunt")  # CQ / an activation tag: we answered their CQ
    ORIGIN["recent"][base_call(call)] = {"how": how, "why": why, "mode": hunt_mode(), "t": now}

def record_origin(call, band_, mode_):
    """A logged QSO: keep where it came from. Not started by the hunter in
    the last hour: worked by hand in WS."""
    now = time.time()
    r = ORIGIN["recent"].pop(base_call(call), None)
    if not r or now - r["t"] > 3600:
        r = {"how": "by hand", "why": "", "mode": hunt_mode() if state["running"] else "stopped"}
    tx = any(now - m["seen"] < 120 for m in list(state["mytx"])[:2])
    ORIGIN["qsos"].append({"call": call, "t": round(now), "band": band_ or band(), "fmode": mode_ or mode(),
                           "how": r["how"], "why": r["why"], "mode": r["mode"],
                           "on": bool(state["running"]), "tx": tx})  # hunter on / we transmitted in the last 2 min
    save_origins()

def tick_origins(now):
    """Time the hunter spends running in each mode, per UTC day."""
    dt, ORIGIN["last"] = now - ORIGIN["last"], now
    if state["running"] and 0 < dt < 60:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        d = ORIGIN["secs"].setdefault(day, {})
        d[hunt_mode()] = d.get(hunt_mode(), 0) + dt
        on, b, m = ORIGIN["on"], band(), mode()
        if b:
            if on and on[-1][2:] == [b, m] and now - on[-1][1] < 90:
                on[-1][1] = round(now)
            else:
                on.append([round(now - dt), round(now), b, m])
    if now - ORIGIN["saved"] > 300:
        save_origins()

def save_origins():
    ORIGIN["saved"] = time.time()
    try:
        with open(ORIGIN_FILE, "w") as f:
            json.dump({"qsos": ORIGIN["qsos"], "secs": {k: {m: round(v) for m, v in d.items()}
                                                       for k, d in ORIGIN["secs"].items()},
                       "on": ORIGIN["on"], "on_since": ORIGIN["on_since"]}, f)
    except OSError as e:
        log_err("QSO origins not saved", e)

def load_origins():
    try:
        d = json.load(open(ORIGIN_FILE))
        ORIGIN["qsos"], ORIGIN["secs"] = d.get("qsos", []), d.get("secs", {})
        ORIGIN["on"], ORIGIN["on_since"] = d.get("on", []), d.get("on_since", 0)
    except (OSError, ValueError):
        pass
    if not ORIGIN["on_since"]:
        ORIGIN["on_since"] = round(time.time())
        save_origins()

def origin_stats(day=None):
    """Today's QSOs by how they started and by hunter mode, with QSOs per hour."""
    day = day or time.strftime("%Y-%m-%d", time.gmtime())
    q = [x for x in ORIGIN["qsos"] if time.strftime("%Y-%m-%d", time.gmtime(x["t"])) == day]
    how, per = {}, {}
    for x in q:
        how[x["how"]] = how.get(x["how"], 0) + 1
        per[x["mode"]] = per.get(x["mode"], 0) + 1
    secs = ORIGIN["secs"].get(day, {})
    rate = {m: round(per.get(m, 0) / (secs[m] / 3600), 1) for m in secs if secs[m] >= 600}  # 10 min+ in that mode
    return {"how": how, "mode": per, "hours": {m: round(v / 3600, 2) for m, v in secs.items()}, "rate": rate}

# QSOs per hour on each band while the hunter was on. Before live tracking
# (on_since), "on" is guessed from ALL.TXT: runs of our Tx on one band and
# mode, each Tx within TX_RUN_GAP of the last (the hunter listens in between).
TX_RUN_GAP = 300
TXH = {"runs": [], "until": None}
def tx_runs(until):
    """Our transmissions in ALL.TXT before `until`, as [start, end, band, fmode] runs."""
    import calendar
    txs = []
    for p in ws_files("ALL.TXT"):
        if not os.path.exists(p):
            continue
        with open(p, errors="ignore") as f:
            for line in f:
                if " Tx " not in line:
                    continue
                w = line.split()
                try:
                    t = calendar.timegm(time.strptime(w[0], "%y%m%d_%H%M%S"))
                    b = band_of(float(w[1]) * 1e6)
                except (ValueError, IndexError):
                    continue
                if t < until and b and w[3] in MODES:  # off-band (a lost dial) doesn't count
                    txs.append((t, b, w[3]))
    runs = []
    for t, b, m in sorted(set(txs)):
        end = t + MODES[m][0]
        if runs and runs[-1][2:] == [b, m] and t - runs[-1][1] <= TX_RUN_GAP:
            runs[-1][1] = end
        else:
            runs.append([t, end, b, m])
    return runs

def band_rates():
    """{(band, fmode): {on_h, on_n, off_n, rate}}: hunter-on hours, QSOs logged
    while on and while off (by hand). The ALL.TXT part is read once."""
    if TXH["until"] != ORIGIN["on_since"]:
        TXH.update(runs=tx_runs(ORIGIN["on_since"]), until=ORIGIN["on_since"])
    by = {}
    for r in TXH["runs"] + ORIGIN["on"]:
        by.setdefault((r[2], r[3]), []).append(r)
    out = {}
    for k, runs in by.items():
        o = out.setdefault(k, {"on_s": 0, "on_n": 0, "off_n": 0})
        o["on_s"] += sum(e - s for s, e, *_ in runs)
    log_last()
    for t, b, m in LOG_LAST["qsos"]:
        o = out.setdefault((b, m), {"on_s": 0, "on_n": 0, "off_n": 0})
        on = any(s - 60 <= t <= e + 60 for s, e, *_ in by.get((b, m), ()))  # time_on is to the minute
        o["on_n" if on else "off_n"] += 1
    for o in out.values():
        o["on_h"] = round(o.pop("on_s") / 3600, 1)
        o["rate"] = round(o["on_n"] / o["on_h"], 1) if o["on_h"] >= 0.2 else None
    return out

def engine_tick():
    """Called once per T/R period. Priority: our target > callers-to-me >
    activator CQs > other CQs > tail-end > our own CQ."""
    now = time.time()
    tick_origins(now)
    wrl_check(now)
    desync_check(now)
    psk_tick(now)
    # refresh today's worked if a log file changed
    try:
        for p, mt in list(state.get("adi_mtime", {}).items()):
            if os.path.exists(p) and os.path.getmtime(p) != mt:
                state["worked"], state["adi_mtime"] = load_worked()
                break
    except Exception as e:
        log_err("ADIF reload", e)
    prune(now)
    lt = (tx_light(now) or {}).get("state")  # without ws-hunter: tell the operator when to Enable Tx
    if lt != ENG.get("light"):
        ENG["light"] = lt
        if lt in ("enable", "reenable", "mode"):
            log("TX light: " + tx_light(now)["msg"])
    if not state["running"]:
        return at("stopped")
    if ENG.get("tuning"):
        return at("tuning", ENG["tuning"])
    wait = new_band_wait(now)
    if wait > 0:
        return at("newband", f"listening on {band()}: {round(wait)} s more")
    if need_tune():  # only now, just before the first transmission on this band
        start_tune()
        return at("tuning", f"new band {band()}")
    t = state["target"]
    cfg = state["config"]
    if t and is_worked(t["call"]) and not t.get("again"):  # logged (seen in ADIF) without a QSO Logged packet
        log(f"{t['call']} is in the log, releasing target")
        state["target"] = t = None
    if t:
        t["left"] -= 1
        if t["left"] <= 0:
            waiting = [] if manual_on() else fresh_callers(now)  # manual: no one is next until picked
            if not (t.get("done") and waiting):
                halt_ws()  # with a caller waiting, the next Reply takes over TX instead
            if t.get("done"):
                add_worked(t["call"])
                log(f"QSO with {t['call']} complete")
            elif t.get("resp"):
                log(f"no answer from {t['call']} to {state['config'].get('qso_tries', 7)} replies, "
                    f"giving up, holding {hold(t['call'])}m")
            else:
                log(f"timeout, holding {t['call']} {hold(t['call'])}m")
                start_check(now, f"no answer from {t['call']}")
            state["target"] = None
        else:
            ENG["busy"] += 1
            # re-arm every cycle until they reply (suite treats Reply as one-shot);
            # once QSO progresses (resp), WS auto-sequences — don't touch it
            if not t.get("resp") and t.get("msg"):
                send_exact_reply(CTX["tx"], t["msg"])
            # WS answers a late RRR/73 from the previous station by itself,
            # even mid-QSO, switching its DX Call away: point it back
            dx = (state["udp"].get("dxcall") or "").strip()
            if t.get("resp") and not t.get("done") and t.get("last") and dx and not same_call(dx, t["call"]):
                if send_exact_reply(CTX["tx"], t["last"]):
                    log(f"WS switched to {dx} in the middle of our QSO with {t['call']}: back to {t['call']}")
            how = ("signing off" if t.get("done") else "in QSO") if t.get("resp") \
                else f"calling, {(t['left'] + 1) // 2} tries left"
            return at("target", f"{t['call']}: {how}")
    check_qrm(now)  # spectra wait for their decodes; we may transmit meanwhile
    if manual_on():
        # Contest rules (ARRL, WW Digi): each QSO started by the operator. The
        # hunter finishes the QSO they started; the next one waits for a tap.
        ENG["busy"] = 0
        n = len(suggestions(now))
        return at("manual", f"{n} to pick from" if n else "nothing to pick yet")
    # stations calling us, first-come-first-serve, working through all of them
    waiting = fresh_callers(now)
    while waiting:
        c = waiting.pop(0)
        ENG["callers"].remove(c)
        if c.get("again"):
            what = "73" if c["msg"].split()[-1] == "RRR" else "RR73"
            if acquire(c["call"], "", c["msg"], tries=1, why=f"{what} again"):
                state["target"]["again"] = True
                k = base_call(c["call"])
                ENG["rr73_again"][k] = ENG["rr73_again"].get(k, 0) + 1
                return at("caller", f"{c['call']} ({what} again)")
            continue
        if acquire(c["call"], "", c["msg"], tries=cfg.get("cq_tries", 3), why="called us"):
            ENG["busy"] += 1
            more = f" ({len(waiting)} more waiting)" if waiting else ""
            return at("caller", c["call"] + more)
    # Every check_every transmissions, between QSOs: one cycle without
    # transmitting, to hear whether anyone else has come up on frequency
    every = cfg.get("check_every", 10)
    if every and ENG["tx_n"] >= every and now >= ENG["check_until"]:
        start_check(now, f"{ENG['tx_n']} transmissions")
    if now < ENG["check_until"]:
        return at("check", f"{math.ceil((ENG['check_until'] - now) / period())} period(s) left")
    # Holds of every kind (busy frequency, listen pause, a station on hold)
    # never stop us answering a station that is calling us; they only stop
    # us starting something new.
    if now < ENG["qrm_until"]:
        return at("qrm", ENG["qrm_why"])  # someone else is using the frequency
    if now < ENG["idle_until"]:
        # listen-only rest
        return at("listen", f"{round(ENG['idle_until'] - now)} s left")
    # CQ callers: activations first (if enabled), then best chance of a
    # contact; fall through to the next if one can't be answered.
    # CQ first mode: we run the frequency. Only activators (when they come
    # first) are worth leaving our CQ for; other CQs and sign-offs are not.
    cq_first = cfg.get("cq_first", False)
    cands = [e for e in list(state["cq"].values()) if not cq_skip(e, now)
             and not (cq_first and not (e.get("special") and cfg.get("special_first", True)))]
    cands.sort(key=cq_rank)
    for e in cands:
        if acquire(e["call"], e["grid"], e["msg"], tries=cfg.get("cq_tries", 3),
                   why=e["tag"] if e.get("special") else "CQ", can_wait=True):
            ENG["busy"] += 1
            special = e.get("special") and cfg.get("special_first", True)
            return at("activator" if special else "cq", f"{e['call']} ({e['snr']:+d})")
        if time.time() < ENG["check_until"]:  # listening a cycle before moving our offset
            return at("check", "a full cycle before picking a Tx offset")
    if now < ENG["cq_until"]:
        # our own CQ keeps running; WS repeats it each period
        cq_check(now)
        return at("mycq", f"{round(ENG['cq_until'] - now)} s left")
    # no CQ: tail-end a station signing off, never one mid-QSO. A Reply to
    # "A B ..." makes WS call B, the sender. RR73/RRR usually comes from the
    # station running the frequency, who is about to be free; prefer those.
    best = ([] if cq_first else tail_cands(now))[:1]
    if best and acquire(best[0][1], "", best[0][0]["msg"], tries=cfg.get("tail_tries", 2), why="tail-end",
                        can_wait=True):
        ENG["busy"] += 1
        return at("tail", best[0][1])
    if time.time() < ENG["check_until"]:
        return at("check", "a full cycle before picking a Tx offset")
    ENG["busy"] = 0
    if ENG["cq_until"]:  # our CQ run just ended: stop and listen before the next
        ENG["cq_until"] = 0
        halt_ws()
        ENG["idle_until"] = now + cfg.get("idle_secs", 60)
        log(f"CQ run over, listening {cfg.get('idle_secs', 60)}s")
        return at("cq_end", f"listening {cfg.get('idle_secs', 60)} s")
    if cq_first or cfg.get("call_cq", True):  # last resort (or first): call CQ ourselves
        start_cq(cfg.get("cq_secs", 120))
        if time.time() < ENG["check_until"]:
            return at("check", "a full cycle before picking a Tx offset")
        return at("cq_start", f"{cfg.get('cq_secs', 120)} s run")
    # spare cycle: every idle_every idle cycles, rest to catch the off sequence
    ENG["idle_n"] += 1
    if ENG["idle_n"] % max(1, cfg.get("idle_every", 4)) == 0:
        ENG["idle_until"] = now + cfg.get("idle_secs", 60)
        log(f"listening {cfg.get('idle_secs', 60)}s (off-sequence watch)")
    return at("idle", "CQ calling is off")

def tail_cands(now):
    """Stations signing off in the last two periods, best first: (decode, call)."""
    out = []
    for m in list(state["msgs"]):
        if now - m.get("seen", 0) >= cyc(2):
            continue
        to, frm, rest = addressing(m["msg"])
        if not frm or "." in frm or is_me(to) or is_me(frm):
            continue
        if not rest or rest[-1] not in SIGNOFF:
            continue
        if is_worked(frm) or on_hold(frm) or m["snr"] < state["config"].get("min_snr", -20):
            continue
        out.append(((rest[-1] == "73", -m["snr"]), m, frm))
    out.sort(key=lambda x: x[0])
    return [(m, frm) for _, m, frm in out]

def suggestions(now=None):
    """Manual (contest) mode: what the hunter would work next, in its own
    order (callers, activators, best CQs, tail-ends). Nothing starts until the
    operator taps one; see /api/work."""
    now = now or time.time()
    cfg, t = state["config"], state["target"]
    out, seen = [], {base_call(t["call"])} if t else set()
    def add(call, msg, why, tries):
        p = state["decparams"].get(msg)
        if p and base_call(call) not in seen:
            seen.add(base_call(call))
            out.append({"call": call, "msg": msg, "why": why, "snr": p["snr"], "tries": tries})
    for c in fresh_callers(now):
        if c.get("again"):
            add(c["call"], c["msg"], ("73" if c["msg"].split()[-1] == "RRR" else "RR73") + " again", 1)
        else:
            add(c["call"], c["msg"], "called us", cfg.get("cq_tries", 3))
    for e in sorted([e for e in list(state["cq"].values()) if not cq_skip(e, now)], key=cq_rank):
        add(e["call"], e["msg"], e["tag"] if e.get("special") else "CQ", cfg.get("cq_tries", 3))
    for m, frm in tail_cands(now):
        add(frm, m["msg"], "tail-end", cfg.get("tail_tries", 2))
    return out[:12]

def start_check(now, why):
    """Stop transmitting for one full cycle (an even and an odd period), so
    both periods are heard: the busy-frequency check and the offset picker
    never see the periods we transmit in (the rig mutes its receive audio)."""
    halt_ws()
    ENG["cq_until"] = 0  # a CQ run starts afresh after, on a fresh clear offset
    ENG["check_until"] = now + cyc(2) - 1  # engine ticks at period starts: this one and the next
    ENG["tx_n"] = 0
    log(f"listening one cycle for interference ({why})")

def cq_check(now):
    """During our CQ run, make sure WS really is set to call CQ: Enable Tx on
    and (ClearDX builds) the CQ message selected. Re-select twice at most."""
    u = state["udp"]
    if not has_ws_hunter() or now - ENG.get("cq_start", now) < 4 or now - u.get("tx_t", 0) > 20:
        return
    msg = u.get("tx_msg", "").strip()
    bad = not u.get("tx_enabled") or (has_ws_hunter("261013") and msg and not msg.startswith("CQ"))
    if not bad:
        return
    if ENG.get("cq_fix", 0) >= 2:
        if ENG["cq_fix"] == 2:
            ENG["cq_fix"] = 3
            log(f"WS still not calling CQ (Enable Tx {'on' if u.get('tx_enabled') else 'off'}, next: {msg or '?'})")
        return
    ENG["cq_fix"] = ENG.get("cq_fix", 0) + 1
    ENG["cq_start"] = now
    log(f"WS not set to call CQ (Enable Tx {'on' if u.get('tx_enabled') else 'off'}, next: {msg or '?'}); re-selecting")
    select_cq()
    enable_tx()

def start_cq(secs):
    """Reset WS's message stack to its default CQ and enable Tx. Halt first so
    Enable Tx is known to be off: on stock WS the fallback is a toggle click."""
    halt_ws()
    try:
        # CQ on whichever period has fewer stations heard recently
        now = time.time()
        busy = [sum(1 for o in list(ENG["occ"]) if o["par"] == p and now - o["t"] < 120) for p in (0, 1)]
        par = 0 if busy[0] <= busy[1] else 1
        if has_ws_hunter():
            CTX["tx"].sendto(pack_set_bool(SET_TX_PERIOD, state["ws_id"], par == 0), ws_dest())
        if not tune_tx(par, can_wait=True):
            return  # listening a cycle before choosing an offset
        select_cq()
        if not has_ws_hunter():
            time.sleep(1.0)  # let WS act on Halt + Configure before clicking
        how = enable_tx()
    except Exception as e:
        log(f"CQ start failed: {e}"); return
    ENG["cq_until"] = time.time() + secs
    ENG["cq_start"], ENG["cq_fix"] = time.time(), 0
    log(f"calling CQ for {secs}s ({how})")

def pileup(call, now=None):
    """Stations heard calling `call` in the last two periods (not counting us)."""
    now = now or time.time()
    return sum(1 for c, ts in list(ENG["calls_to"].get(base_call(call), {}).items())
               if now - ts < cyc(2) and not is_me(c))

def in_qso(e):
    """They have sent to someone since their last CQ."""
    return ENG["active"].get(base_call(e["call"]), 0) > e["seen"]

def cq_score(e, now=None):
    """Chance of a contact: their SNR, less 3 dB per station already calling
    them, plus 3 if heard in the last period (still on frequency)."""
    now = now or time.time()
    return e["snr"] - 3 * pileup(e["call"], now) + (3 if now - e["seen"] < cyc(1) + 1 else 0)

def cq_rank(e):
    special = state["config"].get("special_first", True) and e.get("special", False)
    return (not special, -cq_score(e), e["km"] is None, e["km"] or 0)

def acquire(call, grid, msg, tries, why, can_wait=False):
    """Call them. can_wait: if our offset needs to move and we haven't heard
    a full cycle, listen one first and return False (starting nothing)."""
    p = state["decparams"].get(msg)
    extra, dx = 0, None
    if p:
        tx_par = 1 - parity(p["t_ms"])  # we answer in the other period
        dx = {"call": call, "ll": (grid and GRID_RE.match(grid.upper()) and grid_to_latlon(grid)) or locate(call),
              "df": p["df"], "new": why != "called us"}  # someone calling us knows where we are
        if not tune_tx(tx_par, can_wait, dx):
            return False
        # Starting in their period, our first call goes out a period later;
        # allow for it so their answer to our last call is decoded before we
        # give up on them.
        extra = 1 if parity(tod_ms(time.time())) != tx_par else 0
    if not send_exact_reply(CTX["tx"], msg):
        return False
    # `left` counts T/R periods; we transmit in every other one
    note_origin(call, why)
    state["target"] = {"call": call, "grid": grid, "left": tries * 2 + extra, "why": why,
                       "msg": msg, "resp": False, "signoff": signoff_text()}
    push_signoff(state["target"])
    ENG["cq_until"] = 0  # a Reply replaces our CQ in WS
    state["stats"]["calls_made"] += 1
    df, par = ENG.pop("picked", (None, None)) if p else (None, None)
    note = remote_note(df, par, time.time(), dx) if df is not None else ""
    log(f"calling {call} {grid} ({why}, {tries} tries)" + (f" at {df} Hz{note}" if note else ""))
    return True

def send_exact_reply(tx, msg):
    """Send Reply with the decode's exact params (required by WS filter),
    addressed to WSJT-X's ephemeral port (it never binds 2237 itself)."""
    p = state["decparams"].get(msg)
    if not p:
        log(f"no decode params for: {msg}"); return False
    tx.sendto(pack_reply(state["ws_id"], p["t_ms"], p["snr"], p["dt"], p["df"], msg,
                       p.get("mode", "~")),
              ws_dest())
    return True

def handle_decode(msg, snr, tx, myll, seen, cycle, t_ms=None):
        now = time.time()
        t = state["target"]  # local ref: the engine thread may clear state["target"]
        tgt = (t or {}).get("call", "")
        to, frm, rest = addressing(msg)
        to_me = is_me(to)
        again = False
        # even/odd sequence: decode time falls just after the 15s TX slot
        if t_ms is not None:
            ENG["parity"][parity(t_ms)] += 1
        state["msgs"].appendleft({"msg": msg, "snr": snr, "t": time.strftime("%H:%M:%S"),
            "seen": now, "tome": to_me,
            "dx": bool(tgt) and (same_call(to, tgt) or same_call(frm, tgt))})
        m = CQ_RE.match(msg)
        if m:
            tag = (m.group(1) or "").upper()
            call, grid = m.group(2).upper(), (m.group(3) or "").upper()
            dist = km(myll, grid_to_latlon(grid)) if grid else None
            note_grid(call, grid)
            state["cq"][call] = {"call": call, "grid": grid, "snr": snr,
                "tag": tag, "special": tag in SPECIAL,
                "km": dist, "worked": is_worked(call), "msg": msg,
                "seen": now, "t": time.strftime("%H:%M:%S"),
                "hold": on_hold(call),
                "mode": state["decparams"].get(msg, {}).get("mode", "?")}
            ENG["active"][base_call(call)] = 0  # calling CQ again: free
            state["stats"]["cq_heard"] += 1
            return  # selection happens in engine_tick (priority order)
        signoff = bool(rest) and rest[-1] in SIGNOFF
        if frm and rest:
            note_grid(frm, rest[-1])
        if to and frm and "." not in to + frm:  # every exchange, both ends, for the globe
            PAIRS[frozenset((base_call(to), base_call(frm)))] = {"a": frm, "b": to, "t": now}
        if frm and "." not in frm and not is_me(frm):
            fb = base_call(frm)
            ENG["active"][fb] = 0 if signoff else now
            if to and not signoff:
                ENG["calls_to"].setdefault(base_call(to), {})[fb] = now
            if t and same_call(frm, tgt) and not to_me and not t.get("resp"):
                # they picked someone else: stop calling now (no hold; we will
                # see their next CQ or sign-off)
                halt_ws()
                log(f"{tgt} is working {to}; standing by")
                state["target"] = None
        if to_me and frm and "." not in frm:
            if t and same_call(frm, tgt):
                # our target answered; WS auto-sequences from here. After their
                # sign-off we only owe a 73, so release the target soon after.
                if not t.get("resp"):
                    RIG_WAKE.set()  # QSO power before our next transmission
                if rest and rest[-1] == "RRR" and t.get("done") and t.get("rrr_n", 0) < 3:
                    # RRR again: they never copied our 73. WS stops TX after
                    # sending a 73, so answer it again ourselves.
                    t["rrr_n"] = t.get("rrr_n", 0) + 1
                    if send_exact_reply(CTX["tx"], msg):
                        log(f"{frm} sent RRR again, sending our 73 again")
                t["resp"] = True
                t["last"] = msg
                t["done"] = t.get("done") or signoff
                push_signoff(t)
                # each reply buys qso_tries more of our transmissions (+1 period
                # to decode their answer to the last); silence after that: give up
                t["left"] = 2 if signoff else state["config"].get("qso_tries", 7) * 2 + 1
                log(f"reply: {msg}")
            elif state["running"] and ((not signoff and not is_worked(frm)) or (again := rr73_again(frm, rest, now))):
                # A hold stops us calling them, not them answering us: a reply
                # from a held station is a late answer to our call. Lift the
                # hold and work them next.
                late = on_hold(frm)
                if late:
                    unhold(frm)
                # someone calling us: queue first-come-first-serve; a repeat call
                # keeps their place but refreshes the message we will answer
                q = next((c for c in list(ENG["callers"]) if same_call(c["call"], frm)), None)
                if q:
                    q.update(msg=msg, t=now)
                else:
                    entry = {"call": frm, "msg": msg, "t": now, "again": again}
                    if again:
                        ENG["callers"].appendleft(entry)
                        log(f"{frm} didn't copy our {'73' if rest[-1] == 'RRR' else 'RR73'}, "
                            + ("tap to send it again: " if manual_on() else "sending it again: ")
                            + msg)
                    elif late:
                        ENG["callers"].appendleft(entry)
                        log(f"late reply, answering next: {msg}")
                    else:
                        ENG["callers"].append(entry)
                        log(f"caller queued: {msg}")
                    if t and not t.get("resp") and not manual_on():  # never abandon a QSO in progress
                        halt_ws()
                        log(f"dropping {tgt} for caller {frm}")
                        state["target"] = None
        state["answering"][msg] = {"msg": msg, "snr": snr, "tome": to_me,
            "from": frm or msg, "seen": now, "t": time.strftime("%H:%M:%S")}

# Radio via FLRig's XML-RPC server: TX power control, PTT and S-meter.
# One multicall per poll (FLRig answers in ~0.5 s, serialised with its CAT polling).
RIG = {"ok": False, "err": ""}
RIG_WAKE = threading.Event()  # poll now: e.g. the power for this QSO stage changed
MAX_W = {"Xiegu-G90": 20, "Xiegu-X6100": 10, "IC-705": 10, "FT-818": 6, "FT-817": 5}  # rig.get_maxpwr often says 0

def flrig():
    import xmlrpc.client
    class T(xmlrpc.client.Transport):
        def make_connection(self, host):
            c = super().make_connection(host); c.timeout = 3; return c
    return xmlrpc.client.ServerProxy(state["config"]["flrig_url"], transport=T())  # one per call: not thread-safe

# FLRig's get_power reads 0 on the G90, so its power setting is read with a
# raw CI-V 14 0A instead. The G90 answers on its own scale (not BCD 0-255),
# measured for each setting; 1 and 2 W both read 0000, 10 and 11 W 0128.
CIV = {"Xiegu-G90": (0x70, {1: 0x000, 2: 0x000, 3: 0x01D, 4: 0x02A, 5: 0x048, 6: 0x055, 7: 0x063,
                            8: 0x080, 9: 0x09D, 10: 0x128, 11: 0x128, 12: 0x136, 13: 0x143,
                            14: 0x161, 15: 0x17E, 16: 0x18B, 17: 0x209, 18: 0x216, 19: 0x224,
                            20: 0x25F})}

def civ_watts(raw, table):
    """Watts for a raw G90 power value: the nearest measured setting."""
    return min(table, key=lambda w: (abs(table[w] - raw), w))

def civ_power(fl):
    """The rig's TX power setting in W, asked over raw CI-V; None if this rig
    isn't in CIV or it didn't answer (it can't while transmitting)."""
    if RIG.get("xcvr") not in CIV:
        return None
    addr, table = CIV[RIG["xcvr"]]
    for _ in range(3):  # often garbled: it collides with FLRig's own polling
        rep = str(fl.rig.cat_string(f"xFE xFE x{addr:02X} xE0 x14 x0A xFD"))
        m = re.search(rf"FE FE E0 {addr:02X} 14 0A ([0-9A-F]{{2}}) ([0-9A-F]{{2}}) FD", rep)
        if m:
            return civ_watts(int(m.group(1) + m.group(2), 16), table)
        time.sleep(0.3)
    return None

def same_watts(a, b):
    """Two power settings the rig can tell apart? (G90: 1 = 2 W, 10 = 11 W)"""
    table = CIV.get(RIG.get("xcvr"), (0, {}))[1]
    return a == b or (a in table and b in table and table[a] == table[b])

def answered():
    """Has the station we are working answered us (or called us first)?"""
    t = state["target"]
    return bool(t and (t.get("resp") or t.get("why") == "called us"))

def want_watts():
    """Low power while calling, more once someone answers: stations that hear
    us at all get a stronger signal for the rest of the QSO."""
    c = state["config"]
    return c.get("watts_qso" if answered() else "watts_call")

def hold_power(fl, now):
    """Keep the radio at the power for this stage of the QSO: set it when the
    stage changes, read its real setting every few seconds while receiving,
    and set it back (logged) if something else changed it."""
    want = want_watts()
    if want and want != RIG.get("want"):
        # going up can't wait for our transmission to end (it is for that
        # transmission); going down can
        if not RIG.get("ptt") or want > (RIG.get("want") or 0):
            fl.rig.set_power(want)
            log(f"TX power {want} W ({'in QSO' if answered() else 'calling'})")
            RIG.update(want=want, power=want, pw_t=now, pw_off=0)
        return
    if RIG.get("ptt") or now - RIG.get("pw_t", 0) < 6:
        return
    w = civ_power(fl)
    if w is None:
        return
    RIG.update(power=w, pw_t=now)
    if not want or same_watts(w, want):
        RIG["pw_off"] = 0
        return
    RIG["pw_off"] = RIG.get("pw_off", 0) + 1
    RIG["pw_t"] = now - 4  # confirm on the next poll, not in 6 s
    if RIG["pw_off"] >= 2:  # twice in a row: not a read caught mid-change
        fl.rig.set_power(want)
        RIG.update(power=want, pw_off=0, pw_t=0)
        log(f"radio TX power went to {w} W (not set here): set back to {want} W")

# --- SWR, relative. The G90 answers CI-V 15 12 (the Icom SWR meter) with a
# 16-bit value that grows with the power setting and tops out at 0x25F; FLRig's
# get_swrmeter reads it onto Icom's scale (0-100, rising with it: 0x104 -> 40,
# 0x14C -> 62, from about 0x237 up -> 100). A raw CI-V query can't be used: in
# TX the G90 answers after FLRig's cat_string has given up. Its mapping to SWR
# is unknown (the radio shows SWR only as a bar), so each transmission's
# reading is logged and a new one is compared with the usual reading for that
# band and power.
SWR_LEARN = 3     # transmissions on a band and power before "usual" means anything
SWR_TOP = 100     # top of FLRig's scale
SWR_FILE = os.path.join(DATA_DIR, "swr_log.json")
SWR = {"on": None, "cur": [], "log": deque(maxlen=500)}

def swr_poll(ptt, swr, now):
    """SWR meter readings while transmitting; one record per transmission."""
    if RIG.get("xcvr") not in CIV:
        return
    if ptt:
        if not SWR["on"]:
            SWR.update(on=now, cur=[], what="tune" if ENG.get("tuning") else mode_raw())
        SWR["cur"].append(swr)
        RIG["swr_raw"] = swr if swr and swr > 0 else None
        return
    if not SWR["on"]:
        return
    # 0 = read just as PTT came up or dropped; -1 = FLRig got no reading (G90 at 20 W)
    vals = [v for v in SWR["cur"] if v and v > 0]
    if vals and now - SWR["on"] >= 3:
        SWR["log"].append({"id": int(SWR["on"]), "t": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(SWR["on"])),
                           "secs": round(now - SWR["on"]), "band": band(), "khz": (state["udp"].get("dial") or 0) / 1000,
                           "mode": SWR["what"], "watts": RIG.get("power"), "raw": sorted(vals)[len(vals) // 2],
                           "raws": vals})
        save_swr()
    SWR.update(on=None, cur=[])
    RIG["swr_raw"] = None

def save_swr():
    try:
        with open(SWR_FILE, "w") as f:
            json.dump(list(SWR["log"]), f, indent=0)
    except OSError as e:
        log_err("SWR log", e)

def load_swr():
    try:
        SWR["log"].extend(r for r in json.load(open(SWR_FILE)) if (r.get("raw") or 0) > 0)  # -1: no reading
    except (OSError, ValueError):
        pass

def swr_usual(b, watts, skip=None):
    """Median raw reading of past transmissions on band b at this power
    (antenna tunes left out), and how many there were."""
    vals = sorted(r["raw"] for r in list(SWR["log"])
                  if r["band"] == b and r["watts"] == watts and r["mode"] != "tune" and r["id"] != skip)
    return (vals[len(vals) // 2] if vals else None), len(vals)

def swr_judge(raw, b, watts, skip=None):
    usual, n = swr_usual(b, watts, skip)
    if raw is None or n < SWR_LEARN or not usual:
        return {"usual": usual, "n": n, "level": "learning"}
    if usual >= SWR_TOP:  # the meter is at its top at this power: nothing to compare
        return {"usual": usual, "n": n, "level": "off scale"}
    x = raw / usual
    return {"usual": usual, "n": n, "x": round(x, 2),
            "level": "much higher" if x >= state["config"]["swr_bad"] else "higher" if x >= state["config"]["swr_high"] else "normal"}

def swr_state():
    """The reading on the air now and the last transmission's, each against
    the usual for that band and power."""
    if RIG.get("xcvr") not in CIV:
        return None
    last = SWR["log"][-1] if SWR["log"] else None
    live = RIG.get("swr_raw")
    return {"live": live, "live_vs": swr_judge(live, band(), RIG.get("power")) if live else None,
            "last": last and {k: v for k, v in last.items() if k not in ("raws", "radio_swr")}
            | {"vs": swr_judge(last["raw"], last["band"], last["watts"], skip=last["id"])}}

def rig_poll():
    import xmlrpc.client
    while True:
        try:
            if not RIG["ok"]:
                fl = flrig()
                RIG["xcvr"] = str(fl.rig.get_xcvr())
                RIG["max"] = int(float(fl.rig.get_maxpwr() or 0)) or MAX_W.get(RIG["xcvr"], 100)
            m = xmlrpc.client.MultiCall(flrig())
            m.rig.get_ptt(); m.rig.get_power(); m.rig.get_smeter(); m.rig.get_pwrmeter(); m.rig.get_swrmeter()
            m.rig.get_vfo()
            ptt, power, sm, out, swr, vfo = list(m())
            # (the G90's power meter is coarse, full or nothing, but shows we put out power)
            ptt = bool(int(ptt))
            RIG.update(ok=True, err="", t=time.time(), ptt=ptt, s=float(sm), vfo=int(float(vfo or 0)), fail_since=0,
                       out=max(0.0, float(out or 0)) if ptt else 0.0)
            if RIG.get("xcvr") not in CIV:
                RIG["power"] = int(float(power))
            swr_poll(ptt, int(float(swr or 0)), time.time())
            hold_power(flrig(), time.time())
            if float(sm) > 0:
                RIG["has_s"] = True  # some rigs (the G90) never report an S-meter over CAT
        except Exception as e:
            if RIG["ok"] or not RIG["err"]:
                log(f"FLRig: {e}")
            RIG.update(ok=False, err=str(e)[:80])
            # it was answering before, or it's there but stuck (a timeout, not a refused connection)
            if (RIG.get("xcvr") or "timed out" in str(e)) and not RIG.get("fail_since"):
                RIG["fail_since"] = time.time()
        RIG_WAKE.wait(1 if RIG.get("ptt") else 2)
        RIG_WAKE.clear()

def rx_level(now):
    """Incoming signal, from WS: the strongest decode of the last period."""
    snrs = [m["snr"] for m in list(state["msgs"]) if now - m["seen"] < cyc(1) + 3]
    return {"best": max(snrs) if snrs else None, "n": len(snrs)}

def manual_target(now):
    """Operating WS by hand (no hunter target): its DX Call is the target,
    while Enable Tx is on or for a minute after our last transmission, so a
    finished QSO's DX Call left in WS doesn't stay on the globe. A QSO once
    they have sent us anything in the last two minutes."""
    u = state["udp"]
    dx = (u.get("dxcall") or "").strip()
    iv = ENG["tx_iv"]
    last_tx = (iv[-1][1] or now) if iv else 0
    if not dx or not (u.get("tx_enabled") or now - last_tx < 60):
        return None
    p = PAIRS.get(frozenset((base_call(dx), base_call(state["mycall"]))))
    resp = bool(p) and same_call(p["a"], dx) and now - p["t"] < 120
    return {"call": dx, "ll": locate(dx), "resp": resp}

def lan_urls():
    """This page's address on the local network: one per LAN interface, the
    one with the default route first (a UDP connect sends nothing)."""
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET: never reached
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    port = state["config"]["http_port"]
    seen = []
    for ip in ips:
        if ip not in seen and not ip.startswith(("127.", "169.254.")):
            seen.append(ip)
    return {"urls": [f"http://{ip}:{port}/" for ip in seen], "host": socket.gethostname()}

def globe():
    """Positions for the globe: me, today's contacts, the target, and recent
    traffic between other stations (both ends located)."""
    now, here = time.time(), (band(), mode())
    me = grid_to_latlon(state["mygrid"]) if state["mygrid"] else locate(state["mycall"])
    seen, worked = set(), []
    for k in [k for k, ts in list(RECENT.items()) if now - ts > 86400]:
        RECENT.pop(k, None)
    for (c, b, m), ts in sorted(list({**RECENT, **state["worked"]}.items()), key=lambda kv: -(kv[1] or 0)):
        ll = locate(c)
        if ll and (c, b, m) not in seen:
            seen.add((c, b, m))
            worked.append({"call": c, "ll": ll, "band": b, "mode": m, "here": (b, m) == here})
    t = state["target"]
    # a QSO for the globe once they have answered, or from the start when they
    # called us: either way they are working us directly
    tgt = {"call": t["call"], "ll": locate(t["call"]),
           "resp": bool(t.get("resp")) or t.get("why") == "called us"} if t else manual_target(now)
    # Others answering us while we work someone: the caller queue, without
    # pruning it (that is the engine's job), shown only when more than one
    # station is answering us at once.
    waiting = [c for c in list(ENG["callers"]) if now - c["t"] <= cyc(6)
               and not (t and same_call(c["call"], t["call"])) and not is_worked(c["call"])]
    answering = len(waiting) + (1 if tgt and tgt["resp"] else 0)
    callers = [{"call": c["call"], "ll": ll, "age": round(now - c["t"])}
               for c in waiting for ll in [locate(c["call"])] if ll] if answering >= 2 else []
    traffic = []
    for p in sorted(list(PAIRS.values()), key=lambda p: -p["t"]):
        age = now - p["t"]
        if age > state["config"]["traffic_secs"] or len(traffic) >= 150:
            continue
        a, b = (me if is_me(c) else locate(c) for c in (p["a"], p["b"]))
        if a and b and a != b:
            traffic.append({"from": p["a"], "to": p["b"], "a": a, "b": b, "age": round(age),
                            "tome": is_me(p["b"])})
    return {"me": me, "mycall": state["mycall"], "worked": worked[:300], "target": tgt, "callers": callers,
            "traffic": traffic, "fade": state["config"]["traffic_secs"], "band": here[0], "mode": here[1],
            "remote": remote_view(now)}

# --- Band scan: visit the published FT8/FT4/FT2 dial frequencies and take
# one clean ws-hunter spectrum on each, to see which bands are busy. WS runs
# FT2 meanwhile (3.75 s periods, so a spectrum every 3.75 s); the period in
# which the rig retuned mixes two bands and is thrown away.
SET_DIAL = 20
SCAN_FILE = os.path.join(DATA_DIR, "scan_results.json")
FREQ_FILE = os.path.join(DATA_DIR, "scan_freqs.json")
# WS's default frequency list (models/FrequencyList.cpp), downloaded so new or
# moved FT2 frequencies arrive without a hunter update; the fork is the fallback
FREQ_URLS = ["https://raw.githubusercontent.com/wa1hco/ws-suite-inhibit/HEAD/models/FrequencyList.cpp",
             "https://raw.githubusercontent.com/TheBengineer/ws-hunter/hunter/models/FrequencyList.cpp"]
FREQ_RE = re.compile(r"\{\s*(\d+)\s*,\s*Modes::(FT8|FT4|FT2)\s*,\s*IARURegions::(\w+)\s*,[^}]*?(true|false)\s*\}")
# (kHz, mode) as in that list for region 2, HF only, if it can't be fetched
FREQS_BUILTIN = [(1840, "FT8"), (1843, "FT2"), (3573, "FT8"), (3575, "FT4"), (3578, "FT2"),
                 (5357, "FT8"), (5357, "FT4"), (5357, "FT2"), (7074, "FT8"), (7047.5, "FT4"), (7052, "FT2"),
                 (10136, "FT8"), (10140, "FT4"), (10144, "FT2"), (14074, "FT8"), (14080, "FT4"), (14084, "FT2"),
                 (18100, "FT8"), (18104, "FT4"), (18108, "FT2"), (21074, "FT8"), (21140, "FT4"), (21144, "FT2"),
                 (24915, "FT8"), (24919, "FT4"), (24923, "FT2"), (28074, "FT8"), (28180, "FT4"), (28184, "FT2")]
SCAN_SETTLE = 0.6  # s after the dial command before a period counts as clean
SCAN = {"on": False, "stop": False, "goto": None, "i": 0, "n": 0, "cur": None, "t0": 0, "err": "",
        "q": None, "by_tm": {}, "rows": {}, "freqs": {"src": "built-in", "t": 0, "list": FREQS_BUILTIN}}

def ws_region():
    """IARU region set in WS (R1, R2, R3), default R2."""
    try:
        m = re.search(r"^Region=.*?(R[123])", open(os.path.expanduser("~/.config/WS.ini"), errors="ignore").read(), re.M)
        return m.group(1) if m else "R2"
    except OSError:
        return "R2"

def parse_freq_list(src, region):
    """FT8/FT4/FT2 dial frequencies in WS's FrequencyList.cpp, one per band
    and mode: a preferred entry for all regions or ours first; another
    region's when that is all there is (80m FT2 is listed for R3 only)."""
    best = {}
    for hz, md, reg, pref in FREQ_RE.findall(src):
        hz = int(hz)
        b = band_of(hz)
        if not b or hz > 30e6:  # HF: what the G90 and most portable rigs cover
            continue
        rank = (pref == "true", reg in ("ALL", region))
        if (b, md) not in best or rank > best[(b, md)][0]:
            best[(b, md)] = (rank, hz)
    return sorted((hz / 1000, md) for (b, md), (rank, hz) in best.items())

def load_freqs(fetch=True):
    """Scan frequencies: the cached download, refreshed once a day."""
    try:
        c = json.load(open(FREQ_FILE))
        SCAN["freqs"] = {"src": c["src"], "t": c["t"], "list": [tuple(x) for x in c["list"]]}
    except (OSError, ValueError, KeyError):
        pass
    if not fetch or time.time() - SCAN["freqs"]["t"] < 86400:
        return
    import urllib.request
    for url in FREQ_URLS:
        try:
            src = urllib.request.urlopen(url, timeout=10).read().decode("latin-1")
            fl = parse_freq_list(src, ws_region())
            if sum(1 for _, m in fl if m == "FT2") < 5:
                raise ValueError("no FT2 frequencies in it")
            SCAN["freqs"] = {"src": url.split("/")[3] + " " + time.strftime("%Y-%m-%d", time.gmtime()),
                             "t": time.time(), "list": fl}
            with open(FREQ_FILE, "w") as f:
                json.dump(SCAN["freqs"], f)
            log(f"scan frequencies: {len(fl)} from {url.split('/')[3]}")
            return
        except Exception as e:
            log(f"scan frequencies from {url.split('/')[3]}: {str(e)[:60]}")

def scan_points(bands, modes):
    """One stop per dial frequency (60m's 5357 kHz serves all three modes)."""
    pts = {}
    for khz, md in SCAN["freqs"]["list"]:
        b = band_of(khz * 1000)
        if b in bands and md in modes:
            pts.setdefault(round(khz * 1000), {"hz": round(khz * 1000), "band": b, "modes": []})["modes"].append(md)
    return [pts[k] for k in sorted(pts)]

def parse_spectrum(data, off):
    """ws-hunter SpectrumSummary -> (period start ms, T/R ms, start Hz, bin Hz, dB per bin)."""
    t_ms, tr_ms, start, bin_hz = struct.unpack(">IIII", data[off:off+16])
    floor, step = struct.unpack(">dd", data[off+16:off+32])
    n = struct.unpack(">I", data[off+32:off+36])[0]
    return t_ms, tr_ms, start, bin_hz, [floor + v * step for v in data[off+36:off+36+n]]

def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0.0

SIG_DB, SIG_PEAK_DB, CARRIER_DB = 2.0, 4.0, 6.0  # see scan_metrics
def scan_metrics(db, start, bin_hz, sig_bw=50):
    """Rough band quality from one averaged spectrum. Each bin is compared
    with the local quiet level (the 25th percentile within +-150 Hz), which
    follows the receiver's passband and broad humps but not a 50-170 Hz
    wide FT signal. Runs of bins 2+ dB above it, 30-300 Hz wide and 4+ dB
    at the peak, with about the same level on both sides, are FT-like
    signals (the edge of a broad lump is up on one side only); 1-2 bins
    6+ dB up are carriers (birdies, CW). Wideband interference shows
    instead as a raised noise floor and a lumpy quiet level, judged across
    the scan (scan_rate); the quiet level's shape over 300-2700 Hz is kept
    for that."""
    f = [start + i * bin_hz for i in range(len(db))]
    mid = [i for i, x in enumerate(f) if 300 <= x < 2700]
    if len(mid) < 30:
        return None
    w = max(3, 150 // bin_hz)
    base = [pct(db[max(0, i - w):i + w + 1], .25) for i in range(len(db))]
    ex = [d - b for d, b in zip(db, base)]
    sigs, carriers, run = [], [], []
    def close(run):
        if not run:
            return
        width, peak = len(run) * bin_hz, max(ex[i] for i in run)
        # the quiet level over 300 Hz each side: a signal has noise on both
        # sides (a neighbour doesn't fill 300 Hz), the edge of a broad lump doesn't
        k = max(3, 300 // bin_hz)
        side = [pct(db[max(0, run[0] - k - 2):max(0, run[0] - 2)] or [db[run[0]]], .25),
                pct(db[run[-1] + 3:run[-1] + k + 3] or [db[run[-1]]], .25)]
        if abs(side[0] - side[1]) >= max(3.0, peak / 2):
            return
        if 30 <= width <= 300 and peak >= SIG_PEAK_DB:
            sigs.append((f[run[0]], f[run[-1]] + bin_hz, round(peak, 1),
                         max(1, round(width / (sig_bw + 20)))))  # stations side by side
        elif width < 30 and peak >= CARRIER_DB:
            carriers.append((f[run[0]], f[run[-1]] + bin_hz, round(peak, 1)))
    for i, x in enumerate(f):
        if 200 <= x < 2900 and ex[i] >= SIG_DB:
            run.append(i)
        else:
            close(run); run = []
    close(run)
    floor = pct([db[i] for i in mid], .2)
    n = sum(s[3] for s in sigs)
    return {"floor": round(floor, 1), "_shape": [round(base[i] - floor, 1) for i in mid], "signals": n,
            "strength": round(pct([s[2] for s in sigs], .5), 1) if sigs else None,
            "busy": round(sum(s[1] - s[0] for s in sigs) / 2400, 2),  # share of the passband with signals
            "sigs": [s[:3] for s in sigs], "carriers": carriers,
            # dB over the quietest tenth, 0.5 dB steps, 200-3000 Hz: the row's little chart
            "lv": bytes(max(0, min(255, round(2 * (d - floor)))) for d, x in zip(db, f) if 200 <= x < 3000).hex(),
            "lv_start": max(200, start), "bin": bin_hz}

QRM_HUMP_DB, NOISY_DB, WEAK_SIG_DB = 4.0, 10.0, 6.0
def scan_rate(rows, known):
    """Label one scan's rows busy / active / quiet / QRM / noisy.
    QRM is wideband interference: a lump in the quiet level. The receiver's
    own passband shape is the same on every band, so the median quiet-level
    shape over every scanned frequency (`known`, older scans included) is
    taken off first; 'hump' is how far the top tenth of what is left sits
    above the bottom tenth. Dense FT traffic lifts the quiet level too, so
    the hump only counts where signals cover under 30% of the passband.
    'noise' is the floor above the scan's median floor: the low bands are
    naturally noisier, and a band 10 dB over the rest with only weak
    signals on it is 'noisy'."""
    n = len(rows[0]["_shape"])
    shapes = [r["_shape"] for r in known if len(r.get("_shape", ())) == n]
    shape = [sorted(c)[len(c) // 2] for c in zip(*shapes)]
    typical = pct([r["floor"] for r in rows], .5)
    for r in rows:
        res = [a - b for a, b in zip(r["_shape"], shape)]
        r["hump"] = round(pct(res, .9) - pct(res, .1), 1)
        r["noise"] = round(r["floor"] - typical, 1)
        r["qrm"] = r["hump"] >= QRM_HUMP_DB and r["busy"] < 0.3
        weak = r["signals"] < 3 or (r["strength"] or 0) < WEAK_SIG_DB
        r["label"] = ("QRM" if r["qrm"] and r["signals"] < 3 else "noisy" if r["noise"] >= NOISY_DB and weak
                      else "busy" if r["signals"] >= 10 else "active" if r["signals"] >= 3 else "quiet")

LOG_LAST = {"mt": None, "last": {}, "qsos": []}
def log_last():
    """{(band, mode): (last QSO epoch, QSOs in the last 24 h)} over the whole log."""
    import datetime
    paths = [p for p in ws_files("wsjtx_log.adi") if os.path.exists(p)]
    mt = tuple(os.path.getmtime(p) for p in paths)
    if mt == LOG_LAST["mt"]:
        return LOG_LAST["last"]
    last, now, qsos = {}, time.time(), []
    for p in paths:
        for rec in open(p, errors="ignore").read().split("<eor"):
            md = re.search(r"<qso_date:\d+>(\d{8})", rec, re.I)
            mt_ = re.search(r"<time_on:\d+>(\d{4})", rec, re.I)
            mb = re.search(r"<band:\d+>(\w+)", rec, re.I)
            mm = re.search(r"<submode:\d+>(\w+)", rec, re.I) or re.search(r"<mode:\d+>(\w+)", rec, re.I)
            if not (md and mt_ and mb and mm):
                continue
            ts = datetime.datetime.strptime(md.group(1) + mt_.group(1), "%Y%m%d%H%M").replace(
                tzinfo=datetime.timezone.utc).timestamp()
            k = (mb.group(1).lower(), mm.group(1).upper())
            qsos.append((ts,) + k)
            t0, n = last.get(k, (0, 0))
            last[k] = (max(t0, ts), n + (now - ts < 86400))
    LOG_LAST.update(mt=mt, last=last, qsos=qsos)
    return last

def ws_set_mode(m, wait=8.0):
    """Switch WS's mode (Configure) and wait for Status to report it."""
    if mode_raw() == m:
        return True
    CTX["tx"].sendto(pack_configure(state["ws_id"], "", "", False, mode=m), ws_dest())
    end = time.time() + wait
    while time.time() < end:
        if mode_raw() == m:
            time.sleep(0.5)  # WS may jump to that mode's frequency first: let it, then set ours
            return True
        time.sleep(0.1)
    return False

def ws_set_dial(hz):
    CTX["tx"].sendto(struct.pack(">III", 0xADBCCBDA, 2, SET_DIAL) + pack_str(state["ws_id"])
                     + struct.pack(">Q", int(hz)), ws_dest())

def mode_raw():
    return state["udp"].get("mode")

def scan_spectrum(data, off):
    """UDP thread, while scanning: spectra go to the scan instead of the hunter."""
    q = SCAN["q"]
    if q is not None:
        q.put(parse_spectrum(data, off))

def scan_measure(pt, per_ms):
    """Tune to one point and return the first spectrum of a period that
    started after the retune, or None."""
    import queue
    while not SCAN["q"].empty():
        SCAN["q"].get_nowait()
    ws_set_dial(pt["hz"])
    t_tune = time.time()
    ready = tod_ms(t_tune + SCAN_SETTLE)
    end = t_tune + 3 * per_ms / 1000 + 4
    while time.time() < end and not SCAN["stop"]:
        try:
            t_ms, tr_ms, start, bin_hz, db = SCAN["q"].get(timeout=0.5)
        except queue.Empty:
            if time.time() - t_tune > 2 and abs((state["udp"].get("dial") or 0) - pt["hz"]) > 100:
                ws_set_dial(pt["hz"])  # the dial command was lost
                t_tune, ready = time.time(), tod_ms(time.time() + SCAN_SETTLE)
            continue
        if tr_ms != per_ms or (t_ms - ready) % 86400000 > 43200000:
            continue  # started before the retune had settled
        if abs((state["udp"].get("dial") or 0) - pt["hz"]) > 100:
            continue  # WS isn't there (yet)
        return t_ms, start, bin_hz, db
    return None

def scan_run(points):
    import queue
    u = state["udp"]
    orig_hz, orig_mode, was_running = u.get("dial"), mode_raw(), state["running"]
    SCAN.update(on=True, stop=False, goto=None, i=0, n=len(points), t0=time.time(), err="", cur=None,
                q=queue.Queue(), by_tm={})
    if was_running:
        state["running"] = False; state["target"] = None; ENG["cq_until"] = 0
    halt_ws()
    CTX["tx"].sendto(pack_set_bool(SET_TX_ENABLED, state["ws_id"], False), ws_dest())
    log(f"band scan: {len(points)} frequencies" + (", hunter paused" if was_running else ""))
    done = []
    try:
        if not ws_set_mode("FT2"):
            raise RuntimeError("WS did not switch to FT2")
        per = int(MODES["FT2"][0] * 1000)
        for pt in points:
            if SCAN["stop"]:
                break
            SCAN["cur"] = pt
            if u.get("transmitting"):
                halt_ws()
            got = scan_measure(pt, per)
            SCAN["i"] += 1
            if not got:
                continue
            t_ms, start, bin_hz, db = got
            m = scan_metrics(db, start, bin_hz, max(MODES[md][1] for md in pt["modes"]))
            if not m:
                continue
            row = {**pt, **m, "t": time.time(), "decodes": 0}
            SCAN["by_tm"][t_ms] = row  # FT2 decodes of that period arrive after we move on
            for md in pt["modes"]:
                SCAN["rows"][f"{pt['hz']} {md}"] = row
            done.append(row)
            scan_rate(done, {id(r): r for r in SCAN["rows"].values()}.values())
    except Exception as e:
        SCAN["err"] = str(e)[:80]
        log(f"band scan: {e}")
    finally:
        SCAN.update(on=False, cur=None, q=None)
        hz, md = SCAN["goto"] or (orig_hz, orig_mode)
        try:
            if md:
                ws_set_mode(md)
            if hz:
                ws_set_dial(hz)
        except Exception as e:
            log(f"band scan: could not tune back: {e}")
        if was_running:
            state["running"] = True
        try:
            with open(SCAN_FILE, "w") as f:
                json.dump(list(SCAN["rows"].items()), f)
        except OSError:
            pass
        log(f"band scan done: {len(done)} of {len(points)} measured in {round(time.time() - SCAN['t0'])} s"
            + (f", now on {hz / 1e6:.4f} MHz {md}" if hz and md else "") + (", hunter resumed" if was_running else ""))

def scan_go(hz, md):
    """Tune WS to a scanned frequency and mode (after a running scan stops)."""
    if SCAN["on"]:
        SCAN["goto"], SCAN["stop"] = (hz, md), True
        return
    if ws_set_mode(md):
        ws_set_dial(hz)
    log(f"tuned to {hz / 1e6:.4f} MHz {md} from the band scan")

def scan_state():
    now, last, rates = time.time(), log_last(), band_rates()
    rows = []
    for key, r in list(SCAN["rows"].items()):
        md = key.split()[1]
        lt, n24 = last.get((r["band"], md), (0, 0))
        rows.append({k: v for k, v in r.items() if k != "modes" and k[0] != "_"} | {
            "mode": md, "age": round(now - r["t"]), "worked_ago": round(now - lt) if lt else None,
            "worked_24h": n24, "here": r["hz"] == state["udp"].get("dial") and md == mode_raw(),
            "hunted": rates.get((r["band"], md))})
    rows.sort(key=lambda r: (r["hz"], r["mode"]))
    cur = SCAN["cur"]
    return {"on": SCAN["on"], "i": SCAN["i"], "n": SCAN["n"], "err": SCAN["err"],
            "cur": cur and {"band": cur["band"], "modes": cur["modes"], "hz": cur["hz"]},
            "eta": round((SCAN["n"] - SCAN["i"]) * 2 * MODES["FT2"][0]) if SCAN["on"] else 0,
            "src": SCAN["freqs"]["src"], "freqs": [list(x) for x in SCAN["freqs"]["list"]],
            "ws_hunter": has_ws_hunter(), "rows": rows, "on_since": ORIGIN["on_since"]}

def load_scan():
    try:
        SCAN["rows"] = {k: v for k, v in json.load(open(SCAN_FILE))}
        rows = list({id(r): r for r in SCAN["rows"].values() if "_shape" in r}.values())
        if rows:
            scan_rate(rows, rows)  # labels by the current rules
    except (OSError, ValueError, KeyError, TypeError):
        pass

# --- Antenna tune after a band change. Listening needs no tune, so a new band
# is heard first (new_band_wait); then, just before the first transmission
# there, move just under its FT8 frequency (clear of everyone using FT8 above
# the dial), start the rig's tuner, then go back to the operating frequency.
def new_band_wait(now):
    """Seconds still to listen after arriving on a band (or tuning there):
    until the new_band_cycles setting's whole cycles have been heard, so the offset
    picker has the last two periods of each parity before we transmit."""
    t = ENG.get("arrived")
    if not t:
        return 0
    per = period()
    first = math.ceil(t / per) * per  # the first whole period after we got here
    return first + 2 * state["config"]["new_band_cycles"] * per - now
def need_tune():
    b = band()
    return bool(state["config"].get("tune_new_band") and has_ws_hunter() and b and ENG.get("tuned_band")
                and b != ENG["tuned_band"] and not SCAN["on"] and RIG.get("xcvr") in CIV)

def start_tune():
    if ENG.get("tuning"):
        return
    ENG["tuning"] = "starting"
    threading.Thread(target=atu_tune, daemon=True).start()

# US amateur HF/6m privileges (kHz) by license class: where a tune carrier
# may go. CW is allowed anywhere in a class's privileges (47 CFR 97.305).
PRIV = {
    "extra": {"160m": [(1800, 2000)], "80m": [(3500, 4000)], "40m": [(7000, 7300)],
              "30m": [(10100, 10150)], "20m": [(14000, 14350)], "17m": [(18068, 18168)],
              "15m": [(21000, 21450)], "12m": [(24890, 24990)], "10m": [(28000, 29700)],
              "6m": [(50000, 54000)]},
    "general": {"160m": [(1800, 2000)], "80m": [(3525, 3600), (3800, 4000)],
                "40m": [(7025, 7125), (7175, 7300)], "30m": [(10100, 10150)],
                "20m": [(14025, 14150), (14225, 14350)], "17m": [(18068, 18168)],
                "15m": [(21025, 21200), (21275, 21450)], "12m": [(24890, 24990)],
                "10m": [(28000, 29700)], "6m": [(50000, 54000)]},
}
TUNE_EDGE_KHZ = 3  # keep the carrier this far inside a segment edge

def tune_freq(b):
    """Where to tune on band b, in Hz: at least tune_away_khz (50) from its
    FT8 dial frequency so the tune carrier stays off the FT8 segment. Below
    if the license allows it there, else above; if neither fits (General on
    80/40/30m) the allowed spot nearest to that distance. Returns (hz, note);
    hz None on 60m (channels) or an unknown band."""
    ft8 = next((khz for khz, m in SCAN["freqs"]["list"] if m == "FT8" and band_of(khz * 1000) == b), None)
    lic = state["config"].get("license") or "general"
    segs = PRIV.get(lic, PRIV["general"]).get(b)
    if not ft8 or not segs:
        return None, "60m is channelized" if b == "60m" else f"no FT8 frequency known for {b}"
    away = max(50, float(state["config"].get("tune_away_khz", 50)))
    segs = [(lo + TUNE_EDGE_KHZ, hi - TUNE_EDGE_KHZ) for lo, hi in segs]
    def inside(k):
        return any(lo <= k <= hi for lo, hi in segs)
    for k, where in ((ft8 - away, "below"), (ft8 + away, "above")):
        if inside(k):
            return round(k * 1000), f"{away:g} kHz {where} FT8"
    # nearest allowed spot to either goal, preferring below on a tie
    best = min(((abs(min(max(g, lo), hi) - g), i, min(max(g, lo), hi))
                for i, g in enumerate((ft8 - away, ft8 + away)) for lo, hi in segs))[2]
    return round(best * 1000), (f"{abs(best - ft8):g} kHz {'below' if best < ft8 else 'above'} FT8: "
                                f"as far as {lic.title()} privileges allow")

def wait_dial(hz, secs=6):
    """Until both WS and the rig itself are on hz."""
    end = time.time() + secs
    while time.time() < end:
        try:
            if abs((state["udp"].get("dial") or 0) - hz) <= 100 and abs(int(float(flrig().rig.get_vfo())) - hz) <= 100:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False

def tuner_state(fl):
    """The G90's tuner over CI-V 1C 01: 0 off, 1 on, 2 tuning; None = no answer
    (it doesn't answer while tuning)."""
    addr = CIV[RIG["xcvr"]][0]
    rep = str(fl.rig.cat_string(f"xFE xFE x{addr:02X} xE0 x1C x01 xFD"))
    m = re.search(rf"FE FE E0 {addr:02X} 1C 01 ([0-9A-F]{{2}}) FD", rep)
    return int(m.group(1), 16) if m else None

def atu_tune():
    """Runs in its own thread while ENG["tuning"] holds the engine off."""
    b, op = band(), state["udp"].get("dial")
    t0 = time.time()
    iv = [t0, None]
    ENG["tune_iv"].append(iv)  # the offset picker skips the periods we were off the dial
    try:
        hz, why = tune_freq(b)
        if not (hz and op):
            log(f"antenna tune: {why}; not tuning")
            return
        ENG["tuning"] = f"{b}: moving to {hz / 1e6:.4f} MHz"
        halt_ws()
        CTX["tx"].sendto(pack_set_bool(SET_TX_ENABLED, state["ws_id"], False), ws_dest())
        ws_set_dial(hz)
        if not wait_dial(hz) or band() != b:
            log(f"antenna tune: the rig didn't reach {hz / 1e6:.4f} MHz; not tuning")
            return
        time.sleep(0.5)
        fl = flrig()
        ENG["tuning"] = f"{b}: tuning at {hz / 1e6:.4f} MHz"
        fl.rig.tune()
        time.sleep(1.5)
        end, st = time.time() + state["config"]["tune_wait"], None
        while time.time() < end:
            st = tuner_state(fl)
            if st is not None and st != 2:
                break
            time.sleep(0.5)
        log(f"antenna tuned on {b} at {hz / 1e6:.4f} MHz, {why} ({round(time.time() - t0)} s)" if st is not None and st != 2
            else f"antenna tune on {b}: no word from the tuner after {state['config']['tune_wait']} s")
    except Exception as e:
        log(f"antenna tune: {e}")
    finally:
        try:
            if op and band_of(op) == b:
                ws_set_dial(op)  # back where we were going to operate
                wait_dial(op)
        except Exception:
            pass
        ENG["tuned_band"] = b  # tried once per band change: don't retry every period
        iv[1] = time.time()
        ENG["tuning"] = None

# ---- online logbooks: each contact WS logs (its Logged ADIF packet) is queued
# for every service turned on and sent from a background thread, retried while
# the service can't be reached. Keys and passwords live in logbooks.json (owner
# only) and never go to the web page; the queue survives a restart.
LB_FILE = os.path.join(DATA_DIR, "logbooks.json")
LB_QUEUE_FILE = os.path.join(DATA_DIR, "logbooks_queue.json")
LB = {"cfg": {}, "queue": [], "status": {}, "lock": threading.Lock(), "wake": threading.Event()}

def lb_post(url, data, timeout=20):
    """POST a form: (HTTP status, body). Can't reach it: OSError."""
    import urllib.request, urllib.error, urllib.parse
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode(),
                                 headers={"User-Agent": "FT8-Hunter/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def adif_record(text):
    """One ADIF record from WS's Logged ADIF text: the header dropped, <eor> kept."""
    m = re.search(r"<eoh>", text, re.I)
    rec = " ".join((text[m.end():] if m else text).split())
    return rec if re.search(r"<eor>\s*$", rec, re.I) else rec + " <eor>"

def adif_field(rec, name):
    m = re.search(rf"<{name}:(\d+)(?::[^>]*)?>", rec, re.I)
    return rec[m.end():m.end() + int(m.group(1))].strip() if m else ""

def qrz_reply(body):
    """QRZ answers with a form: RESULT=OK&LOGID=...; REASON can hold spaces."""
    return {k.strip().upper(): " ".join(v.split()) for k, _, v in (kv.partition("=") for kv in body.strip().split("&"))}

def qrz_send(cfg, rec):
    code, body = lb_post("https://logbook.qrz.com/api", {"KEY": cfg["key"], "ACTION": "INSERT", "ADIF": rec})
    r = qrz_reply(body)
    res, why = r.get("RESULT", "").upper(), r.get("REASON", "")
    if res in ("OK", "REPLACE"):
        return "ok", ""
    if "duplicate" in why.lower():
        return "dupe", ""
    if res == "AUTH" or "api key" in why.lower():
        return "hold", why or "the API key was refused"
    if res == "FAIL":
        return "hold", why or "rejected"
    return "retry", f"HTTP {code}: {body.strip()[:60]}"

def qrz_test(cfg):
    code, body = lb_post("https://logbook.qrz.com/api", {"KEY": cfg["key"], "ACTION": "STATUS"})
    r = qrz_reply(body)
    if r.get("RESULT", "").upper() == "OK":
        return True, f"logbook {r.get('CALLSIGN', '?')}, {r.get('COUNT', '?')} contacts"
    return False, r.get("REASON") or f"HTTP {code}: {body.strip()[:60]}"

# service: name, settings (key, label, secret), where to find them, send, test
LOGBOOKS = {
    "qrz": {"name": "QRZ Logbook", "fields": [("key", "API key", True)],
            "help": "qrz.com → My Logbook → Settings: the API key (uploads need a QRZ XML subscription)",
            "send": qrz_send, "test": qrz_test},
}

def lb_ready(svc):
    c = LB["cfg"].get(svc, {})
    return bool(c.get("on")) and all(c.get(k) for k, _, _ in LOGBOOKS[svc]["fields"])

def lb_save():
    for path, obj in ((LB_FILE, LB["cfg"]), (LB_QUEUE_FILE, LB["queue"])):
        try:  # keys and passwords: owner only
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                json.dump(obj, f, indent=1)
        except OSError as e:
            log(f"logbooks not saved: {e}")

def lb_load():
    for path, key in ((LB_FILE, "cfg"), (LB_QUEUE_FILE, "queue")):
        try:
            LB[key] = json.load(open(path))
        except (OSError, ValueError):
            pass

def lb_enqueue(adif):
    """A contact WS just logged goes to every logbook turned on."""
    rec = adif_record(adif)
    call = adif_field(rec, "call").upper()
    if not call:
        return
    with LB["lock"]:
        for svc in LOGBOOKS:
            if lb_ready(svc) and not any(i["svc"] == svc and i["rec"] == rec for i in LB["queue"]):
                LB["queue"].append({"svc": svc, "rec": rec, "call": call, "tries": 0, "next": 0})
        lb_save()
    LB["wake"].set()

def lb_run(now=None):
    """Send what's due. ok / dupe: done; retry: later, backing off; hold: the
    service refused it (bad key, rejected) and it waits for Save or Retry."""
    now = now or time.time()
    with LB["lock"]:
        due = [i for i in LB["queue"] if not i.get("held") and i["next"] <= now and lb_ready(i["svc"])]
    for it in due:
        svc = LOGBOOKS[it["svc"]]
        try:
            res, why = svc["send"](LB["cfg"][it["svc"]], it["rec"])
        except Exception as e:  # OSError: offline, DNS, timeout
            res, why = "retry", str(getattr(e, "reason", e))[:80]
        st = LB["status"].setdefault(it["svc"], {"sent": 0, "dupes": 0})
        with LB["lock"]:
            if res in ("ok", "dupe"):
                LB["queue"].remove(it)
                st["sent" if res == "ok" else "dupes"] += 1
                st.update(last=it["call"], last_t=time.time(), err="")
                log(f"{svc['name']}: {it['call']} " + ("logged" if res == "ok" else "was already there"))
            elif res == "hold":
                it.update(held=True, err=why); st["err"] = why
                log(f"{svc['name']} refused {it['call']}: {why}. Fix it in Online logbooks, then Retry")
            else:
                it["tries"] += 1
                wait = min(3600, 60 * 2 ** (it["tries"] - 1))
                it.update(next=now + wait, err=why); st["err"] = why
                if it["tries"] == 1:
                    log(f"{svc['name']}: can't send {it['call']} ({why}); retrying, every hour at most")
            lb_save()

def lb_worker():
    while True:
        LB["wake"].wait(20); LB["wake"].clear()
        try:
            lb_run()
        except Exception as e:
            log_err("logbooks", e)

def lb_view():
    """Logbook settings for the page: secrets only say whether they're set."""
    out = []
    for svc, d in LOGBOOKS.items():
        c, st = LB["cfg"].get(svc, {}), LB["status"].get(svc, {})
        q = [i for i in LB["queue"] if i["svc"] == svc]
        out.append({"id": svc, "name": d["name"], "help": d["help"], "on": bool(c.get("on")),
                    "ready": lb_ready(svc),
                    "fields": [{"key": k, "label": lab, "secret": sec, "set": bool(c.get(k)),
                                "value": "" if sec else c.get(k, "")} for k, lab, sec in d["fields"]],
                    "sent": st.get("sent", 0), "dupes": st.get("dupes", 0),
                    "last": st.get("last"), "last_t": st.get("last_t"),
                    "waiting": sum(not i.get("held") for i in q), "held": sum(bool(i.get("held")) for i in q),
                    "err": next((i["err"] for i in q if i.get("held")), "") or (st.get("err", "") if q else "")})
    return out

def lb_update(svc, body):
    """Settings from the page. A blank secret keeps the saved one; saving lets
    held contacts go again."""
    c = LB["cfg"].setdefault(svc, {})
    for k, _, sec in LOGBOOKS[svc]["fields"]:
        v = str(body.get("fields", {}).get(k, "")).strip()
        if v or not sec:
            c[k] = v
    if "on" in body:
        c["on"] = bool(body["on"])
    lb_retry(svc)
    log(f"{LOGBOOKS[svc]['name']}: " + ("uploading each contact" if lb_ready(svc) else
                                        "off" if not c.get("on") else "on, but needs its settings filled in"))

def lb_retry(svc):
    with LB["lock"]:
        for i in LB["queue"]:
            if i["svc"] == svc:
                i.update(held=False, next=0)
        lb_save()
    LB["wake"].set()

# Updates. The hunter asks this repo's latest release twice a day (and on
# Check now). It only tells the page and the window: nothing is installed
# until the operator says so. Only an installed Windows hunter installs
# itself (the new installer, its SHA-256 checked, run silently, the hunter
# restarted by it); anywhere else the page links the release.
UPDATE_REPO = "TheBengineer/FT8-Hunter"
UPDATE_ASSET = "FT8-Hunter-windows-x64-setup.exe"
UPDATE_EVERY = 12 * 3600
UPD = {"latest": "", "notes": "", "url": "", "asset": None, "t": 0, "err": "", "busy": "", "pct": 0,
       "told": "", "wake": threading.Event()}

def ver_key(v):
    """'v1.10.2' -> (1, 10, 2), for comparing; anything else sorts lowest."""
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?$", str(v).strip())
    return tuple(int(x or 0) for x in m.groups()) if m else (-1,)

def upd_get(url, timeout=30):
    import urllib.request, ssl
    try:  # the packaged builds carry certifi's root certificates: python.org's macOS Python has none
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = None
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        "User-Agent": f"FT8-Hunter/{VERSION}", "Accept": "application/vnd.github+json"}), timeout=timeout, context=ctx)

def update_check():
    """The newest release: its version, notes, page and Windows installer."""
    try:
        with upd_get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest") as r:
            rel = json.load(r)
        a = next((x for x in rel.get("assets", []) if x.get("name") == UPDATE_ASSET), None)
        UPD.update(latest=str(rel.get("tag_name", "")).lstrip("v"), notes=(rel.get("body") or "")[:4000],
                   url=rel.get("html_url", ""), err="",
                   asset=a and {"url": a["browser_download_url"], "size": a["size"],
                                "sha256": (a.get("digest") or "").partition("sha256:")[2].lower()})
    except Exception as e:
        UPD["err"] = f"can't check for updates: {e}"[:160]
    UPD["t"] = time.time()
    if update_newer() and UPD["told"] != UPD["latest"]:
        UPD["told"] = UPD["latest"]
        log(f"FT8 Hunter {UPD['latest']} is out (this is {VERSION})")

def update_newer():
    return ver_key(UPD["latest"]) > ver_key(VERSION)

def update_installable():
    """Installed by our installer on Windows, and the release has one with its SHA-256."""
    a = UPD["asset"]
    return bool(FROZEN and sys.platform == "win32" and a and len(a["sha256"]) == 64
                and os.path.exists(os.path.join(os.path.dirname(sys.executable), "unins000.exe")))

def update_view():
    c = state["config"]
    return {"version": VERSION, "latest": UPD["latest"], "newer": update_newer(),
            "skipped": bool(UPD["latest"]) and c["update_skip"] == UPD["latest"],
            "notes": UPD["notes"], "url": UPD["url"], "can_install": update_installable(),
            "busy": UPD["busy"], "pct": UPD["pct"], "err": UPD["err"], "checked": UPD["t"], "on": c["update_check"]}

def update_worker():
    UPD["wake"].wait(30)  # not in the rush of starting up
    while True:
        UPD["wake"].clear()
        if state["config"]["update_check"] and time.time() - UPD["t"] >= UPDATE_EVERY:
            update_check()
        UPD["wake"].wait(600)

def update_launch(path):
    """Run the new installer on its own and quit, so it can replace us; it
    starts the hunter again when it's done (/UPDATE=1)."""
    import subprocess
    local = os.path.normcase(os.environ.get("LOCALAPPDATA", "~")) + os.sep
    who = "/CURRENTUSER" if os.path.normcase(sys.executable).startswith(local) else "/ALLUSERS"  # as installed (all users: a UAC prompt)
    # without this the installer, and the hunter it starts, inherit this one-file
    # exe's PyInstaller variables: the new hunter then looks for Python in our
    # unpacked folder, gone once we quit ("Failed to load Python DLL")
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", who, "/UPDATE=1"], close_fds=True, env=env,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    time.sleep(1)
    shutdown(None, None)

def update_install():
    """Download the new installer, check it against the release, run it."""
    import hashlib
    a, ver = UPD["asset"], UPD["latest"]
    path = os.path.join(DATA_DIR, "updates", f"FT8-Hunter-{ver}-setup.exe")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h, n = hashlib.sha256(), 0
        with upd_get(a["url"], 120) as r, open(path + ".part", "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk); h.update(chunk); n += len(chunk)
                UPD["pct"] = min(100, round(100 * n / max(a["size"], 1)))
        if n != a["size"] or h.hexdigest() != a["sha256"]:
            os.remove(path + ".part")
            raise ValueError("the download doesn't match the release (size or SHA-256)")
        os.replace(path + ".part", path)
        UPD["busy"] = "installing"
        log(f"installing FT8 Hunter {ver}: the hunter closes and starts again when it's done")
        update_launch(path)
    except Exception as e:
        UPD.update(busy="", err=f"update failed: {e}"[:160])
        log(UPD["err"])

def update_start():
    """The operator said yes: stop hunting, halt WS's TX, then download and
    install in the background. Why not, if it can't now."""
    why = ("this hunter can't install updates itself (only an installed Windows one can): use the release page"
           if not update_installable() else "no newer version" if not update_newer()
           else "already updating" if UPD["busy"]
           else "a QSO is in progress: finish or skip it first" if state["target"] else "")
    if why:
        return why
    if state["running"]:
        state["running"] = False; ENG["cq_until"] = 0; at("stopped")
    halt_ws()
    log(f"updating to {UPD['latest']}: hunter stopped, TX halted")
    UPD.update(busy="downloading", pct=0, err="")
    threading.Thread(target=update_install, daemon=True).start()
    return ""

class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path == "/":
            try:
                b = open(os.path.join(RES_DIR, "ft8_app.html"), "rb").read()
                self.send_response(200); self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b))); self.end_headers()
                self.wfile.write(b); return
            except FileNotFoundError:
                self.send_error(404); return
        if self.path == "/nosleep.js":  # NoSleep.js (MIT), vendored: phones on the LAN may be offline
            b = open(os.path.join(RES_DIR, "nosleep.min.js"), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b); return
        if self.path == "/api/globe":
            return self._json(globe())
        if self.path == "/api/lan":
            # with each address's QR code ("0"/"1" rows), so the page needs no QR library from the internet
            d = lan_urls()
            d["qr"] = {u: ["".join("1" if on else "0" for on in row) for row in m]
                       for u, m in ((u, qr_matrix(u)) for u in d["urls"]) if m}
            return self._json(d)
        if self.path == "/api/timeline":
            # contacts of the last 24 h, oldest first, for the time bar: the
            # log's (minute) times, or the moment WS logged one this session
            now = time.time()
            qs = {k: ts for k, ts in list({**state["worked"], **RECENT}.items()) if ts and now - ts < 86400}
            return self._json({"now": now, "qsos": [{"call": c, "band": b, "mode": m, "t": ts}
                                                     for (c, b, m), ts in sorted(qs.items(), key=lambda kv: kv[1])]})
        if self.path == "/api/logbooks":
            return self._json({"services": lb_view()})
        if self.path == "/api/scan":
            return self._json(scan_state())
        if self.path == "/api/waterfall":
            return self._json(waterfall())
        if self.path == "/api/debug/spectrum":
            return self._json({"noise_hist": [round(x, 1) for x in list(ENG["noise_hist"])],
                               "spectra": list(ENG["spec_log"])})
        if self.path == "/api/state":
            # snapshot shared structures first: the UDP/engine threads mutate them
            now = time.time()
            cq = list(state["cq"].values())
            for e in cq:
                e["worked"] = is_worked(e["call"])
                e["skip"] = cq_skip(e, now)
                e["hold_s"] = max(0, round(ENG["holds"].get(slot_key(e["call"]), 0) - now))
                e["pileup"] = pileup(e["call"], now)
                e["score"] = round(cq_score(e, now))
            cq.sort(key=cq_rank)
            ans = sorted(list(state["answering"].values()), key=lambda e: (not e["tome"], -e["seen"]))
            all_msgs = list(state["msgs"])
            msgs = all_msgs[:30]
            dxf = [m for m in all_msgs if m.get("dx")][:15]
            u = state["udp"]
            u["holder"] = udp_holder(udp_port)
            here = (band(), mode())  # only this band and mode's holds and contacts apply
            holds = {k[0]: max(0, round(until - now)) for k, until in list(ENG["holds"].items())
                     if until > now and k[1:] == here}
            worked = sorted({c for c, b, m in list(state["worked"]) if is_worked(c)})
            # today's book across every band and mode, newest first: contacts
            # plus stations we gave up on (still on hold or not)
            book = [{"call": c, "band": b, "mode": m, "kind": "worked", "t": ts}
                    for (c, b, m), ts in list(state["worked"].items())]
            book += [{"call": c, "band": k[1], "mode": k[2], "kind": "ignored", "t": ts,
                      "hold_s": max(0, round(ENG["holds"].get(k, 0) - now))}
                     for k, (c, ts) in list(ENG["held"].items())
                     if not any(base_call(w[0]) == k[0] and w[1:] == k[1:] for w in list(state["worked"]))]
            book.sort(key=lambda e: -(e["t"] or 0))
            par = ENG["parity"]
            return self._json({"running": state["running"], "mycall": state["mycall"],
                "mygrid": state["mygrid"], "target": state["target"], "cq": cq[:40],
                "suggest": suggestions(now) if manual_on() else [],
                "manual": manual_on(), "manual_forced": not has_ws_hunter(),
                "answering": ans[:80], "now": now, "worked": worked, "book": book[:200], "band": band(),
                "msgs": msgs, "mytx": list(state["mytx"])[:15], "origins": origin_stats(), "dxfocus": dxf,
                "holds": holds, "callers": [c["call"] for c in list(ENG["callers"])],
                "parity": {"even": par[0], "odd": par[1],
                           "hot": ("even" if par[0] >= par[1] else "odd")},
                "idle": max(0, round(ENG["idle_until"] - time.time())),
                "cq_calling": max(0, round(ENG["cq_until"] - time.time())),
                "mode": mode(), "period": period(),
                "rig": {**{k: v for k, v in RIG.items() if k != "url"}, "qso": answered()}, "rx": rx_level(now),
                "swr": swr_state(),
                "light": tx_light(now), "caps": ws_caps(),
                "step": ENG.get("step") or {"step": "stopped" if not state["running"] else "idle"},
                "qrm": {"secs": max(0, round(ENG["qrm_until"] - time.time())), "why": ENG["qrm_why"]},
                "desync": {"why": DESYNC["why"], "t": DESYNC["t"], "stopped": DESYNC["stopped"]},
                "wrl": {"on": WRL["on"], "port": state["config"]["wrl_port"], "warn": state["config"].get("wrl_warn", True)},
                "txdf": {"current": u.get("tx_df"), **ENG.get("suggest", {})},
                "log": list(state["log"]), "config": state["config"], "stats": state["stats"],
                "update": update_view(),
                "udp": {"holder": u["holder"], "ws_id": state["ws_id"],
                        "revision": u.get("revision", ""), "ws_hunter": has_ws_hunter(),
                        "dxcall": u["dxcall"], "rx_count": u["rx_count"],
                        "de_call": u.get("de_call", ""), "de_grid": u.get("de_grid", ""),
                        "tx_enabled": u.get("tx_enabled", False),
                        "tx_msg": u.get("tx_msg", ""), "watchdog": u.get("watchdog", False),
                        "transmitting": u.get("transmitting", False) if u.get("tx_t", 0) and time.time() - u.get("tx_t", 0) < 20 else False,
                        "by_type": u.get("by_type", {}),
                        "fwd_port": state["config"]["wrl_port"],
                        "wrl_by_type": u.get("wrl_by_type", {}),
                        "heard_ago": round(time.time() - u["last_rx"], 1) if u["last_rx"] else None}})
        self.send_error(404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        p = urlparse(self.path).path
        if p == "/api/start" and not state["mycall"]:
            return self._json({"error": "set your call sign in Settings (or in WS's Settings -> General)"}, 409)
        if p == "/api/start" and DESYNC["why"] and state["config"].get("desync_stop", True):
            return self._json({"error": f"radio out of sync: {DESYNC['why']}. Fix it first (FLRig, the radio, WS's dial)"}, 409)
        if p == "/api/start":
            state["running"] = True; DESYNC.update(stopped=False, started=time.time())
            log("hunter started"); at("idle", "starting at next period")
        elif p == "/api/update/check":
            update_check()
            return self._json(update_view())
        elif p == "/api/update/skip":
            state["config"]["update_skip"] = UPD["latest"]; save_config()
            log(f"FT8 Hunter {UPD['latest']}: skipped, not offered again")
        elif p == "/api/update/install":
            why = update_start()
            if why:
                return self._json({"error": why}, 409)
        elif p == "/api/stop":
            state["running"] = False; state["target"] = None; ENG["cq_until"] = 0
            ENG["callers"].clear(); halt_ws(); log("stopped, TX halted"); at("stopped")
        elif p == "/api/skip":
            t = state["target"]
            if t:
                halt_ws()
                hold(t["call"])
            state["target"] = None; log("target skipped + held")
        elif p == "/api/cq":
            # reset WS stack to default CQ + stop hunting so WS calls CQ on its own
            halt_ws()
            state["running"] = False
            state["target"] = None; ENG["callers"].clear()
            try:
                select_cq()
                how = enable_tx()
                log(f"CQ mode: stack reset + {how}")
            except Exception as e:
                log(f"CQ reset failed: {e}")
        elif p == "/api/arm":
            log(f"arm TX: {enable_tx()}")
        elif p == "/api/call" and body.get("call"):
            c = body["call"].upper(); g = body.get("grid", "").upper()
            # the exact CQ line last decoded for this call
            e = next((e for e in list(state["cq"].values()) if same_call(e["call"], c)), None)
            if not e or not e.get("msg"):
                self._json({"error": f"no recent CQ decode for {c}"}, 404); return
            if acquire(e["call"], g or e["grid"], e["msg"],
                       tries=state["config"].get("cq_tries", 3), why="manual"):
                log(f"manual call {e['msg']}")
        elif p == "/api/work":
            # manual (contest) mode: the operator picked one of the suggestions
            s_ = next((x for x in suggestions() if x["msg"] == body.get("msg")), None)
            t = state["target"]
            if not state["running"]:
                return self._json({"error": "start the hunter first"}, 409)
            if not s_:
                return self._json({"error": "no longer on offer (not heard lately, worked or on hold)"}, 409)
            if ENG.get("tuning"):
                return self._json({"error": "tuning the antenna: try again in a moment"}, 409)
            if t and t.get("resp") and not t.get("done"):
                return self._json({"error": f"in a QSO with {t['call']}: finish or skip it first"}, 409)
            if t and t.get("done"):
                add_worked(t["call"])  # signing off: our 73 has gone, the next Reply takes over TX
            for c in [c for c in list(ENG["callers"]) if same_call(c["call"], s_["call"])]:
                ENG["callers"].remove(c)
            if not acquire(s_["call"], "", s_["msg"], tries=s_["tries"], why=s_["why"]):
                return self._json({"error": "no decode details for that message"}, 409)
            if s_["why"].endswith("again"):
                state["target"]["again"] = True
                k = base_call(s_["call"])
                ENG["rr73_again"][k] = ENG["rr73_again"].get(k, 0) + 1
            state["target"]["picked"] = True
            log(f"picked by the operator: {s_['msg']}")
            at("target", f"{s_['call']}: picked")
        elif p == "/api/smoke":
            # Replay poke to WSJT-X's own port: WS answers with decode backlog + Status
            before = state["udp"].get("ws_rx", 0)
            dest = ws_dest()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for _ in range(3):
                s.sendto(pack_replay(state["ws_id"]), dest)
                time.sleep(0.3)
            time.sleep(2.0)
            heard = state["udp"].get("ws_rx", 0) - before
            holder = udp_holder(udp_port)
            state["udp"]["holder"] = holder
            log(f"smoke: holder={holder} ws_id={state['ws_id']} pkts_heard={heard}")
            self._json({"ok": True, "holder": holder, "ws_id": state["ws_id"],
                        "ws_addr": state["ws_addr"], "heard": heard,
                        "connected": heard > 0}); return
        elif p == "/api/rig/power":
            try:
                # the slider sets the power for the stage we are in now
                w = max(1, min(int(body.get("watts", 0)), RIG.get("max", 100)))
                flrig().rig.set_power(w)
                RIG.update(power=w, want=w, pw_t=time.time(), pw_off=0)
                state["config"]["watts_qso" if answered() else "watts_call"] = w
                save_config()
                log(f"TX power {'in QSO' if answered() else 'calling'} set to {w} W")
            except Exception as e:
                return self._json({"error": str(e)[:80]}, 502)
        elif p in ("/api/logbooks", "/api/logbooks/test", "/api/logbooks/retry"):
            svc = body.get("id")
            if svc not in LOGBOOKS:
                return self._json({"error": "unknown logbook"}, 404)
            if p == "/api/logbooks":
                lb_update(svc, body)
            elif p == "/api/logbooks/retry":
                lb_retry(svc)
            else:  # what's typed, else what's saved
                d = LOGBOOKS[svc]
                cfg = {**LB["cfg"].get(svc, {}), **{k: v.strip() for k, v in body.get("fields", {}).items() if v.strip()}}
                if not all(cfg.get(k) for k, _, _ in d["fields"]):
                    return self._json({"ok": False, "detail": "fill in " + ", ".join(lab for k, lab, _ in d["fields"] if not cfg.get(k))})
                try:
                    good, detail = d["test"](cfg)
                except Exception as e:
                    good, detail = False, f"can't reach it: {str(getattr(e, 'reason', e))[:80]}"
                return self._json({"ok": good, "detail": detail})
            return self._json({"ok": True, "services": lb_view()})
        elif p == "/api/scan/start":
            if not has_ws_hunter():
                return self._json({"error": "needs the ws-hunter build of WS"}, 409)
            if SCAN["on"] or ENG.get("tuning"):
                return self._json({"error": "already scanning" if SCAN["on"] else "tuning the antenna"}, 409)
            if answered():  # calling someone may be dropped; a QSO under way is not
                return self._json({"error": f"in a QSO with {state['target']['call']}: scan when it is done"}, 409)
            c = state["config"]
            c["scan_bands"] = [b for b in body.get("bands", c["scan_bands"]) if band_of_name(b)]
            c["scan_modes"] = [m for m in body.get("modes", c["scan_modes"]) if m in MODES]
            save_config()
            pts = scan_points(set(c["scan_bands"]), set(c["scan_modes"]))
            if not pts:
                return self._json({"error": "pick at least one band and mode"}, 400)
            SCAN["on"] = True  # before the thread starts: a second click is refused
            threading.Thread(target=scan_run, args=(pts,), daemon=True).start()
        elif p == "/api/scan/stop":
            SCAN["stop"] = True
        elif p == "/api/scan/go":
            md = body.get("mode")
            if md not in MODES or not band_of(int(body.get("hz", 0))):
                return self._json({"error": "bad frequency or mode"}, 400)
            if answered() and band_of(int(body["hz"])) != band():  # a band change is deliberate, never mid-QSO
                return self._json({"error": f"in a QSO with {state['target']['call']}: change band when it is done"}, 409)
            threading.Thread(target=scan_go, args=(int(body["hz"]), md), daemon=True).start()
        elif p == "/api/txdf":
            # manual Tx offset from the waterfall, locked (the auto picker leaves it
            # alone) unless lock is false; {"lock": bool} alone locks/unlocks the current one
            c = state["config"]
            if "df" in body:
                if not has_ws_hunter():
                    return self._json({"error": "needs the ws-hunter build of WS"}, 409)
                f = max(200, min(int(body["df"]), 3000 - bw()))
                tx, wid = CTX["tx"], state["ws_id"]
                tx.sendto(pack_set_bool(SET_HOLD_TX_FREQ, wid, True), ws_dest())
                tx.sendto(pack_set_u32(SET_TX_DF, wid, f), ws_dest())
                state["udp"]["tx_df"] = f
                c["df_lock"] = bool(body.get("lock", True))
                log(f"Tx offset set to {f} Hz from the waterfall" + (", locked" if c["df_lock"] else ""))
            elif "lock" in body:
                c["df_lock"] = bool(body["lock"])
                cur = state["udp"].get("tx_df")
                log(f"Tx offset locked at {cur} Hz: the auto picker leaves it alone" if c["df_lock"]
                    else "Tx offset unlocked: the auto picker moves it off busy spots")
            save_config()
        elif p == "/api/config":
            state["config"].update({k: v for k, v in body.items() if k in state["config"]})
            c = state["config"]  # keep the offset range usable: 200 Hz up, room for a signal
            c["df_lo"] = max(200, min(int(c["df_lo"]), int(c["df_hi"]) - 300))
            for k in ("watts_call", "watts_qso"):
                c[k] = max(1, min(int(c[k]), RIG.get("max", 100)))
            c["check_every"] = max(0, min(int(c["check_every"]), 100))
            c["tune_away_khz"] = max(50.0, min(float(c["tune_away_khz"]), 150.0))  # off FT8, near enough to match
            c["license"] = c["license"] if c["license"] in PRIV else ""
            c["mycall"] = str(c["mycall"]).strip().upper()
            if c["mycall"] and not MYCALL_RE.fullmatch(c["mycall"]):
                log(f"call sign not saved: {c['mycall']} doesn't look like one"); c["mycall"] = ""
            c["mygrid"] = str(c["mygrid"]).strip()
            if c["mygrid"] and not GRID_RE.match(c["mygrid"].upper()):
                log(f"grid not saved: {c['mygrid']} isn't a 4 or 6 character locator"); c["mygrid"] = ""
            for k in ("udp_port", "http_port", "wrl_port"):
                c[k] = max(1024, min(int(c[k]), 65535))
            c["flrig_url"] = str(c["flrig_url"]).strip() or "http://127.0.0.1:12345"
            c["ws_dir"] = str(c["ws_dir"]).strip()
            c["desync_stop"] = bool(c["desync_stop"])
            c["psk_remote"] = bool(c["psk_remote"])
            c["silent_min"] = max(0, min(int(c["silent_min"]), 60))
            c["wrl_warn"] = bool(c["wrl_warn"]); WRL["t"] = 0  # re-check at the next tick
            c["traffic_secs"] = max(30, min(int(c["traffic_secs"]), 3600))
            c["tune_wait"] = max(5, min(int(c["tune_wait"]), 120))
            c["new_band_cycles"] = max(0, min(int(c["new_band_cycles"]), 10))
            c["swr_high"] = max(1.05, min(float(c["swr_high"]), 5.0))
            c["swr_bad"] = max(c["swr_high"], min(float(c["swr_bad"]), 10.0))
            c["update_check"] = bool(c["update_check"]); c["update_skip"] = str(c["update_skip"])
            if "update_check" in body:
                UPD["wake"].set()  # turned on: look now if it's due
            set_identity()
            if isinstance(c["signoff"], str):
                c["signoff"] = [x for x in re.split(r"[,\n]", c["signoff"]) if x.strip()]
            raw = [" ".join(str(x).upper().split()) for x in c["signoff"] if str(x).strip()][:100]
            c["signoff"] = list(dict.fromkeys(signoff_fix(x) for x in raw if sign_msg(x) is not False))
            bad = [x for x in raw if sign_msg(x) is False]
            if bad:
                log(f"sign-off not usable (needs a 73 for WS to log the QSO; FT8 free text is 13 characters "
                    f"with our call, so 6 for the text; A-Z 0-9 +-./?): {', '.join(bad)}")
            save_config()
            RIG_WAKE.set()  # new power applies now
            if "compat" in body:
                log("compatibility mode " + ("on: plain WSJT-X UDP only; the TX light shows when to Enable Tx"
                                             if c["compat"] else "off: ws-hunter controls when WS has them"))
            if "manual" in body:
                if c["manual"] and ENG["cq_until"]:  # our own CQ run was the hunter's choice: end it
                    ENG["cq_until"] = 0
                    halt_ws()
                log("manual (contest) mode " + ("on: tap a station to start each QSO" if c["manual"]
                                                else "off: the hunter picks"))
            if "cq_first" in body:
                log("CQ first mode " + ("on: running our own CQ" if c["cq_first"] else "off: hunting"))
        else: return self._json({"error": "unknown"}, 404)
        self._json({"ok": True})
    def log_message(self, *a): pass

ap = argparse.ArgumentParser()
# Optional: each one given is saved into Settings (which hold them from then on)
ap.add_argument("--mycall"); ap.add_argument("--mygrid")
ap.add_argument("--port", type=int, help="WS UDP server port (default 2237)")
ap.add_argument("--http", type=int, help="web UI port (default 8080)")
ap.add_argument("--flrig", help="FLRig XML-RPC URL (default http://127.0.0.1:12345)")
ap.add_argument("--window", action="store_true",
                help="run in a small window (link, QR code for phones, Quit) instead of as a service")
ap.add_argument("--no-window", action="store_true",
                help="Windows and packaged builds: run as a service, no window")
a = ap.parse_args()
load_config()
flags = {k: v for k, v in (("mycall", a.mycall and a.mycall.upper()), ("mygrid", a.mygrid), ("udp_port", a.port),
                           ("http_port", a.http), ("flrig_url", a.flrig)) if v}
if flags:
    state["config"].update(flags); save_config()
udp_port = state["config"]["udp_port"]
set_identity()
def qr_matrix(text):
    """QR code modules for `text` (rows of booleans, quiet zone included), or
    None without the qrcode package (packaged builds carry it)."""
    try:
        import qrcode
    except ImportError:
        return None
    q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    return q.get_matrix()

def run_window(httpd):
    """The hunter as a desktop app: the web server in the background and a
    small window with the link (opens the default browser), a QR code of
    the LAN address for phones, the hunter's state, and Quit. False if
    there's no Tk here (python3-tk on Linux), so the caller serves headless."""
    try:
        import tkinter as tk
    except ImportError:
        print("no tkinter here (Linux: apt install python3-tk): running without a window", flush=True)
        return False
    import webbrowser
    port = state["config"]["http_port"]
    local = f"http://localhost:{port}/"
    try:
        root = tk.Tk()
    except tk.TclError as e:  # no display
        print(f"no window ({e}): running without one", flush=True)
        return False
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    root.title("FT8 Hunter")
    root.resizable(False, False)
    pad = {"padx": 14, "pady": 4}
    tk.Label(root, text="📡 FT8 Hunter", font=("TkDefaultFont", 16, "bold")).pack(pady=(12, 2))
    status = tk.Label(root, text="", fg="#555")
    status.pack(**pad)
    link = tk.Label(root, text=local, fg="#1a62c9", cursor="hand2", font=("TkDefaultFont", 12, "underline"))
    link.pack(**pad)
    link.bind("<Button-1>", lambda e: webbrowser.open(local))
    tk.Button(root, text="Open in browser", command=lambda: webbrowser.open(local)).pack(**pad)
    # the phone link and QR code, once the LAN address is known: looking it up
    # can take a while (macOS resolves its .local name, and may first ask
    # whether the app may use the local network), so the window doesn't wait
    lan_box = tk.Frame(root)
    lan_box.pack()
    lan_msg = tk.Label(lan_box, text="Looking up this computer's network address…", justify="center", fg="#555")
    lan_msg.pack(**pad)
    found = {}
    threading.Thread(target=lambda: found.update(urls=lan_urls()["urls"]), daemon=True).start()

    def show_lan():
        if "urls" not in found:
            root.after(250, show_lan)
            return
        lan = (found["urls"] or [None])[0]
        m = qr_matrix(lan) if lan else None
        if m:
            cell = max(3, 200 // len(m))
            cv = tk.Canvas(lan_box, width=len(m) * cell, height=len(m) * cell, bg="white", highlightthickness=0)
            for y, row in enumerate(m):
                for x, on in enumerate(row):
                    if on:
                        cv.create_rectangle(x * cell, y * cell, (x + 1) * cell, (y + 1) * cell, fill="black", width=0)
            cv.pack(pady=(10, 2), before=lan_msg)
        lan_msg.config(text=(f"On your phone (same Wi-Fi): scan, or open\n{lan}" if m else
                             f"On your phone (same Wi-Fi), open\n{lan}" if lan else "No network address found"))
    show_lan()

    def quit_():
        root.destroy()
        shutdown(None, None)  # halts WS if it was ours to run

    from tkinter import messagebox
    upd_box = tk.Frame(root)  # this version, and one button: check, up to date, or update
    upd_box.pack(pady=(6, 0))
    tk.Label(upd_box, text=f"Version {VERSION}", fg="#555").pack()
    upd_btn = tk.Button(upd_box)
    upd_btn.pack()
    btn_fg = upd_btn.cget("fg")  # the theme's (dark mode on macOS), when there's no update
    checking = threading.Event()

    def check_now():
        def run():
            try:
                update_check()
            finally:
                checking.clear()
        checking.set()
        upd_btn.config(text="Checking for updates…", state="disabled")
        threading.Thread(target=run, daemon=True).start()

    def update_ask():
        u = update_view()
        if not u["newer"]:
            check_now()
            return
        if not u["can_install"]:
            webbrowser.open(u["url"])
            return
        ans = messagebox.askyesnocancel("Update FT8 Hunter",
            f"FT8 Hunter {u['latest']} is out (this is {VERSION}).\n\n"
            "Install it now? The hunter stops, WS's TX is turned off, and it starts again once installed.\n\n"
            "Yes: install now · No: skip this version · Cancel: not now", parent=root)
        if ans:
            why = update_start()
            if why:
                messagebox.showinfo("Update FT8 Hunter", f"Not now: {why}.", parent=root)
        elif ans is False:
            state["config"]["update_skip"] = u["latest"]; save_config()
            log(f"FT8 Hunter {u['latest']}: skipped, not offered again")
    upd_btn.config(command=update_ask)

    tk.Button(root, text="Quit", command=quit_, width=12).pack(pady=(8, 14))
    root.protocol("WM_DELETE_WINDOW", quit_)

    def tick():
        t = state["target"]
        status.config(text=(f"{state['mycall'] or 'no call sign yet'} · " +
                            (f"hunting · calling {t['call']}" if state["running"] and t else
                             "hunting" if state["running"] else "stopped (press Start in the page)")))
        u = update_view()
        if u["busy"]:
            text = f"Updating to {u['latest']}: {u['busy']} {u['pct']}%"
        elif checking.is_set():
            text = "Checking for updates…"
        elif u["newer"]:  # offered even when skipped: here it's asked for
            text = f"⬆ Update to {u['latest']}…" if u["can_install"] else f"⬆ {u['latest']} is out: get it…"
        elif u["err"]:
            text = "Couldn't check for updates: try again"
        elif u["checked"]:
            text = "✓ You're on the latest (check again)"
        else:
            text = "Check for updates"
        upd_btn.config(text=text, state="disabled" if u["busy"] or checking.is_set() else "normal",
                       fg="#1f7a3d" if u["newer"] else btn_fg)
        root.after(500 if checking.is_set() or u["busy"] else 2000, tick)
    tick()
    if FROZEN:
        root.after(800, lambda: webbrowser.open(local))  # a packaged app opens the page once at start
    root.mainloop()
    return True

def shutdown(signum, frame):
    """Service stop/restart: halt WS so it isn't left transmitting unattended."""
    if CTX["tx"] is not None and state.get("ws_addr"):
        halt_ws()
        print("halted WS on shutdown", flush=True)
    os._exit(0)
import signal
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
threading.Thread(target=watcher, daemon=True).start()
threading.Thread(target=rig_poll, daemon=True).start()
load_scan()
load_swr()
load_origins()
threading.Thread(target=band_rates, daemon=True).start()  # reads ALL.TXT once
lb_load()
threading.Thread(target=lb_worker, daemon=True).start()
threading.Thread(target=load_freqs, daemon=True).start()
threading.Thread(target=update_worker, daemon=True).start()
log(f"FT8 Hunter {VERSION}")
log(f"hunter ready as {state['mycall']} {state['mygrid']}" if state["mycall"] else
    "hunter ready: no call sign yet (Settings, or WS's own); it won't start until it has one")
print(f"open http://0.0.0.0:{state['config']['http_port']}/")
try:
    httpd = ThreadingHTTPServer(("0.0.0.0", state["config"]["http_port"]), H)
except OSError as e:
    print(f"can't serve on port {state['config']['http_port']}: {e} (is the hunter already running?)", flush=True)
    sys.exit(1)
# a desktop app on Windows (from source too) and in packaged builds; a service elsewhere
if not ((a.window or ((FROZEN or sys.platform == "win32") and not a.no_window)) and run_window(httpd)):
    httpd.serve_forever()
