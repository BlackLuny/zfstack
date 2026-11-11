# Selection after V1 screen, before native-Tokio measurements

The 16 frozen V1 attempts all passed request/payload/configuration checks.
Both directions used the same FIFO/250-KB queue for all profiles. No candidate
changed queue discipline or capacity. Every contended candidate gave every bulk
connection some measured progress; the off controls retained finite-window gaps.

Select **99% of the configured downstream IP capacity** for independent tests.
At CUBIC/20 ms, 100% pacing improves mean user latency only 15.32%, below the
predeclared 20% screen gate. The 99% profile improves mean/P99 by 47.69%/71.96%,
goodput changes -0.73%, stack CPU/GB -15.35%, stack+link CPU/GB -9.64%.
BBR/20 ms: mean/P99 -67.70%/-86.40%, goodput -0.35%, stack CPU/GB -51.66%,
stack+link CPU/GB -29.72%. These are two-pair descriptive estimates, not CIs.
The 97% profile sacrifices more goodput (-2.79% CUBIC, -2.28% BBR) for modest
additional mean-latency benefit, and is not selected.

V2 adds a real production Tokio driver/app-queue fixture to the same link and
request generator. Core pacing behavior is unchanged. Its bridge thread is a
fixture input conversion, separately reported; process CPU includes it, stack
CPU includes the real current-thread Tokio driver and async application tasks.
The old bare-host wakeup/poll counters do not count native Tokio wakeups and
must not be presented as doing so. Shutdown requires zero remaining charged
memory and no unexpected application errors.

Run a native smoke first, then freeze the independent RTT/confirmation/peak
plans on the V2 executable. V1 data do not enter their comparisons or confidence
intervals. Keep their FIFO controls and measure 1–500 ms on the actual Tokio
path. If the native fixture exposes a validity or implementation issue, retain
the attempt and freeze a new version before continuing. Do not build while any
measurement is running. The production change remains unqualified until these
guards and the relevant CI cases pass.
