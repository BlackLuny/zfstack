# Independent #68 merge-decision evidence

Collected 2026-10-16 on this Cloud Agent VM (4 vCPU KVM, advertised 2400 MHz, rustc 1.83.0).
Same method as `bench/results/2026-10-16-issue71-decision/`. Not a substitute for a TUN/network qualification package.

| Path | Contents |
|---|---|
| `host.txt` | rustc, cargo, uname, lscpu, memory |
| `trees.txt` | T=`88a2e3f`, W=`bf782a5`, WH=`0a5bd0c`, W2=independent rebuild of W |
| `binary-sha256.txt` | fat LTO `pareto_probe` / `tx_write_probe` hashes |
| `cases.json` | 9 TX cases, 12 pairs, CPU3, `portable_full_scan_v1` |
| `combined-summary.json` | all window medians + Callgrind Ir |
| `windows/*/summary.json` | per-window bootstrap (seed 65, 10000 draws) |
| `callgrind/*.ir.txt` | process instruction counts, 25000 iterations |
| `asm/` | LTO `put` / `try_allocate_kind` dumps |
| `tests/` | `cargo test --lib` on W and PR HEAD (WH) |

Method: isolated worktrees, `members=[]`, lockfile from T, `CARGO_PROFILE_RELEASE_LTO=fat`, `codegen-units=1`, `panic=abort`. Same frozen probes from `88a2e3f` on every arm. Write sidecar only replaces `TxBuf::push` with `write_with`.
