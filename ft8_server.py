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
    "worked": set(), "log": deque(maxlen=50),
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
SET_TX_PERIOD, SET_TX_DF, SET_HOLD_TX_FREQ, SET_TX_ENABLED, SPECTRUM = 19, 24, 25, 26, 27
def pack_set_bool(mtype, id_, value):
    """ws-hunter station-control message carrying one bool."""
    return struct.pack(">III", 0xADBCCBDA, 2, mtype) + pack_str(id_) + struct.pack(">B", 1 if value else 0)

def pack_set_u32(mtype, id_, value):
    return struct.pack(">III", 0xADBCCBDA, 2, mtype) + pack_str(id_) + struct.pack(">I", value)

def has_ws_hunter():
    """True when WS reports the ws-hunter build in its Heartbeat revision."""
    return "hunter" in state["udp"].get("revision", "")

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

CTY = {"exact": {}, "prefix": {}}  # call/prefix -> (country, continent)
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
                (CTY["exact"] if m.group(1) else CTY["prefix"])[m.group(2)] = (f[0], c.group(1) if c else f[3])
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
    """Only TODAY's contacts count (UTC date, as ADIF records qso_date).
    Returns (set, {path: mtime})."""
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    paths = [os.path.expanduser("~/.local/share/WS/wsjtx_log.adi"),
             os.path.expanduser("~/.local/share/WSJT-X/wsjtx_log.adi"),
             os.path.expanduser("~/.wsjtx/wsjtx_log.adi")]
    w, mtimes = set(), {}
    for p in paths:
        if not os.path.exists(p):
            continue
        mtimes[p] = os.path.getmtime(p)
        txt = open(p, errors="ignore").read()
        # split into records, keep calls with qso_date == today
        for rec in txt.split("<eor"):
            md = re.search(r"<qso_date:\d+>(\d{8})", rec)
            if md and md.group(1) != today:
                continue
            mc = re.search(r"<call:\d+>([A-Za-z0-9/]+)", rec)
            if mc:
                w.add(mc.group(1).upper())
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
# Targeting engine: holds {call: until_ts}, callers queue [(call,msg)], parity {0:n,1:n},
# idle_until ts (listen-only rest), busy_cycles (consecutive cycles with a target)
ENG = {"holds": {}, "callers": deque(maxlen=20), "parity": {0: 0, 1: 0},
       "idle_until": 0, "busy": 0, "idle_n": 0,
       "cq_until": 0,  # calling our own CQ until this time (0 = not calling)
       "occ": deque(maxlen=800),  # recent decodes {t, par, df}: who is where
       "spec": {},                # parity -> latest ws-hunter spectrum summary
       "noise_hist": deque(maxlen=40),  # per-period noise estimates (dB)
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
                    if dxcall:
                        state["worked"].add(dxcall)
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
                        state["udp"]["transmitting"] = bool(data[p]); p += 1
                        state["udp"]["tx_t"] = time.time()
                        if p + 9 <= len(data):  # Decoding bool, Rx DF u32, Tx DF u32
                            state["udp"]["rx_df"], state["udp"]["tx_df"] = struct.unpack(">II", data[p+1:p+9])
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
    ENG["occ"].append({"t": time.time(), "par": parity(params["t_ms"]), "df": params["df"]})
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

def ws_dest():
    return state["ws_addr"] or ("127.0.0.1", udp_port)

def halt_ws():
    try:
        CTX["tx"].sendto(pack_halt(state["ws_id"]), ws_dest())
    except Exception:
        pass

def on_hold(call):
    return ENG["holds"].get(base_call(call), 0) > time.time()

def hold(call):
    """Hold a call for hold_min minutes; returns the minutes."""
    mins = state["config"].get("hold_min", 10)
    ENG["holds"][base_call(call)] = time.time() + mins * 60
    return mins

def is_worked(call):
    b = base_call(call)
    return is_me(call) or any(base_call(w) == b for w in list(state["worked"]))

def prune(now):
    """Drop stale entries so long sessions don't grow without bound."""
    for d in (state["cq"], state["answering"]):
        for k in [k for k, e in list(d.items()) if now - e["seen"] > 600]:
            d.pop(k, None)
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
    if state["target"]:
        halt_ws()
    state["target"] = None
    for k in ("cq", "answering", "decparams"):
        state[k].clear()
    state["msgs"].clear()
    for k in ("callers", "occ", "noise_hist"):
        ENG[k].clear()
    for k in ("spec", "active", "calls_to"):
        ENG[k].clear()
    ENG["cq_until"] = 0
    log(f"mode {old} -> {new}: {period()} s periods")

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

def handle_spectrum(data, off):
    """ws-hunter SpectrumSummary: period QTime ms, T/R ms, start Hz, bin Hz,
    floor dB, step dB, levels. Marks hot bins, and flags wide undecoded
    energy (SSB, CW/RTTY clusters...) as someone else using the frequency."""
    t_ms, tr_ms, start, bin_hz = struct.unpack(">IIII", data[off:off+16])
    floor, step = struct.unpack(">dd", data[off+16:off+32])
    n = struct.unpack(">I", data[off+32:off+36])[0]
    levels = data[off+36:off+36+n]
    db = [floor + v * step for v in levels]
    freqs = [start + i * bin_hz for i in range(len(db))]
    band = sorted(d for f, d in zip(freqs, db) if 200 <= f < 2800)
    if not band:
        return
    # Noise floor from history, not this period alone: a voice signal can
    # fill the whole passband, so this period's own floor would be the signal.
    ENG["noise_hist"].append(band[len(band) // 10])
    hist = sorted(ENG["noise_hist"])
    noise = hist[len(hist) // 4]
    hot = [f for f, d in zip(freqs, db) if d > noise + 8]
    now, par = time.time(), int(t_ms // max(tr_ms, 1)) % 2
    ENG["spec"][par] = {"t": now, "bin": bin_hz, "hot": hot, "noise": noise}
    if not state["config"].get("qrm_detect", True):
        return
    # undecoded wide energy: strong bins no FT8 decode explains, in one run
    dec_iv = [(o["df"] - GUARD, o["df"] + bw() + GUARD) for o in list(ENG["occ"])
              if o["par"] == par and now - o["t"] < cyc(2)]
    run = best = 0
    for f, d in zip(freqs, db):
        strong = d > noise + 12 and not any(a <= f < b for a, b in dec_iv)
        run = run + bin_hz if strong else 0
        best = max(best, run)
    if best >= 300:
        ENG["qrm_n"] += 1
        if ENG["qrm_n"] >= 2 and now >= ENG["qrm_until"]:
            mins = state["config"].get("qrm_backoff_min", 5)
            ENG["qrm_until"] = now + mins * 60
            ENG["qrm_why"] = f"{best} Hz of undecoded signal"
            log(f"non-FT8 activity on frequency ({best} Hz wide) — no new calls for {mins}m")
            if ENG["cq_until"]:
                ENG["cq_until"] = 0; halt_ws()
    else:
        ENG["qrm_n"] = 0

def engine_tick():
    """Called once per 15s FT8 cycle. Priority: callers-to-me > CQ > traffic."""
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
        return
    t = state["target"]
    cfg = state["config"]
    if t and is_worked(t["call"]):  # logged (seen in ADIF) without a QSO Logged packet
        log(f"{t['call']} is in the log, releasing target")
        state["target"] = t = None
    if t:
        t["left"] -= 1
        if t["left"] <= 0:
            halt_ws()
            if t.get("done"):
                state["worked"].add(t["call"])
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
            return
    if now < ENG["qrm_until"]:
        return  # someone else is using the frequency: start nothing new
    # drop stale/held callers, first-come-first-serve
    while ENG["callers"]:
        c, m = ENG["callers"][0]
        if on_hold(c) or is_worked(c):
            ENG["callers"].popleft(); continue
        break
    if ENG["callers"]:
        c, m = ENG["callers"].popleft()
        if acquire(c, "", m, tries=cfg.get("cq_tries", 3), why="called us"):
            ENG["busy"] += 1
            return
    if now < ENG["idle_until"]:
        return  # listen-only rest (stations calling us are still answered above)
    # CQ callers: activations first (if enabled), then strongest; fall through
    # to the next if one can't be answered. Region-directed CQs are skipped.
    # A CQ older than ~2 cycles, or followed by them sending to someone else,
    # means they are busy now; calling anyway just adds QRM.
    cands = [e for e in list(state["cq"].values())
             if e.get("tag", "") in ANSWERABLE and dx_ok(e)
             and not is_worked(e["call"]) and not on_hold(e["call"])
             and now - e["seen"] < cyc(2) + 5 and not in_qso(e)
             and e["snr"] >= cfg.get("min_snr", -20)]
    cands.sort(key=cq_rank)
    for e in cands:
        if e.get("msg") and acquire(e["call"], e["grid"], e["msg"],
                                    tries=cfg.get("cq_tries", 3),
                                    why=e["tag"] if e.get("special") else "CQ"):
            ENG["busy"] += 1
            return
    if now < ENG["cq_until"]:
        return  # our own CQ keeps running; WS repeats it each period
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
        return
    ENG["busy"] = 0
    if ENG["cq_until"]:  # our CQ run just ended: stop and listen before the next
        ENG["cq_until"] = 0
        halt_ws()
        ENG["idle_until"] = now + cfg.get("idle_secs", 60)
        log(f"CQ run over, listening {cfg.get('idle_secs', 60)}s")
        return
    if cfg.get("call_cq", True):  # last resort: call CQ ourselves
        start_cq(cfg.get("cq_secs", 120))
        return
    # spare cycle: every idle_every idle cycles, rest to catch the off sequence
    ENG["idle_n"] += 1
    if ENG["idle_n"] % max(1, cfg.get("idle_every", 4)) == 0:
        ENG["idle_until"] = now + cfg.get("idle_secs", 60)
        log(f"listening {cfg.get('idle_secs', 60)}s (off-sequence watch)")

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
        CTX["tx"].sendto(pack_configure(state["ws_id"]), ws_dest())
        if has_ws_hunter():
            CTX["tx"].sendto(pack_set_bool(SET_TX_ENABLED, state["ws_id"], True), ws_dest())
            how = "Enable Tx via UDP"
        else:
            time.sleep(1.0)  # let WS act on Halt + Configure before clicking
            how = arm_tx()
    except Exception as e:
        log(f"CQ start failed: {e}"); return
    ENG["cq_until"] = time.time() + secs
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
    if p:
        tune_tx(1 - parity(p["t_ms"]))  # we answer in the other period
    if not send_exact_reply(CTX["tx"], msg):
        return False
    # `left` counts T/R periods; we transmit in every other one
    state["target"] = {"call": call, "grid": grid, "left": tries * 2, "why": why,
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
            elif state["running"] and not signoff and not is_worked(frm) and not on_hold(frm) \
                    and not any(same_call(c, frm) for c, _ in list(ENG["callers"])):
                # someone calling us: queue first-come-first-serve
                ENG["callers"].append((frm, msg))
                log(f"caller queued: {msg}")
                if t and not t.get("resp"):  # never abandon a QSO in progress
                    halt_ws()
                    log(f"dropping {tgt} for caller {frm}")
                    state["target"] = None
        state["answering"][msg] = {"msg": msg, "snr": snr, "tome": to_me,
            "from": frm or msg, "seen": now, "t": time.strftime("%H:%M:%S")}

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
        if self.path == "/api/state":
            # snapshot shared structures first: the UDP/engine threads mutate them
            cq = list(state["cq"].values())
            for e in cq:
                e["worked"] = is_worked(e["call"])
            cq.sort(key=cq_rank)
            ans = sorted(list(state["answering"].values()), key=lambda e: (not e["tome"], -e["seen"]))
            all_msgs = list(state["msgs"])
            msgs = all_msgs[:30]
            dxf = [m for m in all_msgs if m.get("dx")][:15]
            u = state["udp"]
            u["holder"] = udp_holder(udp_port)
            holds = {c: max(0, round(until - time.time())) for c, until in list(ENG["holds"].items())
                     if until > time.time()}
            par = ENG["parity"]
            return self._json({"running": state["running"], "mycall": state["mycall"],
                "mygrid": state["mygrid"], "target": state["target"], "cq": cq[:40],
                "answering": ans[:30], "worked": sorted(list(state["worked"])),
                "msgs": msgs, "mytx": list(state["mytx"])[:15], "dxfocus": dxf,
                "holds": holds, "callers": [c for c, _ in list(ENG["callers"])],
                "parity": {"even": par[0], "odd": par[1],
                           "hot": ("even" if par[0] >= par[1] else "odd")},
                "idle": max(0, round(ENG["idle_until"] - time.time())),
                "cq_calling": max(0, round(ENG["cq_until"] - time.time())),
                "mode": mode(), "period": period(),
                "qrm": {"secs": max(0, round(ENG["qrm_until"] - time.time())), "why": ENG["qrm_why"]},
                "txdf": {"current": u.get("tx_df"), **ENG.get("suggest", {})},
                "log": list(state["log"]), "config": state["config"], "stats": state["stats"],
                "udp": {"holder": u["holder"], "ws_id": state["ws_id"],
                        "revision": u.get("revision", ""), "ws_hunter": has_ws_hunter(),
                        "dxcall": u["dxcall"], "rx_count": u["rx_count"],
                        "tx_enabled": u.get("tx_enabled", False),
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
        if p == "/api/start": state["running"] = True; log("hunter started")
        elif p == "/api/stop":
            state["running"] = False; state["target"] = None; ENG["cq_until"] = 0
            ENG["callers"].clear(); halt_ws(); log("stopped, TX halted")
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
                CTX["tx"].sendto(pack_configure(state["ws_id"]), ws_dest())
                time.sleep(2)  # let Status report back before toggle check
                log(f"CQ mode: stack reset + {arm_tx(only_if_off=True)}")
            except Exception as e:
                log(f"CQ reset failed: {e}")
        elif p == "/api/arm":
            log(f"arm TX: {arm_tx()}")
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
        elif p == "/api/config":
            state["config"].update({k: v for k, v in body.items() if k in state["config"]})
        else: return self._json({"error": "unknown"}, 404)
        self._json({"ok": True})
    def log_message(self, *a): pass

ap = argparse.ArgumentParser()
ap.add_argument("--mycall", default="KR4MZM"); ap.add_argument("--mygrid", default="EM84")
ap.add_argument("--port", type=int, default=2237); ap.add_argument("--http", type=int, default=8080)
a = ap.parse_args()
state["mycall"] = a.mycall.upper(); state["mygrid"] = a.mygrid.upper(); udp_port = a.port
threading.Thread(target=watcher, daemon=True).start()
log(f"hunter ready as {state['mycall']} {state['mygrid']}")
print(f"open http://0.0.0.0:{a.http}/")
ThreadingHTTPServer(("0.0.0.0", a.http), H).serve_forever()
