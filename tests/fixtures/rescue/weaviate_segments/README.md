# Synthetic Weaviate LSM segment fixtures

Real LSM segment files are large (~4-8 MB each). The unit tests at
`tests/test_weaviate_rescue.py` mock the docker subprocess that
inspects segment files, so no on-disk fixtures are required to
verify quarantine identifies the canonical 5-file group:

- `segment-<id>.db`
- `segment-<id>.bloom`
- `segment-<id>.cna`
- `segment-<id>.secondary.0.bloom`
- `segment-<id>.secondary.1.bloom`

The v0.8.2 `test_rescue_go_binary.py` suite has a real-fixture
segment harness for the Go scan-shard binary; that one runs only
when `dejavu-rescue` is on PATH.

To reproduce the original torn-segment, copy
`segment-1778777077583721728.*` from your Weaviate volume's
`claudedejavuturn/aFEA9t3Ikz8D/lsm/objects/` directory (or use the
quarantine the `quarantine-segment` command preserves at
`$DATA_ROOT/weaviate-quarantine/`).
