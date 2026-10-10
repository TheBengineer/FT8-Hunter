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
               "idle_every": 4, "idle_secs": 60},
    "stats": {"cq_heard": 0, "calls_made": 0},
}
CQ_RE = re.compile(r"^CQ\s+(?:DX\s+|TEST\s+)?(?:POTA\s+)?([A-Z0-9/]*\d[A-Z0-9/]*)(?:\s+([A-Z]{2}\d{2}(?:\d{2})?))?", re.I)
udp_port = 2237

def log(msg):
    state["log"].appendleft({"t": time.strftime("%H:%M:%S"), "msg": msg})

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

def parse_decode(data):
    """Return (snr:int, message:str) for type-2 Decode, else None."""
    try:
        if struct.unpack(">II", data[:8]) != (0xADBCCBDA, 2): return None
        off = 8
        idlen = struct.unpack(">i", data[off:off+4])[0]; off += 4 + max(idlen, 0)
        if struct.unpack(">I", data[off:off+4])[0] != 2: return None
        off += 4 + 1  # type + New flag
        off += 4      # Time ms
        snr = struct.unpack(">i", data[off:off+4])[0]; off += 4
        off += 8 + 4  # DeltaTime + DeltaFreq
        strs, p = [], off
        for _ in range(3):
            ln = struct.unpack(">i", data[p:p+4])[0]; p += 4
            if ln < 0 or ln > 100: break
            strs.append(data[p:p+ln].decode("utf-8", "errors")); p += ln
        return (snr, (strs[1] if len(strs) > 1 else strs[0]).strip())
    except Exception:
        return None

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

def load_worked():
    """Only TODAY's contacts count (local date). Returns (set, {path: mtime})."""
    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
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
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    CTX.update(tx=tx, myll=grid_to_latlon(state["mygrid"]) if state["mygrid"] else None)
    threading.Thread(target=udp_listener, daemon=True).start()
    # Reply TX still goes via UDP to WS
    threading.Thread(target=tail_alltxt, daemon=True).start()
    threading.Thread(target=cycle_ticker, daemon=True).start()

def cycle_ticker():
    """Independent 15s engine tick (drives targeting even if ALL.TXT stalls)."""
    FT = 15
    nxt = (int(time.time()) // FT + 1) * FT
    while True:
        time.sleep(max(0.5, nxt - time.time()))
        nxt += FT
        CTX["cycle"][0] += 1
        try:
            engine_tick()
        except Exception as e:
            log(f"tick err: {e}")

CTX = {"tx": None, "myll": None, "seen": {}, "cycle": [0]}
# Targeting engine: holds {call: until_ts}, callers queue [(call,msg)], parity {0:n,1:n},
# idle_until ts (listen-only rest), busy_cycles (consecutive cycles with a target)
ENG = {"holds": {}, "callers": deque(maxlen=20), "parity": {0: 0, 1: 0},
       "idle_until": 0, "busy": 0, "idle_n": 0}

state["ws_id"] = "WSJT-X"   # learned from WS broadcasts when heard
state["ws_addr"] = None    # (ip, ephemeral port) WSJT-X listens on — learned from its packets
state["udp"] = {"holder": "?", "last_rx": 0, "dxcall": "", "tx_enabled": False,
                 "transmitting": False, "rx_count": 0}
state["decparams"] = {}  # msg -> {t_ms, snr, dt, df}

state["fwd_port"] = 2238  # passthrough port: hunter rebroadcasts WS packets here for WRL Desktop
def udp_listener():
    """Passive REUSEPORT listener: learns WS id, tracks Status/TX state."""
    fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
            if "WS" in wid.upper() or "JT" in wid.upper():
                state["ws_id"] = wid
            state["udp"]["last_rx"] = time.time()
            state["udp"]["rx_count"] += 1
            if mtype in (1, 2):  # Status / Decode = genuine WS traffic
                state["udp"]["ws_rx"] = state["udp"].get("ws_rx", 0) + 1
                state["udp"]["last_rx"] = time.time()
            if mtype == 5:  # QSO Logged: contact done -> worked + release target
                try:
                    strs5, q5 = [], off
                    while q5 + 4 <= len(data):
                        ln = struct.unpack(">i", data[q5:q5+4])[0]; q5 += 4
                        if ln < 0 or ln > 200 or q5 + ln > len(data):
                            break
                        strs5.append(data[q5:q5+ln].decode("utf-8", "errors")); q5 += ln
                    blob = " ".join(strs5).upper()
                    for cand in re.findall(r"[A-Z0-9/]*\d[A-Z0-9/]*", blob):
                        if len(cand) >= 3 and cand != state["mycall"]:
                            state["worked"].add(cand)
                            if (state["target"] or {}).get("call") == cand:
                                log(f"logged QSO with {cand} — nice!")
                                state["target"] = None
                            break
                except Exception:
                    pass
            if mtype == 1:  # Status body: DialFreq u64, Mode, DXcall, Report,
                try:           # TxMode, TxEnabled?, Transmitting?, ...
                    p = off + 8  # dialfreq u64
                    strs = []
                    for _ in range(5):  # Mode, DXcall, Report, TxMode
                        ln = struct.unpack(">i", data[p:p+4])[0]; p += 4
                        if ln < 0 or ln > 64:
                            break
                        strs.append(data[p:p+ln].decode("utf-8", "errors")); p += ln
                    if len(strs) >= 2:
                        state["udp"]["dxcall"] = strs[1]
                    if len(strs) == 5 and p + 2 <= len(data):
                        state["udp"]["tx_enabled"] = bool(data[p]); p += 1
                        state["udp"]["transmitting"] = bool(data[p]); p += 1
                        state["udp"]["tx_t"] = time.time()
                except Exception:
                    pass
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
                        state["decparams"][dmsg] = {"t_ms": t_ms, "snr": dsnr,
                            "dt": ddt, "df": ddf, "mode": dmode}
                        if len(state["decparams"]) > 500:
                            state["decparams"] = dict(list(state["decparams"].items())[-300:])
                        handle_decode(dmsg, dsnr, CTX["tx"], CTX["myll"],
                                      CTX["seen"], CTX["cycle"], t_ms)
                except Exception:
                    pass
        except Exception:
            pass

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
    FT = 15  # one FT8 cycle
    next_tick = (int(time.time()) // FT + 1) * FT
    while True:
        line = fh.readline()
        if not line:
            time.sleep(1)
            now = time.time()
            if now >= next_tick:
                next_tick += FT
                continue
        m = ALL_RE.match(line.strip())
        if not m:
            tm = TX_RE.match(line.strip())
            if tm:
                state["mytx"].appendleft({"msg": tm.group(5).strip(),
                    "t": time.strftime("%H:%M:%S")})
            continue
        tstr, snr, dt, audio, msg = m.group(1), int(m.group(2)), float(m.group(3)), int(m.group(4)), m.group(5).strip()
        if msg not in state["decparams"]:  # UDP feed has fuller params incl. mode
            state["decparams"][msg] = {"t_ms": alltxt_time_ms(tstr), "snr": snr, "dt": dt, "df": audio}
        if len(state["decparams"]) > 500:
            state["decparams"] = dict(list(state["decparams"].items())[-300:])
        handle_decode(msg, snr, tx, myll, seen, cycle)

def ws_dest():
    return state["ws_addr"] or ("127.0.0.1", udp_port)

def halt_ws():
    try:
        CTX["tx"].sendto(pack_halt(state["ws_id"]), ws_dest())
    except Exception:
        pass

def on_hold(call):
    return ENG["holds"].get(call.upper(), 0) > time.time()

def is_worked(call):
    w = state["worked"]
    return call in w or call.split("/")[0] in w or call == state["mycall"]

def engine_tick():
    """Called once per 15s FT8 cycle. Priority: callers-to-me > CQ > traffic."""
    now = time.time()
    # refresh today's worked if a log file changed
    try:
        for p, mt in list(state.get("adi_mtime", {}).items()):
            if os.path.exists(p) and os.path.getmtime(p) != mt:
                state["worked"], state["adi_mtime"] = load_worked()
                break
    except Exception:
        pass
    if not state["running"]:
        return
    if now < ENG["idle_until"]:
        return  # listen-only rest
    t = state["target"]
    cfg = state["config"]
    if t:
        t["left"] -= 1
        if t["left"] <= 0:
            halt_ws()
            ENG["holds"][t["call"]] = now + cfg.get("hold_min", 10) * 60
            log(f"timeout, holding {t['call']} {cfg.get('hold_min', 10)}m")
            state["target"] = None
        else:
            ENG["busy"] += 1
            # re-arm every cycle until they reply (suite treats Reply as one-shot);
            # once QSO progresses (resp), WS auto-sequences — don't touch it
            if not t.get("resp") and t.get("msg"):
                send_exact_reply(CTX["tx"], t["msg"])
            return
    # drop stale/held callers, first-come-first-serve
    while ENG["callers"]:
        c, m = ENG["callers"][0]
        if on_hold(c) or is_worked(c):
            ENG["callers"].popleft(); continue
        break
    if ENG["callers"]:
        c, m = ENG["callers"].popleft()
        acquire(c, "", m, tries=99, why="called us")
        ENG["busy"] += 1
        return
    # CQ callers, strongest first
    cands = [e for e in state["cq"].values()
             if not is_worked(e["call"])
             and not on_hold(e["call"]) and now - e["seen"] < 60]
    cands.sort(key=lambda e: (-e["snr"], e["km"] is None, e["km"] or 0))
    if cands:
        e = cands[0]
        msg = next((m for m in state["decparams"]
                    if re.match(rf"^CQ\s+(?:DX\s+|TEST\s+)?{re.escape(e['call'])}\b", m, re.I)), None)
        if msg and acquire(e["call"], e["grid"], msg, tries=cfg.get("cq_tries", 3), why="CQ"):
            ENG["busy"] += 1
            return
    # no CQ: tail-end strongest traffic (overheard non-CQ), try twice
    heard = [m for m in state["msgs"]
             if not m["msg"].upper().startswith("CQ") and now - m.get("seen", 0) < 90]
    best, bestcall = None, None
    for m in heard:
        parts = m["msg"].upper().split()
        if len(parts) < 2 or state["mycall"] in parts or "/" in parts[0]:
            continue
        if is_worked(parts[0]) or on_hold(parts[0]):
            continue
        if best is None or m["snr"] > best["snr"]:
            best, bestcall = m, parts[0]
    if best and acquire(bestcall, "", best["msg"], tries=cfg.get("tail_tries", 2), why="tail-end"):
        ENG["busy"] += 1
        return
    # spare cycle: every idle_every idle cycles, rest to catch the off sequence
    ENG["idle_n"] += 1
    ENG["busy"] = 0
    if ENG["idle_n"] % max(1, cfg.get("idle_every", 4)) == 0:
        ENG["idle_until"] = now + cfg.get("idle_secs", 60)
        log(f"listening {cfg.get('idle_secs', 60)}s (off-sequence watch)")

def acquire(call, grid, msg, tries, why):
    if not send_exact_reply(CTX["tx"], msg):
        return False
    state["target"] = {"call": call, "grid": grid, "left": tries, "why": why,
                       "msg": msg, "resp": False}
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
        tgt = (state["target"] or {}).get("call", "")
        parts = msg.upper().split()
        to_me = state["mycall"] in parts
        # even/odd sequence: decode time falls just after the 15s TX slot
        if t_ms is not None:
            ENG["parity"][int((t_ms // 1000 // 15) % 2)] += 1
        state["msgs"].appendleft({"msg": msg, "snr": snr, "t": time.strftime("%H:%M:%S"),
            "seen": now, "tome": to_me, "dx": bool(tgt) and tgt in parts})
        m = CQ_RE.match(msg)
        if m:
            call, grid = m.group(1).upper(), (m.group(2) or "").upper()
            dist = km(myll, grid_to_latlon(grid)) if grid else None
            state["cq"][call] = {"call": call, "grid": grid, "snr": snr,
                "km": dist, "worked": call in state["worked"] or call == state["mycall"],
                "seen": now, "t": time.strftime("%H:%M:%S"),
                "hold": on_hold(call),
                "mode": state["decparams"].get(msg, {}).get("mode", "?")}
            state["stats"]["cq_heard"] += 1
            return  # selection happens in engine_tick (priority order)
        # someone calling us? queue first-come-first-serve (preempts everything)
        if to_me and state["running"] and parts and parts[0] != state["mycall"]:
            if not is_worked(parts[0]) and not on_hold(parts[0]) \
               and not any(c == parts[0] for c, _ in ENG["callers"]):
                if tgt == parts[0]:
                    state["target"]["left"] = 99  # QSO progressing; WS auto-sequences
                    state["target"]["resp"] = True
                    log(f"reply: {msg}")
                else:
                    ENG["callers"].append((parts[0], msg))
                    log(f"caller queued: {msg}")
                    if tgt:  # drop current target, answer the caller now
                        halt_ws()
                        log(f"dropping {tgt} for caller {parts[0]}")
                        state["target"] = None
            elif tgt == parts[0]:
                state["target"]["left"] = 99
                state["target"]["resp"] = True
                log(f"reply: {msg}")
        state["answering"][msg] = {"msg": msg, "snr": snr, "tome": to_me,
            "from": parts[0] if parts else msg, "seen": now, "t": time.strftime("%H:%M:%S")}

def arm_tx(only_if_off=False):
    """Click WS 'Enable Tx' via xdotool (no UDP message exists for it).
    Position computed from live window geometry (calibrated 2026-10-10;
    re-calibrate if the WS layout/splitter moves: button frac ≈ (0.742, 0.428))."""
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
        bx, by = x + int(w * 0.742), y + int(h * 0.428)
        if only_if_off and state["udp"].get("tx_enabled"):
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
            for e in state["cq"].values():
                e["worked"] = is_worked(e["call"])
            cq = sorted(state["cq"].values(), key=lambda e: (-e["snr"], e["km"] is None, e["km"] or 0))
            ans = sorted(state["answering"].values(), key=lambda e: (not e["tome"], -e["seen"]))
            tgt = (state["target"] or {}).get("call", "")
            msgs = list(state["msgs"])[:30]
            dxf = [m for m in state["msgs"] if tgt and tgt in m["msg"].upper().split()][:15]
            u = state["udp"]
            u["holder"] = udp_holder(udp_port)
            holds = {c: max(0, round(until - time.time())) for c, until in ENG["holds"].items()
                     if until > time.time()}
            par = ENG["parity"]
            tot = (par[0] + par[1]) or 1
            return self._json({"running": state["running"], "mycall": state["mycall"],
                "mygrid": state["mygrid"], "target": state["target"], "cq": cq[:40],
                "answering": ans[:30], "worked": sorted(state["worked"]),
                "msgs": msgs, "mytx": list(state["mytx"])[:15], "dxfocus": dxf,
                "holds": holds, "callers": [c for c, _ in ENG["callers"]],
                "parity": {"even": par[0], "odd": par[1],
                           "hot": ("even" if par[0] >= par[1] else "odd")},
                "idle": max(0, round(ENG["idle_until"] - time.time())),
                "log": list(state["log"]), "config": state["config"], "stats": state["stats"],
                "udp": {"holder": u["holder"], "ws_id": state["ws_id"],
                        "dxcall": u["dxcall"], "rx_count": u["rx_count"],
                        "tx_enabled": u.get("tx_enabled", False),
                        "transmitting": u.get("transmitting", False) if u.get("tx_t", 0) and time.time() - u.get("tx_t", 0) < 20 else False,
                        "by_type": u.get("by_type", {}),
                        "fwd_port": state.get("fwd_port", 2238),
                        "heard_ago": round(time.time() - u["last_rx"], 1) if u["last_rx"] else None}})
        self.send_error(404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        p = urlparse(self.path).path
        if p == "/api/start": state["running"] = True; log("hunter started")
        elif p == "/api/stop":
            state["running"] = False; state["target"] = None
            ENG["callers"].clear(); halt_ws(); log("stopped, TX halted")
        elif p == "/api/skip":
            if state["target"]:
                halt_ws()
                ENG["holds"][state["target"]["call"]] = time.time() + state["config"].get("hold_min", 10) * 60
            state["target"] = None; log("target skipped + held")
        elif p == "/api/cq":
            # reset WS stack to default CQ + stop hunting so WS calls CQ on its own
            halt_ws()
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
            # find the exact decode line for this call
            cand = [m for m in state["decparams"] if re.match(
                rf"^CQ\s+(?:DX\s+|TEST\s+)?{re.escape(c)}\b", m, re.I)]
            if not cand:
                self._json({"error": f"no recent CQ decode for {c}"}, 404); return
            msg = sorted(cand, key=lambda m: state["decparams"][m].get("snr", -99))[-1]
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if send_exact_reply(s, msg):
                state["target"] = {"call": c, "grid": g, "left": state["config"]["max_cycles"]}
                log(f"manual call {msg}")
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
