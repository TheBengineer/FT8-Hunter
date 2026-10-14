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
               "cq_tries": 3, "tail_tries": 2, "hold_min": 10,
               "idle_every": 4, "idle_secs": 60, "special_first": True,
               "call_cq": True, "cq_secs": 120,
               "auto_df": True, "df_lo": 300, "df_hi": 2700,
               "qrm_detect": True, "qrm_backoff_min": 5, "min_snr": -20},
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

def pack_configure(id_, dx_call=" ", dx_grid=" ", generate=True):
    """Type 15 Configure: blank dx_call + generate=True resets WS message
    stack to its default CQ (empty string is ignored upstream, so send a space).
    Other fields = no-change sentinels."""
    M = 0xFFFFFFFF
    pkt = struct.pack(">II", 0xADBCCBDA, 2) + struct.pack(">I", 15) + pack_str(id_)
    pkt += pack_str("") + struct.pack(">I", M) + pack_str("")
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
            mt = (re.search(r"<time_on:\d+>(\d{4,6})", rec, re.I)
                  or re.search(r"<time_off:\d+>(\d{4,6})", rec, re.I))
            ts = None
            if md and mt:
                ts = datetime.datetime.strptime(md.group(1) + mt.group(1)[:4], "%Y%m%d%H%M").replace(
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
       "qrm_first": None,
       "tx_iv": deque(maxlen=50),          # our transmissions [on, off] (epoch s)
       "qrm_until": 0, "qrm_n": 0, "qrm_why": "",
       "active": {},    # base call -> last time heard sending to someone (0 = signed off)
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
                    set_band(band_of(struct.unpack(">Q", data[off:off+8])[0]))
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
                       "df": params["df"]})
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
    log(f"mode {old} -> {new}: {period()} s periods")

def set_band(new):
    """WS moved to another band: same reset as a mode change."""
    old = state["udp"].get("band")
    state["udp"]["band"] = new
    if old is None or old == new or new is None:
        return
    new_slot()
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
    for k in ("callers", "occ", "noise_hist", "spec_pending"):
        ENG[k].clear()
    for k in ("spec", "active", "calls_to"):
        ENG[k].clear()
    ENG["cq_until"] = 0

def occupied(par, now):
    """Occupied audio intervals [lo, hi) on one T/R parity: recent decodes plus,
    on the ws-hunter build, spectrum bins well above the noise floor."""
    iv = [(o["df"] - GUARD, o["df"] + bw() + GUARD) for o in list(ENG["occ"])
          if o["par"] == par and now - o["t"] < 120]
    sp = ENG["spec"].get(par)
    if sp and now - sp["t"] < 120:
        iv += [(f - GUARD, f + sp["bin"] + GUARD) for f in sp["hot"]]
    return iv

def pick_tx_df(par, current=None):
    """Clearest Tx offset on parity `par`: the spot furthest from any occupied
    interval (and the band edges). Keeps `current` while it stays clear."""
    cfg = state["config"]
    lo, hi = cfg.get("df_lo", 300), cfg.get("df_hi", 2700)
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
    return best if room(best) >= 0 else None

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
    """Did any of our transmissions overlap this period (+ audio latency)?"""
    end = t_ms + tr_ms
    for on, off in list(ENG["tx_iv"]):
        a, b = tod_ms(on) - margin_ms, tod_ms(off if off else time.time()) + margin_ms
        if a < end and b > t_ms:
            return True
    return False

def handle_spectrum(data, off):
    """ws-hunter SpectrumSummary: period QTime ms, T/R ms, start Hz, bin Hz,
    floor dB, step dB, levels. Marks hot bins for the offset picker right
    away; the busy-frequency check waits for that period's decodes."""
    t_ms, tr_ms, start, bin_hz = struct.unpack(">IIII", data[off:off+16])
    floor, step = struct.unpack(">dd", data[off+16:off+32])
    n = struct.unpack(">I", data[off+32:off+36])[0]
    levels = data[off+36:off+36+n]
    db = [floor + v * step for v in levels]
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
    hot = [f for f, d in zip(freqs, db) if d > noise + 8]
    now, par = time.time(), int(t_ms // max(tr_ms, 1)) % 2
    ENG["spec"][par] = {"t": now, "bin": bin_hz, "hot": hot, "noise": noise}
    ENG["spec_pending"].append({"t": now, "t_ms": t_ms, "tr_ms": tr_ms, "par": par, "bin": bin_hz,
                                "freqs": freqs, "db": db, "noise": noise,
                                "warm": len(ENG["noise_hist"]) >= 8})
    check_qrm(now)

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

def fresh_callers(now=None):
    """Queued callers still worth answering, oldest first. Drops anyone worked,
    held, or not heard for 6 periods (they have given up)."""
    now = now or time.time()
    for c in list(ENG["callers"]):
        if is_worked(c["call"]) or on_hold(c["call"]) or now - c["t"] > cyc(6):
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
    t = state["target"]
    cfg = state["config"]
    if t and is_worked(t["call"]):  # logged (seen in ADIF) without a QSO Logged packet
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
    cands = [e for e in list(state["cq"].values()) if not cq_skip(e, now)]
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
    for m in list(state["msgs"]):
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
    if cfg.get("call_cq", True):  # last resort: call CQ ourselves
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
                t["resp"] = True
                t["done"] = t.get("done") or signoff
                t["left"] = 2 if signoff else 99
                log(f"reply: {msg}")
            elif state["running"] and not signoff and not is_worked(frm):
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
                    entry = {"call": frm, "msg": msg, "t": now}
                    if late:
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
MAX_W = {"Xiegu-G90": 20, "Xiegu-X6100": 10, "IC-705": 10, "FT-818": 6, "FT-817": 5}  # rig.get_maxpwr often says 0

def flrig():
    import xmlrpc.client
    class T(xmlrpc.client.Transport):
        def make_connection(self, host):
            c = super().make_connection(host); c.timeout = 3; return c
    return xmlrpc.client.ServerProxy(RIG["url"], transport=T())  # one per call: not thread-safe

def rig_poll():
    import xmlrpc.client
    while True:
        try:
            if not RIG["ok"]:
                fl = flrig()
                RIG["xcvr"] = str(fl.rig.get_xcvr())
                RIG["max"] = int(float(fl.rig.get_maxpwr() or 0)) or MAX_W.get(RIG["xcvr"], 100)
            m = xmlrpc.client.MultiCall(flrig())
            m.rig.get_ptt(); m.rig.get_power(); m.rig.get_smeter()
            ptt, power, sm = list(m())
            # (the G90's power and SWR meters over CAT don't track reality, so they aren't shown)
            RIG.update(ok=True, err="", t=time.time(), ptt=bool(int(ptt)), power=int(float(power)), s=float(sm))
            if float(sm) > 0:
                RIG["has_s"] = True  # some rigs (the G90) never report an S-meter over CAT
        except Exception as e:
            if RIG["ok"] or not RIG["err"]:
                log(f"FLRig: {e}")
            RIG.update(ok=False, err=str(e)[:80])
        time.sleep(1 if RIG.get("ptt") else 2)

def rx_level(now):
    """Incoming signal, from WS: the strongest decode of the last period."""
    snrs = [m["snr"] for m in list(state["msgs"]) if now - m["seen"] < cyc(1) + 3]
    return {"best": max(snrs) if snrs else None, "n": len(snrs)}

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
           "resp": bool(t.get("resp")) or t.get("why") == "called us"} if t else None
    traffic = []
    for p in sorted(list(PAIRS.values()), key=lambda p: -p["t"]):
        age = now - p["t"]
        if age > TRAFFIC_SECS or len(traffic) >= 150:
            continue
        a, b = (me if is_me(c) else locate(c) for c in (p["a"], p["b"]))
        if a and b and a != b:
            traffic.append({"from": p["a"], "to": p["b"], "a": a, "b": b, "age": round(age),
                            "tome": is_me(p["b"])})
    return {"me": me, "mycall": state["mycall"], "worked": worked[:300], "target": tgt,
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
                "rig": {k: v for k, v in RIG.items() if k != "url"}, "rx": rx_level(now),
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
                w = max(1, min(int(body.get("watts", 0)), RIG.get("max", 100)))
                flrig().rig.set_power(w)
                RIG["power"] = w
                log(f"TX power set to {w} W")
            except Exception as e:
                return self._json({"error": str(e)[:80]}, 502)
        elif p == "/api/config":
            state["config"].update({k: v for k, v in body.items() if k in state["config"]})
        else: return self._json({"error": "unknown"}, 404)
        self._json({"ok": True})
    def log_message(self, *a): pass

ap = argparse.ArgumentParser()
ap.add_argument("--mycall", default="KR4MZM"); ap.add_argument("--mygrid", default="EM84")
ap.add_argument("--port", type=int, default=2237); ap.add_argument("--http", type=int, default=8080)
ap.add_argument("--flrig", default="http://127.0.0.1:12345", help="FLRig XML-RPC URL (power + meters)")
a = ap.parse_args()
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
log(f"hunter ready as {state['mycall']} {state['mygrid']}")
print(f"open http://0.0.0.0:{a.http}/")
ThreadingHTTPServer(("0.0.0.0", a.http), H).serve_forever()
