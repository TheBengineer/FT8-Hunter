# Changelog

What changed in each release of FT8 Hunter, newest first.

## 1.1.15 (2026-11-05)

- With several stations answering your CQ, the hunter now juggles them. If the
  station you're working misses 2 of your transmissions in a row and someone else
  is waiting, it works the next caller and comes back to pick the QSO up where it
  was. A station set aside that sends something new goes to the front of the
  queue. All the visits to one station share the usual `qso_tries`. There's no
  juggling once a QSO is signing off, or in manual (contest) mode.
- Fixed: stations waiting their turn were forgotten when another QSO took repeats.
  Anyone not heard for 90 s on FT8 was dropped, even while the hunter was busy, so
  a caller who called once and waited was never answered. Now that time doesn't
  count against them, up to 40 periods (10 minutes on FT8).
- A waiting caller heard working someone else or calling CQ now stays queued, behind
  everyone else, and isn't called until they've finished.
- Fixed: a new caller could take over from someone who had called you, when they
  hadn't replied yet to your first answer. That QSO was interrupted, and only came
  back if they called again. Now only a CQ you're answering can be replaced this way.
- On a busy band, a waiting caller's decode could be trimmed from memory, and the
  hunter would then skip them. It's now kept while they wait.

## 1.1.14 (2026-11-04)

- When FLRig goes away, the hunter comes back on its own. Once FLRig has been
  silent for 15 s the hunter stops and turns WS's TX off, as before. It then asks
  FLRig again after 30 s, 1, 2 and 4 minutes, and every 5 minutes after that. The
  radio panel shows when the next try is, and **Try now** asks at once.
- A hunt that FLRig's disconnect stopped now starts again by itself, however long
  FLRig was gone, once FLRig answers and WS's dial has been on a band and matched
  the radio for 15 s. **Don't restart** in the warning cancels it. Before, you had
  to press Start. After any other stop (WS losing the radio with FLRig fine, no
  decodes, the radio and WS on different frequencies), you still press Start.
- Each band in the Band scan card has a **▶** button that scans just that band,
  in the modes you've picked, without changing the bands picked for **Scan bands**.

## 1.1.13 (2026-10-31)

- The "Someone else is on frequency — no new calls for 5m" warning has an
  **It's just interference** button. It ends the pause so the hunter calls
  again straight away, and turns the busy-frequency check off for 30 minutes,
  so the same interference doesn't start another pause. After that the check
  is back on its own.
- The hunter's window now looks up the computer's network address every
  minute, as the page does. Before, it kept the address it had at startup, so
  after the network handed out a new one the window's link and QR code pointed
  phones at the old address, and differed from the page's.

## 1.1.12 (2026-10-31)

- The Linux download now updates itself, as the Windows installer already
  does. When a new version is out, **Install and restart** in the page (or
  **⬆ Update to …** in the window) stops hunting, turns WS's TX off, downloads
  the new release and checks it against the release's SHA-256. It then puts the new
  FT8-Hunter in place of the old one, and starts it again with the same
  options. A `ws-hunter.AppImage` kept beside the hunter is replaced too, but
  only when the release carries a different WS build. Before this, Linux only
  offered a link to the release page. The hunter's folder must be one you can
  write to.
- Run as a systemd service, the Linux hunter exits with code 75 after an update
  and lets systemd start the new version, so give the unit `Restart=on-failure`.

## 1.1.11 (2026-10-30)

- The hunter deletes the .wav files WS keeps when **File → Save all** or **Save
  decoded** is on, once they are an hour old. It deletes only WS's own files,
  named by date and time, from WS's save folder, and leaves the samples folder
  alone. It's on by default; turn it off in Settings with **Delete WS's saved
  .wav files after an hour**.

## 1.1.10 (2026-10-30)

- Q65 support. The hunter takes Q65's period (15, 30, 60, 120 or 300 s) and
  sub-mode (A–E) from WS. Its timing and Tx offset picks follow them;
  before this, Q65 was treated as FT8, with 15 s periods. On Q65 it decides what
  to send 1 s after the period starts, since WS decodes Q65 only just before the
  period ends.
- On Q65, a quiet band no longer stops the hunter with "nothing decoded for 5
  min".
- Decodes that end in a marker, such as an AP decode's `a1` or Q65's `q3`, are
  read correctly. Before, a sign-off decoded with AP help, such as `RR73 a2`,
  wasn't recognised as one.
- The band scan still covers FT8, FT4 and FT2 only.

## 1.1.9 (2026-10-30)

- Bundles WS build v3.2.0-hunter.2. A rig control error now makes WS retry
  quietly, up to three times over 7 s. The **Rig Control Error** box only
  appears if the radio doesn't answer at all. Before, every second error asked
  you to click Retry. This helps when RF in the shack garbles the CAT link now
  and then.

## 1.1.8 (2026-10-29)

- With WS's Split Operation set to **Fake It**, the hunter now also puts the
  dial back in two cases. One is when the radio reports its Tx frequency late,
  for example a slow FLRig. The other is when the slip shows up while a new
  message is starting to transmit. In 1.1.7 the dial could still end up 500 Hz
  or 1 kHz high. Setting Split Operation to **Rig** avoids the slip altogether.

## 1.1.7 (2026-10-28)

- With WS's Split Operation set to **Fake It**, WS sometimes took the radio's Tx
  frequency as its dial after transmitting, leaving the dial 500 Hz or 1 kHz
  high. The hunter now puts it back and says so in its log.
- Updating from the hunter on Windows no longer fails to start the new hunter
  with "Failed to load Python DLL".
- An update no longer stops partway when the old hunter is slow to quit: the
  installer waits up to 15 s for it, then closes it.
