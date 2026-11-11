"""Post-hoc context of registered misses, from retained raw event excerpts only.

The registered analyzer charges IRQ time only while the generator is runnable
or running. Here we inspect the CPU on which it *slept*, without changing any
registered label, test, threshold or outcome. Durations are wall-clock spans,
not CPU-cycle attribution; NET_RX can service more than one network device.
"""
import gzip
import json
from pathlib import Path
import analyze_trace as trace
from retained_evidence import evidence_root

HERE = Path(__file__).resolve().parent
E = evidence_root()
out = {}
for name in ('trace-2', 'trace-3'):
    directory = E / name
    analysis = json.loads((directory/'analysis.json').read_text())
    pids = {int(k): v for k, v in analysis['generator_pids'].items()}
    model = trace.parse(directory/'late-windows.txt.gz', set(pids.values()))
    samples = trace.generator_samples(directory)
    rows = []
    for entry in analysis['late_samples']:
        if not entry['miss']:
            continue
        sample = samples[entry['flow']][entry['seq']]
        start = round(sample['scheduled_at'] * 1e9)
        end = round((sample['scheduled_at'] + sample['generator_lag_ms'] / 1000) * 1e9)
        pid = pids[entry['flow']]
        contexts = []
        for low, high, state, cpu in model['timeline'][pid]:
            a, b = max(start, low), min(end, high)
            if a >= b or state != 'sleeping':
                continue
            spans = [{'start_ns': l, 'end_ns': h, 'duration_ms': (h-l)/1e6, 'label': label}
                     for l, h, label in model['irq'].rows[cpu] if l < b and h > a]
            # The event immediately preceding a NET_RX entry is useful context,
            # but does not identify every NAPI callback handled within NET_RX.
            preceding = []
            with gzip.open(directory/'late-windows.txt.gz', 'rt') as stream:
                for line in stream:
                    m = trace.LINE.match(line)
                    if not m or int(m['cpu']) != cpu:
                        continue
                    t = int(m['sec'])*10**9 + int(m['frac'].ljust(9, '0')[:9])
                    if m['event'].startswith('irq_handler') and any(0 <= span['start_ns']-t <= 10_000 for span in spans):
                        preceding.append(line.rstrip())
            contexts.append({'sleep_cpu': cpu, 'sleep_window_ms': (b-a)/1e6,
                             'irq_softirq_overlap_ms': model['irq'].union(cpu, a, b)/1e6,
                             'full_overlapping_intervals': spans, 'preceding_irq_events': preceding})
        timers = []
        for waking, cpu in model['wakings'][pid]:
            if not start <= waking <= end:
                continue
            for expired, pointer, now in model['expiries'].get(cpu, []):
                if not 0 <= waking-expired <= trace.WAKE_TIMER_LOOKBACK_NS:
                    continue
                armed = [(t, due) for t, due in model['hrtimer_starts'].get(pointer, []) if t <= expired]
                if armed:
                    timers.append({'cpu': cpu, 'timer': pointer, 'armed_ns': armed[-1][0],
                                   'expires_ns': armed[-1][1], 'expire_event_ns': expired,
                                   'expire_now_ns': now, 'waking_ns': waking,
                                   'timer_late_ms': (now-armed[-1][1])/1e6})
        others = {str(flow): sum(1 for s in values if s.get('started_at') is not None
                                and start <= round(s['started_at']*1e9) <= end)
                  for flow, values in samples.items() if flow != entry['flow']}
        rows.append({'flow': entry['flow'], 'seq': entry['seq'], 'host_pid': pid,
                     'registered_class': entry['class'], 'registered_cpus_after_wakeup': entry['cpus'],
                     'sleep_context': contexts, 'associated_timer_expiries': timers,
                     'other_flow_starts_in_miss_window': others})
    out[name] = rows
result = {'scope': 'Post-hoc read-only context; registered labels and decisions unchanged',
          'limitation': 'NET_RX elapsed spans do not identify the device/callback or exclusive CPU time; no new measurement',
          'runs': out}
(HERE/'sleep-context.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result, indent=2))
