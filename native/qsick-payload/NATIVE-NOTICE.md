# Native component notice

`qsick_payload.dll` is a **binary component**. Its C++ source, build script and test
host are deliberately not distributed with this repository; only the compiled DLL and
this notice are tracked.

## What you receive

| File                | Purpose                                                    |
|---------------------|------------------------------------------------------------|
| `qsick_payload.dll` | the injected payload (named pipes + hook engine)           |
| `offsets.json`      | per-build RVA table (data, editable)                       |
| `BUILD.txt`         | version, size, SHA-256 and dependencies of the shipped DLL |
| this notice         | terms for the binary                                       |

The DLL is statically linked (`/MT`) and imports only `KERNEL32.dll` and
`ADVAPI32.dll` — no VC++ redistributable, no Rust toolchain, no package manager.

## Licensing

`qsick_payload.dll` is **not** covered by the repository's LICENSE. It is a
proprietary component, distributed only as part of QSICK and only for the
non-commercial use permitted by the project license. No rights are granted to
reverse engineer, decompile, modify, repackage or redistribute it separately.
The AGPL-3.0 terms continue to apply to the Python and WebUI sources.

## Integrity

Every release stamps `BUILD.txt`. Verify before deploying:

```powershell
Get-FileHash native\qsick-payload\qsick_payload.dll -Algorithm SHA256
```

Compare with the `sha256:` line in `BUILD.txt`. A mismatch means the binary was
replaced — do not inject it.

## Offsets

`offsets.json` ships **empty on purpose**: a wrong RVA corrupts the client instead of
failing cleanly. Add an entry only after verifying it against the exact QQ build, set
`"verified": true`, and keep `QSICK_ALLOW_UNVERIFIED` unset in normal use.

## Security model

- Pipes are created with an owner/SYSTEM/Administrators DACL.
- Every session carries a 128-bit token in the pipe name and in the `HELLO` frame
  (`transport.pipe_auth = "random"` by default).
- Without offsets the payload only serves pipes; it never patches the client.

## Reporting

Security issues: open a private report with the maintainers rather than a public
issue, and include the `BUILD.txt` hash of the binary you tested.
