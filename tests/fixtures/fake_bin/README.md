# fake_bin — fake CLI binaries for seat-runner tests

These scripts emit protocol-correct canned output (per `docs/seatspec.md` §3) for the
claude, pi, and codex CLIs, regardless of arguments (the claude fake only reads
`--resume X` to fake a resumed session id). Tests put this directory first on `PATH`
so the seat runners spawn the fakes instead of real binaries:

```python
os.environ["PATH"] = "<repo>/tests/fixtures/fake_bin" + ":" + os.environ["PATH"]
```

Failure modes are controlled by env flags: `FAKE_CLAUDE_FAIL=1` (fake-claude exits 1
after writing "fake auth error" to stderr), `FAKE_PI_FAIL=1` ("pi broken"), and
`FAKE_CODEX_FAIL=1` ("codex auth failed").
