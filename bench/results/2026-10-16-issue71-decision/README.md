# Independent #71 merge-decision evidence

Collected 2026-10-16 on this Cloud Agent VM (4 vCPU KVM, advertised 2400 MHz, rustc 1.83.0).
Not a substitute for the isolated second-machine package in `bench/results/2026-10-15-issue65-tx/`.

| Path | Contents |
|---|---|
| `host.txt` | rustc, cargo, uname, lscpu, memory |
| `trees.txt` | T=`88a2e3f`, U=`2694c14`, U2=same U other path, W=`bf782a5` (#68) |
| `binary-sha256.txt` | fat LTO `pareto_probe` / `tx_write_probe` hashes |
| `cases.json` | 9 TX cases, 12 pairs, CPU3, `portable_full_scan_v1` |
| `combined-summary.json` | all window medians + Callgrind Ir |
| `windows/*/summary.json` | per-window bootstrap (seed 65, 10000 draws) |
| `callgrind/*.ir.txt` | process instruction counts, 25000 iterations |
| `asm/` | LTO `BlockPool::put` dumps and symbol summary |

Method: isolated worktrees, `members=[]`, shared lockfile, `CARGO_PROFILE_RELEASE_LTO=fat`, `codegen-units=1`, `panic=abort`. Same frozen `pareto_probe.rs` from `88a2e3f` on every arm. Write sidecar only replaces `TxBuf::push` with `write_with`.
