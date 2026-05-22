# restic_release fixture

Tests generate fresh fixture artifacts into a tmpdir per-run via a TEST
GPG key created on the fly. We do NOT commit any real restic release
binary here — it would be ~25 MB and uselessly volatile.

The fixture artifacts produced at test time:

- `restic_<ver>_<os>_<arch>.bz2` — a synthetic archive containing a
  fake `restic` text marker (NOT a real binary). For sha256 + gpg
  verify path tests only.
- `SHA256SUMS` — single-line file listing the hash + filename.
- `SHA256SUMS.asc` — armoured detached signature from the TEST key.

Tests embed the TEST key's fingerprint as the pinned fingerprint
constant for the duration of that test, then restore the real
constant when done. See `tests/test_install_restic.py`.
