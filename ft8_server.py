#!/usr/bin/env python3
"""FT8 hunter daemon: UDP watcher + stdlib JSON API + serves webapp. No deps.
Run: python3 ft8_server.py --mycall KR4MZM --mygrid EM84 --port 2237 --http 8080
API: GET /api/state -> {running, target, cq[], answering[], worked[], log[], config, stats}
     POST /api/start|stop|call|skip|config   Webapp: GET /
"""
import argparse, json, socket, struct, threading, time, re, os, math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
from urllib.parse import urlparse

state = {
    "running": False, "mycall": "", "mygrid": "",
    "target": None, "cq": {}, "answering": {},
    "worked": {}, "log": deque(maxlen=50),
    "msgs": deque(maxlen=80), "mytx": deque(maxlen=20),
    "config": {"max_cycles": 3, "cooldown": 2, "cq_only": True,
               "cq_tries": 3, "tail_tries": 2, "qso_tries": 7, "hold_min": 10,
               "idle_every": 4, "idle_secs": 60, "special_first": True,
               "call_cq": True, "cq_secs": 120, "cq_first": False,
               "auto_df": True, "df_lo": 1300, "df_hi": 2700,  # df_lo: rig feeds back below 1300 Hz
               "qrm_detect": True, "qrm_backoff_min": 5, "min_snr": -20,
               "watts_call": 10, "watts_qso": 20,
               "tune_new_band": True, "tune_below_khz": 1,  # ATU tune this far under FT8 after a band change
               "scan_modes": ["FT8", "FT4", "FT2"],  # band scan: what to visit
               "scan_bands": ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m"]},  # TX power held on the radio: calling / once they answer
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
    m = re.match(r"(\d{6})-hunter", state["udp"].get("revision", ""))
    return bool(m) and m.group(1) >= since

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
    """Enable Tx: a UDP command on the ws-hunter build, else a click."""
    if has_ws_hunter():
        CTX["tx"].sendto(pack_set_bool(SET_TX_ENABLED, state["ws_id"], True), ws_dest())
        return "Enable Tx via UDP"
    return arm_tx()

def alltxt_time_ms(tstr):
    # "261009_221230" -> ms since midnight (decode times fall on whole seconds)
    hh, mm, ss = int(tstr[7:9]), int(tstr[9:11]), int(tstr[11:13])
    return ((hh * 60 + mm) * 60 + ss) * 1000

def udp_holder(port=2237):
    """Who holds local UDP port? Returns process name or 'free'."""
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
TRAFFIC_SECS = 300  # traffic lines fade out over this long
RECENT = {}  # (call, band, mode) -> epoch time, last 24 h: the globe keeps them past UTC midnight
GRID_RE = re.compile(r"^[A-R]{2}\d{2}([A-X]{2})?$")
CTY_PATHS = ("~/ws-hunter-app/share/ws/cty.dat", "~/ws/usr/share/ws/cty.dat",
             "~/ws-wide/usr/share/ws/cty.dat", "/usr/share/wsjtx/cty.dat")

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
    paths = [os.path.expanduser("~/.local/share/WS/wsjtx_log.adi"),
             os.path.expanduser("~/.local/share/WSJT-X/wsjtx_log.adi"),
             os.path.expanduser("~/.wsjtx/wsjtx_log.adi")]
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

state["fwd_port"] = 2238  # passthrough port: hunter rebroadcasts WS packets here for WRL Desktop
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
        except Exception as e:
            log_err("WRL relay", e)
            time.sleep(1)

def udp_listener():
    """Passive REUSEPORT listener: learns WS id, tracks Status/TX state."""
    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fwd.bind(("127.0.0.1", 0))  # fixed source port, so WRL's answers come back here
    threading.Thread(target=wrl_relay, args=(fwd,), daemon=True).start()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
                    fwd.sendto(data, ("127.0.0.1", state["fwd_port"]))
                elif not state.get("ws_addr"):
                    fwd.sendto(data, ("127.0.0.1", state["fwd_port"]))
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
                        t = state["target"]
                        if t and same_call(t["call"], dxcall):
                            log(f"logged QSO with {dxcall} — nice!")
                            state["target"] = None
                        else:
                            log(f"logged QSO with {dxcall}")
                except Exception as e:
                    log_err("QSO Logged parse", e)
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
                                for _ in range(3):
                                    _, q = read_qstr(data, q)
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
    for cand in ("~/.local/share/WS/ALL.TXT", "~/.local/share/WSJT-X/ALL.TXT"):
        cand = os.path.expanduser(cand)
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

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hunter_config.json")
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
    for k in [k for k, p in list(PAIRS.items()) if now - p["t"] > TRAFFIC_SECS]:
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

def pick_tx_df(par, current=None):
    """Clearest Tx offset on parity `par`: the spot furthest from any occupied
    interval (and the band edges). Keeps `current` while it stays clear."""
    cfg = state["config"]
    lo, hi = cfg.get("df_lo", 1300), cfg.get("df_hi", 2700)
    iv = occupied(par, time.time())
    def room(f):  # Hz of clearance around [f, f + bw()]; negative = overlap
        d = min(f - lo, hi - (f + bw()))
        for a, b in iv:
            if f + bw() > a and f < b:
                return -1
            d = min(d, a - (f + bw()) if a >= f + bw() else f - b)
        return d
    if current and room(current) >= 0:
        return current
    best = max(range(lo, hi - bw() + 1, 10), key=lambda f: (room(f), -abs(f - (current or 1500))))
    if room(best) >= 0:
        return best
    # nowhere clear: stay put, unless we are outside the allowed range (below
    # df_lo the rig feeds back), then take the least crowded spot inside it
    return best if current is not None and not lo <= current <= hi - bw() else None

def tune_tx(par):
    """Before transmitting on parity `par`: hold Tx freq (so a Reply can't drag
    us onto the DX) and move to a clear offset. Needs the ws-hunter build;
    on stock WS the pick is only shown in the UI."""
    cur = state["udp"].get("tx_df")
    f = pick_tx_df(par, cur)
    ENG["suggest"] = {"par": par, "df": f}
    if not (has_ws_hunter() and state["config"].get("auto_df", True)):
        return
    tx, wid = CTX["tx"], state["ws_id"]
    tx.sendto(pack_set_bool(SET_HOLD_TX_FREQ, wid, True), ws_dest())
    if f and f != cur:
        tx.sendto(pack_set_u32(SET_TX_DF, wid, f), ws_dest())
        log(f"Tx offset {cur} -> {f} Hz ({'even' if par == 0 else 'odd'})")

def tod_ms(ts):
    """UTC time of day in ms, the clock WS uses for period start times."""
    return int(ts * 1000) % 86400000

def note_tx(txing):
    """Record our transmit intervals from Status, so spectra of periods we
    transmitted in (the rig mutes its receive audio) are never judged."""
    now, iv = time.time(), ENG["tx_iv"]
    if txing and not (iv and iv[-1][1] is None):
        iv.append([now, None])
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
            "learned": len(by_t) >= 8, "ws_hunter": has_ws_hunter()}

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
    """A station we just logged repeating its R-report never copied our RR73:
    without another one the QSO is missing from their log. Worth answering
    within 4 minutes of the contact, at most 3 more times."""
    if not (rest and re.fullmatch(r"R[+-]\d\d", rest[-1])):
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

def engine_tick():
    """Called once per T/R period. Priority: our target > callers-to-me >
    activator CQs > other CQs > tail-end > our own CQ."""
    now = time.time()
    # refresh today's worked if a log file changed
    try:
        for p, mt in list(state.get("adi_mtime", {}).items()):
            if os.path.exists(p) and os.path.getmtime(p) != mt:
                state["worked"], state["adi_mtime"] = load_worked()
                break
    except Exception as e:
        log_err("ADIF reload", e)
    prune(now)
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
            waiting = fresh_callers(now)
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
            state["target"] = None
        else:
            ENG["busy"] += 1
            # re-arm every cycle until they reply (suite treats Reply as one-shot);
            # once QSO progresses (resp), WS auto-sequences — don't touch it
            if not t.get("resp") and t.get("msg"):
                send_exact_reply(CTX["tx"], t["msg"])
            how = ("signing off" if t.get("done") else "in QSO") if t.get("resp") \
                else f"calling, {(t['left'] + 1) // 2} tries left"
            return at("target", f"{t['call']}: {how}")
    check_qrm(now)  # spectra wait for their decodes; we may transmit meanwhile
    # stations calling us, first-come-first-serve, working through all of them
    waiting = fresh_callers(now)
    while waiting:
        c = waiting.pop(0)
        ENG["callers"].remove(c)
        if c.get("again"):
            if acquire(c["call"], "", c["msg"], tries=1, why="RR73 again"):
                state["target"]["again"] = True
                k = base_call(c["call"])
                ENG["rr73_again"][k] = ENG["rr73_again"].get(k, 0) + 1
                return at("caller", c["call"] + " (RR73 again)")
            continue
        if acquire(c["call"], "", c["msg"], tries=cfg.get("cq_tries", 3), why="called us"):
            ENG["busy"] += 1
            more = f" ({len(waiting)} more waiting)" if waiting else ""
            return at("caller", c["call"] + more)
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
                   why=e["tag"] if e.get("special") else "CQ"):
            ENG["busy"] += 1
            special = e.get("special") and cfg.get("special_first", True)
            return at("activator" if special else "cq", f"{e['call']} ({e['snr']:+d})")
    if now < ENG["cq_until"]:
        # our own CQ keeps running; WS repeats it each period
        cq_check(now)
        return at("mycq", f"{round(ENG['cq_until'] - now)} s left")
    # no CQ: tail-end a station signing off, never one mid-QSO. A Reply to
    # "A B ..." makes WS call B, the sender. RR73/RRR usually comes from the
    # station running the frequency, who is about to be free; prefer those.
    best = None
    for m in ([] if cq_first else list(state["msgs"])):
        if now - m.get("seen", 0) >= cyc(2):
            continue
        to, frm, rest = addressing(m["msg"])
        if not frm or "." in frm or is_me(to) or is_me(frm):
            continue
        if not rest or rest[-1] not in SIGNOFF:
            continue
        if is_worked(frm) or on_hold(frm) or m["snr"] < cfg.get("min_snr", -20):
            continue
        key = (rest[-1] == "73", -m["snr"])
        if best is None or key < best[2]:
            best = (m, frm, key)
    if best and acquire(best[1], "", best[0]["msg"], tries=cfg.get("tail_tries", 2), why="tail-end"):
        ENG["busy"] += 1
        return at("tail", best[1])
    ENG["busy"] = 0
    if ENG["cq_until"]:  # our CQ run just ended: stop and listen before the next
        ENG["cq_until"] = 0
        halt_ws()
        ENG["idle_until"] = now + cfg.get("idle_secs", 60)
        log(f"CQ run over, listening {cfg.get('idle_secs', 60)}s")
        return at("cq_end", f"listening {cfg.get('idle_secs', 60)} s")
    if cq_first or cfg.get("call_cq", True):  # last resort (or first): call CQ ourselves
        start_cq(cfg.get("cq_secs", 120))
        return at("cq_start", f"{cfg.get('cq_secs', 120)} s run")
    # spare cycle: every idle_every idle cycles, rest to catch the off sequence
    ENG["idle_n"] += 1
    if ENG["idle_n"] % max(1, cfg.get("idle_every", 4)) == 0:
        ENG["idle_until"] = now + cfg.get("idle_secs", 60)
        log(f"listening {cfg.get('idle_secs', 60)}s (off-sequence watch)")
    return at("idle", "CQ calling is off")

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
        tune_tx(par)
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

def acquire(call, grid, msg, tries, why):
    p = state["decparams"].get(msg)
    extra = 0
    if p:
        tx_par = 1 - parity(p["t_ms"])  # we answer in the other period
        tune_tx(tx_par)
        # Starting in their period, our first call goes out a period later;
        # allow for it so their answer to our last call is decoded before we
        # give up on them.
        extra = 1 if parity(tod_ms(time.time())) != tx_par else 0
    if not send_exact_reply(CTX["tx"], msg):
        return False
    # `left` counts T/R periods; we transmit in every other one
    state["target"] = {"call": call, "grid": grid, "left": tries * 2 + extra, "why": why,
                       "msg": msg, "resp": False}
    ENG["cq_until"] = 0  # a Reply replaces our CQ in WS
    state["stats"]["calls_made"] += 1
    log(f"calling {call} {grid} ({why}, {tries} tries)")
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
                t["resp"] = True
                t["done"] = t.get("done") or signoff
                # each reply buys qso_tries more of our transmissions (+1 period
                # to decode their answer to the last); silence after that: give up
                t["left"] = 2 if signoff else state["config"].get("qso_tries", 7) * 2 + 1
                log(f"reply: {msg}")
            elif state["running"] and not signoff and (not is_worked(frm) or (again := rr73_again(frm, rest, now))):
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
                        log(f"{frm} didn't copy our RR73, sending it again: {msg}")
                    elif late:
                        ENG["callers"].appendleft(entry)
                        log(f"late reply, answering next: {msg}")
                    else:
                        ENG["callers"].append(entry)
                        log(f"caller queued: {msg}")
                    if t and not t.get("resp"):  # never abandon a QSO in progress
                        halt_ws()
                        log(f"dropping {tgt} for caller {frm}")
                        state["target"] = None
        state["answering"][msg] = {"msg": msg, "snr": snr, "tome": to_me,
            "from": frm or msg, "seen": now, "t": time.strftime("%H:%M:%S")}

# Radio via FLRig's XML-RPC server: TX power control, PTT and S-meter.
# One multicall per poll (FLRig answers in ~0.5 s, serialised with its CAT polling).
RIG = {"ok": False, "url": "http://127.0.0.1:12345", "err": ""}
RIG_WAKE = threading.Event()  # poll now: e.g. the power for this QSO stage changed
MAX_W = {"Xiegu-G90": 20, "Xiegu-X6100": 10, "IC-705": 10, "FT-818": 6, "FT-817": 5}  # rig.get_maxpwr often says 0

def flrig():
    import xmlrpc.client
    class T(xmlrpc.client.Transport):
        def make_connection(self, host):
            c = super().make_connection(host); c.timeout = 3; return c
    return xmlrpc.client.ServerProxy(RIG["url"], transport=T())  # one per call: not thread-safe

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

def rig_poll():
    import xmlrpc.client
    while True:
        try:
            if not RIG["ok"]:
                fl = flrig()
                RIG["xcvr"] = str(fl.rig.get_xcvr())
                RIG["max"] = int(float(fl.rig.get_maxpwr() or 0)) or MAX_W.get(RIG["xcvr"], 100)
            m = xmlrpc.client.MultiCall(flrig())
            m.rig.get_ptt(); m.rig.get_power(); m.rig.get_smeter(); m.rig.get_pwrmeter()
            ptt, power, sm, out = list(m())
            # (the G90's SWR meter over CAT doesn't track reality, so it isn't shown;
            # its power meter is coarse, full or nothing, but shows we put out power)
            ptt = bool(int(ptt))
            RIG.update(ok=True, err="", t=time.time(), ptt=ptt, s=float(sm),
                       out=max(0.0, float(out or 0)) if ptt else 0.0)
            if RIG.get("xcvr") not in CIV:
                RIG["power"] = int(float(power))
            hold_power(flrig(), time.time())
            if float(sm) > 0:
                RIG["has_s"] = True  # some rigs (the G90) never report an S-meter over CAT
        except Exception as e:
            if RIG["ok"] or not RIG["err"]:
                log(f"FLRig: {e}")
            RIG.update(ok=False, err=str(e)[:80])
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
        if age > TRAFFIC_SECS or len(traffic) >= 150:
            continue
        a, b = (me if is_me(c) else locate(c) for c in (p["a"], p["b"]))
        if a and b and a != b:
            traffic.append({"from": p["a"], "to": p["b"], "a": a, "b": b, "age": round(age),
                            "tome": is_me(p["b"])})
    return {"me": me, "mycall": state["mycall"], "worked": worked[:300], "target": tgt, "callers": callers,
            "traffic": traffic, "fade": TRAFFIC_SECS}

def arm_tx(only_if_off=False):
    """Click WS 'Enable Tx' via xdotool (no UDP message exists for it).
    Position computed from live window geometry (calibrated 2026-10-10;
    re-calibrate if the WS layout/splitter moves: button frac ≈ (0.737, 0.407))."""
    import subprocess
    xd = os.path.expanduser("~/xdotool/usr/bin/xdotool")
    lib = os.path.expanduser("~/xdotool/usr/lib/x86_64-linux-gnu")
    env = dict(os.environ, LD_LIBRARY_PATH=lib + ":" + os.environ.get("LD_LIBRARY_PATH", ""))
    try:
        out = subprocess.run([xd, "search", "--onlyvisible", "--name", "Digital Mode Suite"],
                             capture_output=True, text=True, env=env, timeout=10).stdout.split()
        if not out:
            return "WS window not found"
        wid = out[0]
        g = subprocess.run([xd, "getwindowgeometry", wid], capture_output=True,
                           text=True, env=env, timeout=10).stdout
        pos = re.search(r"Position:\s*(\d+),(\d+)", g)
        geo = re.search(r"Geometry:\s*(\d+)x(\d+)", g)
        if not pos or not geo:
            return "geometry parse failed"
        x, y = int(pos.group(1)), int(pos.group(2))
        w, h = int(geo.group(1)), int(geo.group(2))
        bx, by = x + int(w * 0.737), y + int(h * 0.407)
        if only_if_off:
            # suite's Status TxEnabled bit is unreliable; treat recent TX as armed
            recent_tx = any(time.time() - m.get("seen", 0) < 90 for m in state["mytx"])
            if state["udp"].get("tx_enabled") or recent_tx:
                return "already armed"
        subprocess.run([xd, "windowraise", wid], env=env, timeout=10)
        subprocess.run([xd, "windowfocus", wid], env=env, timeout=10)
        time.sleep(1.0)
        subprocess.run([xd, "mousemove", str(bx), str(by), "click", "1"],
                       env=env, timeout=10)
        return f"clicked {bx},{by}"
    except Exception as e:
        return f"arm failed: {e}"

# --- Band scan: visit the published FT8/FT4/FT2 dial frequencies and take
# one clean ws-hunter spectrum on each, to see which bands are busy. WS runs
# FT2 meanwhile (3.75 s periods, so a spectrum every 3.75 s); the period in
# which the rig retuned mixes two bands and is thrown away.
SET_DIAL = 20
SCAN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_results.json")
FREQ_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_freqs.json")
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

LOG_LAST = {"mt": None, "last": {}}
def log_last():
    """{(band, mode): (last QSO epoch, QSOs in the last 24 h)} over the whole log."""
    import datetime
    paths = [p for p in (os.path.expanduser("~/.local/share/WS/wsjtx_log.adi"),
                         os.path.expanduser("~/.local/share/WSJT-X/wsjtx_log.adi")) if os.path.exists(p)]
    mt = tuple(os.path.getmtime(p) for p in paths)
    if mt == LOG_LAST["mt"]:
        return LOG_LAST["last"]
    last, now = {}, time.time()
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
            t0, n = last.get(k, (0, 0))
            last[k] = (max(t0, ts), n + (now - ts < 86400))
    LOG_LAST.update(mt=mt, last=last)
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
    now, last = time.time(), log_last()
    rows = []
    for key, r in list(SCAN["rows"].items()):
        md = key.split()[1]
        lt, n24 = last.get((r["band"], md), (0, 0))
        rows.append({k: v for k, v in r.items() if k != "modes" and k[0] != "_"} | {
            "mode": md, "age": round(now - r["t"]), "worked_ago": round(now - lt) if lt else None,
            "worked_24h": n24, "here": r["hz"] == state["udp"].get("dial") and md == mode_raw()})
    rows.sort(key=lambda r: (r["hz"], r["mode"]))
    cur = SCAN["cur"]
    return {"on": SCAN["on"], "i": SCAN["i"], "n": SCAN["n"], "err": SCAN["err"],
            "cur": cur and {"band": cur["band"], "modes": cur["modes"], "hz": cur["hz"]},
            "eta": round((SCAN["n"] - SCAN["i"]) * 2 * MODES["FT2"][0]) if SCAN["on"] else 0,
            "src": SCAN["freqs"]["src"], "freqs": [list(x) for x in SCAN["freqs"]["list"]],
            "ws_hunter": has_ws_hunter(), "rows": rows}

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
TUNE_WAIT = 20  # s the tuner may take
NEW_BAND_CYCLES = 2  # full cycles (an even and an odd period each) heard on a new band before transmitting
def new_band_wait(now):
    """Seconds still to listen after arriving on a band (or tuning there):
    until NEW_BAND_CYCLES whole cycles have been heard, so the offset
    picker has the last two periods of each parity before we transmit."""
    t = ENG.get("arrived")
    if not t:
        return 0
    per = period()
    first = math.ceil(t / per) * per  # the first whole period after we got here
    return first + 2 * NEW_BAND_CYCLES * per - now
def need_tune():
    b = band()
    return bool(state["config"].get("tune_new_band") and has_ws_hunter() and b and ENG.get("tuned_band")
                and b != ENG["tuned_band"] and not SCAN["on"] and RIG.get("xcvr") in CIV)

def start_tune():
    if ENG.get("tuning"):
        return
    ENG["tuning"] = "starting"
    threading.Thread(target=atu_tune, daemon=True).start()

def tune_freq(b):
    """Where to tune on band b: tune_below_khz under its FT8 dial frequency."""
    ft8 = next((khz for khz, m in SCAN["freqs"]["list"] if m == "FT8" and band_of(khz * 1000) == b), None)
    return round((ft8 - state["config"].get("tune_below_khz", 1)) * 1000) if ft8 else None

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
        hz = tune_freq(b)
        if not (hz and op):
            log(f"antenna tune: no FT8 frequency known for {b}; not tuning")
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
        end, st = time.time() + TUNE_WAIT, None
        while time.time() < end:
            st = tuner_state(fl)
            if st is not None and st != 2:
                break
            time.sleep(0.5)
        log(f"antenna tuned on {b} at {hz / 1e6:.4f} MHz ({round(time.time() - t0)} s)" if st is not None and st != 2
            else f"antenna tune on {b}: no word from the tuner after {TUNE_WAIT} s")
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

class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path == "/":
            try:
                b = open(os.path.join(os.path.dirname(__file__), "ft8_app.html"), "rb").read()
                self.send_response(200); self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b))); self.end_headers()
                self.wfile.write(b); return
            except FileNotFoundError:
                self.send_error(404); return
        if self.path == "/nosleep.js":  # NoSleep.js (MIT), vendored: phones on the LAN may be offline
            b = open(os.path.join(os.path.dirname(__file__), "nosleep.min.js"), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b); return
        if self.path == "/api/globe":
            return self._json(globe())
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
                "answering": ans[:80], "now": now, "worked": worked, "book": book[:200], "band": band(),
                "msgs": msgs, "mytx": list(state["mytx"])[:15], "dxfocus": dxf,
                "holds": holds, "callers": [c["call"] for c in list(ENG["callers"])],
                "parity": {"even": par[0], "odd": par[1],
                           "hot": ("even" if par[0] >= par[1] else "odd")},
                "idle": max(0, round(ENG["idle_until"] - time.time())),
                "cq_calling": max(0, round(ENG["cq_until"] - time.time())),
                "mode": mode(), "period": period(),
                "rig": {**{k: v for k, v in RIG.items() if k != "url"}, "qso": answered()}, "rx": rx_level(now),
                "step": ENG.get("step") or {"step": "stopped" if not state["running"] else "idle"},
                "qrm": {"secs": max(0, round(ENG["qrm_until"] - time.time())), "why": ENG["qrm_why"]},
                "txdf": {"current": u.get("tx_df"), **ENG.get("suggest", {})},
                "log": list(state["log"]), "config": state["config"], "stats": state["stats"],
                "udp": {"holder": u["holder"], "ws_id": state["ws_id"],
                        "revision": u.get("revision", ""), "ws_hunter": has_ws_hunter(),
                        "dxcall": u["dxcall"], "rx_count": u["rx_count"],
                        "tx_enabled": u.get("tx_enabled", False),
                        "tx_msg": u.get("tx_msg", ""), "watchdog": u.get("watchdog", False),
                        "transmitting": u.get("transmitting", False) if u.get("tx_t", 0) and time.time() - u.get("tx_t", 0) < 20 else False,
                        "by_type": u.get("by_type", {}),
                        "fwd_port": state.get("fwd_port", 2238),
                        "wrl_by_type": u.get("wrl_by_type", {}),
                        "heard_ago": round(time.time() - u["last_rx"], 1) if u["last_rx"] else None}})
        self.send_error(404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        p = urlparse(self.path).path
        if p == "/api/start": state["running"] = True; log("hunter started"); at("idle", "starting at next period")
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
                if has_ws_hunter():
                    how = enable_tx()
                else:
                    time.sleep(2)  # let Status report back before toggle check
                    how = arm_tx(only_if_off=True)
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
            # manual Tx offset from the waterfall; the auto picker keeps it while it stays clear
            if not has_ws_hunter():
                return self._json({"error": "needs the ws-hunter build of WS"}, 409)
            f = max(200, min(int(body.get("df", 0)), 3000 - bw()))
            tx, wid = CTX["tx"], state["ws_id"]
            tx.sendto(pack_set_bool(SET_HOLD_TX_FREQ, wid, True), ws_dest())
            tx.sendto(pack_set_u32(SET_TX_DF, wid, f), ws_dest())
            state["udp"]["tx_df"] = f
            log(f"Tx offset set to {f} Hz from the waterfall")
        elif p == "/api/config":
            state["config"].update({k: v for k, v in body.items() if k in state["config"]})
            c = state["config"]  # keep the offset range usable: 200 Hz up, room for a signal
            c["df_lo"] = max(200, min(int(c["df_lo"]), int(c["df_hi"]) - 300))
            for k in ("watts_call", "watts_qso"):
                c[k] = max(1, min(int(c[k]), RIG.get("max", 100)))
            c["tune_below_khz"] = max(0.5, min(float(c["tune_below_khz"]), 10.0))  # stay near the band's FT8
            save_config()
            RIG_WAKE.set()  # new power applies now
            if "cq_first" in body:
                log("CQ first mode " + ("on: running our own CQ" if c["cq_first"] else "off: hunting"))
        else: return self._json({"error": "unknown"}, 404)
        self._json({"ok": True})
    def log_message(self, *a): pass

ap = argparse.ArgumentParser()
ap.add_argument("--mycall", default="KR4MZM"); ap.add_argument("--mygrid", default="EM84")
ap.add_argument("--port", type=int, default=2237); ap.add_argument("--http", type=int, default=8080)
ap.add_argument("--flrig", default="http://127.0.0.1:12345", help="FLRig XML-RPC URL (power + meters)")
a = ap.parse_args()
load_config()
state["mycall"] = a.mycall.upper(); state["mygrid"] = a.mygrid.upper(); udp_port = a.port
RIG["url"] = a.flrig
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
threading.Thread(target=load_freqs, daemon=True).start()
log(f"hunter ready as {state['mycall']} {state['mygrid']}")
print(f"open http://0.0.0.0:{a.http}/")
ThreadingHTTPServer(("0.0.0.0", a.http), H).serve_forever()
