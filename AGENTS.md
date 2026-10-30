# Notes for coding agents

FT8 Hunter is `ft8_server.py`, the daemon and web server, with no dependencies
outside the standard library, plus `ft8_app.html`, its page. The tests live in
`test_hunter.py` (`python test_hunter.py` ends with `ALL PASS`).
`.github/workflows/build.yml` builds the downloads and makes the release.

## Releases come with a changelog

Every release has a changelog entry, and its GitHub release shows that entry.

1. **Write the entry first.** Add it at the top of `CHANGELOG.md`, newest
   first, before tagging:

   ```markdown
   ## 1.1.12 (2026-10-31)

   - What changed, written for the operator: what they'll see on the air or in
     the app, not how the code does it.
   - Fixes say what went wrong before ("the dial stayed 1 kHz up after
     transmitting"), so someone can tell whether it was their problem.
   - A new setting: where it is and what it does by default.
   - A new bundled WS build: its tag and what changed in it.
   ```

   Cover everything since the last release (`git log v<last>..HEAD`), with one
   bullet per change a user would notice. Leave out internal changes such as
   tests, CI or refactors unless they change what the user gets.
2. **Bump `VERSION`** in `ft8_server.py`. The tag must be `v` + `VERSION`, or the
   build fails.
3. **Commit, tag and push.** Commit the changelog and the version bump together,
   then tag `v<VERSION>` and push `main` and the tag.
4. **Put the entry in the GitHub release.** Once CI has made the release, replace
   its generated notes with that version's section of `CHANGELOG.md`:

   ```bash
   awk '/^## /{p = ($2 == "1.1.12")} p' CHANGELOG.md | tail -n +2 > notes.md
   gh release edit v1.1.12 --notes-file notes.md
   ```

   Then check the release page shows it.

A release without a changelog entry isn't finished.

## Standing rules

- Commit, tag or push only when the user asks.
- No attribution lines (Co-Authored-By, "Generated with", and the like) in
  commits or pull requests.
- Never run the installed hunter with command-line flags on the user's machine:
  it saves them to `%APPDATA%\FT8-Hunter\hunter_config.json`.
- The bundled WS is pinned in `build.yml` (`WS_TAG` and one SHA-256 per
  download). A new WS build means a new ws-hunter release, then a new tag and
  new checksums here.
