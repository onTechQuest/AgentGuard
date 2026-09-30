# Offline M16 demonstration

These two synthetic completed runs use the existing production artifact schemas
and `execution_mode=offline_fixture`. They contain fingerprints and retained
synthetic scores, not raw dataset content or live provider results.

- `16000000000000000000000000000001`: baseline; one functional failure.
- `16000000000000000000000000000002`: changed model/prompt fingerprints; an
  additional functional failure, lower semantic score, safety failure, lower
  observed latency and tokens.

Both existing gate decisions are FAIL. Neither run is a promoted baseline or a
performance qualification. All model/provider/version names are explicitly
synthetic. Copy these two directories into `reports/evaluations` to run the
documented CLI demo. Copying artifacts performs no evaluations.
