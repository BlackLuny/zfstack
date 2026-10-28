# Placement preflight correction, before A/A or formal peak measurements

The two original controlled-placement preflight attempts completed useful work,
but both were **invalid**: the observer saw only the Python resource wrapper,
not the benchmark or proxy. This kernel does not expose
`/proc/<pid>/task/<tid>/children`; treating its absence as no children silently
left the subprocess tree unpinned. The required driver/TUN-reader audit caught
the failure and stopped the pipeline before A/A or formal peak measurements.
Do not quote these two throughput values as controlled-placement evidence.

Preserve `fastpath-20261024-preflight`, its frozen observer/source snapshots,
both invalid verdicts and the first pipeline failure. The native smoke was
independent of this observer and passed both attempts.

Correct process discovery to read PPID relationships from `/proc/<pid>/stat`,
then inspect command lines and threads only for descendants of the owned
benchmark wrapper. Retain the same masks, 2-s assignment bound, per-thread
observations, CPU quota and validity conditions. Run a separate **two-attempt
`preflight-v2`** using the same frozen old/V5 executables before the original
unchanged A/A, primary, loaded and conditional phases. No old attempt is
overwritten and no sample enters a confidence interval from either preflight.
