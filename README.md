# FT8 Hunter

Automated FT8 operating for WSJT-X / WS Digital Suite: **make as many contacts as
possible without being rude on the band.**

Each cycle it does the first of these that applies:

1. Answer a station calling you.
2. Pounce on an activator (CQ POTA / SOTA / WWFF / IOTA / BOTA / LOTA), strongest first.
3. Answer any other CQ, strongest first (CQs directed at a region are skipped).
4. Tail-end a station signing off.
5. Call CQ for a while, then stop and listen.

To stay polite it limits retries, holds off stations that don't answer, never
abandons a QSO in progress, transmits on a clear offset instead of on top of the
station it calls, and backs off when someone else (SSB, CW…) is using the frequency.

## Run

```bash
python3 ft8_server.py --mycall YOURCALL --mygrid YOURGRID --port 2237 --http 8080   # stdlib only
# open http://localhost:8080/ (mobile-friendly UI)
```

In WS / WSJT-X, open Settings → Reporting, set the UDP server to `127.0.0.1:2237`
and tick **Accept UDP requests**. You can change retries, hold time, CQ and listen
times live from the Settings panel in the UI.

Offline tests (no radio needed): `python3 test_hunter.py`

### WS build with hunter controls

Stock WS has no UDP command to set the Tx offset or Enable Tx, so the hunter can
only *suggest* a clear offset and falls back to an xdotool click to arm TX. The
[ws-hunter](https://github.com/TheBengineer/ws-hunter) fork of WS adds those
controls plus a per-cycle spectrum feed; the hunter detects it automatically.

```bash
git clone https://github.com/TheBengineer/ws-hunter ~/ws-hunter
~/ws-hunter/build-hunter.sh        # installs to ~/ws-hunter-app; run ~/ws-hunter-app/bin/ws
```

## How it works

- **Decode feed**: passive `SO_REUSEPORT` listener on 2237. WSJT-X never binds 2237
  itself — it sends *from* an ephemeral port, so Replies must go back to that port
  (learned automatically from WS's own packets).
- **Reply = click**: a Type 4 Reply is only honored if every field (time, SNR, DT,
  DF, `~` mode marker, message) exactly matches a prior decode — same as clicking a
  line in the decode window. Params come from live Type 2 packets, never guessed.
- **TX control**: WS auto-sequences once armed. The hunter sends Halt Tx (type 8)
  when a target times out or is skipped.
- **Clear offset** (ws-hunter build): before answering or calling CQ it ticks Hold
  Tx Freq and moves to the widest gap on the period it will transmit in, judged
  from recent decodes and the spectrum; it stays put while that spot is clear.
- **Someone else on frequency** (ws-hunter build): wide undecoded energy in the
  spectrum for two cycles running pauses new calls for 5 minutes.
- **WRL passthrough**: every WS packet is rebroadcast to `127.0.0.1:2238` — point
  WRL Desktop there. (Two apps can't share 2237: Linux REUSEPORT *load-balances*,
  so they'd each miss half the traffic.)
