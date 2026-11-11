# Budget-headroom control, declared before its measurements

The first W1/peer/CUBIC attempt retained 251 unfinished/missed-deadline loaded
requests out of 1,200 scheduled. This is an outcome failure, not a discarded
environment sample. It rejects that W1 profile despite good aggregate CPU and
throughput. Other W1 attempts still run to completion under the frozen order.

W1 made peer and port ceilings equal at 64 MiB; a heavy peer can consume the
port allowance. This is a plausible mechanism, **not yet a proven root cause**
of this particular timeout. Keep the wider prefetch settings identical but
restore the original hierarchy to separate these changes:

W2 (`p3` in arm names): 256 KiB minimum / 20 ms / 2 MiB prefetch maximum, with
the original 512 MiB global / 64 MiB port / 32 MiB peer ceilings. Prefetch is
still bounded by send-space and budget accounting; its nominal minimum is
not permission to exceed those limits. All other settings unchanged, same
frozen binary. Compare W2 to W0 in two balanced pairs per CC/queue cell, 16
runs total. Keep every failure; no W1 samples enter W2 estimates.

This is the final exploratory memory profile before candidate selection.
No parameter is promoted on these two pairs alone: the selected candidate
still needs the independent WAN, peak and confirmation phases. If W2 does
not show clear additional CPU benefit while preserving request outcomes,
prefer the original prefetch and hierarchy with the queue-only intervention.
