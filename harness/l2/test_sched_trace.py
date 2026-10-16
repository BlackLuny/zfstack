"""The OVH scheduling/IRQ diagnostic attributes late sends correctly and never touches foreign state."""
import gzip
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).parent / "results/2026-10-16-ovh51-sched-trace"
sys.path.insert(0, str(DIRECTORY))


def load(name):
    spec = importlib.util.spec_from_file_location("sched_trace_" + name, DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trace = load("analyze_trace")
isolation = load("isolation")
arm = load("arm")

GEN, OTHER = 101, 202
SCHEDULED = 10.0


def ts(sec):
    return f"{sec:.6f}"


def line(sec, cpu, event, body, task="python3", pid=GEN):
    return f"         {task}-{pid}     [{cpu:03d}] d..2. {ts(sec)}: {event}: {body}"


def switch(sec, cpu, prev, prev_state, nxt, prev_comm="python3", next_comm="python3"):
    return line(sec, cpu, "sched_switch", f"prev_comm={prev_comm} prev_pid={prev} prev_prio=120 prev_state={prev_state} ==> "
                                          f"next_comm={next_comm} next_pid={nxt} next_prio=120", prev_comm, prev)


def wake(sec, cpu, pid=GEN, event="sched_wakeup"):
    return line(sec, cpu, event, f"comm=python3 pid={pid} prio=120 target_cpu={cpu:03d}", "swapper/0", 0)


def irq(sec, cpu, number, name, exit_sec):
    return [line(sec, cpu, "irq_handler_entry", f"irq={number} name={name}", "swapper/0", 0),
            line(exit_sec, cpu, "irq_handler_exit", f"irq={number} ret=handled", "swapper/0", 0)]


class Fixture:
    def __init__(self, lines, lag_ms=23.0, drops=0, flows=(0,), extra_procs=()):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        with gzip.open(root / "trace.txt.gz", "wt") as stream:
            stream.write("# tracer: nop\n" + "\n".join(lines) + "\n")
        stats = f"entries: 10\noverrun: {drops}\ncommit overrun: 0\ndropped events: 0\n"
        (root / "trace-stats.json").write_text(json.dumps({"cpu0": stats, "cpu4": stats}))
        processes = [{"pid": GEN + flow, "cmdline": ["/venv/python", "/x/harness/l2/udp_probe.py", "--flow", str(flow)]}
                     for flow in flows] + list(extra_procs)
        (root / "processes.json").write_text(json.dumps(processes))
        cpus = {f"cpu{n}": {"siblings": "0,4" if n in (0, 4) else "1,5", "core_id": "0", "governor": "performance"}
                for n in (0, 1, 4, 5)}
        (root / "host-before.json").write_text(json.dumps({"cpus": cpus}))
        rig = root / "baseline/rig"
        rig.mkdir(parents=True)
        for flow in (0,):
            samples = [{"seq": 0, "phase": "baseline", "status": "missed", "scheduled_at": SCHEDULED,
                        "started_at": None, "generator_lag_ms": lag_ms},
                       {"seq": 1, "phase": "baseline", "status": "completed", "scheduled_at": SCHEDULED + .03,
                        "started_at": SCHEDULED + .0301, "generator_lag_ms": .1}]
            (rig / f"udp-{flow}.json").write_text(json.dumps({"samples": samples}))
        self.root = root

    def __enter__(self):
        return trace.analyze(self.root)

    def __exit__(self, *_):
        self.tmp.cleanup()


START = [switch(9.990, 0, OTHER, "S", GEN, "kworker", "python3")]
END = [switch(10.0300, 0, GEN, "S", 0, "python3", "swapper/0")]


class AttributionTests(unittest.TestCase):
    def test_late_select_timer_is_a_late_wakeup_with_timer_delay(self):
        lines = START + [
            line(9.999, 0, "hrtimer_start", "hrtimer=00000000abcd function=hrtimer_wakeup expires=10001000000 softexpires=10001000000 mode=ABS"),
            switch(9.999, 0, GEN, "S", 0, "python3", "swapper/0"),
            line(10.0199, 0, "hrtimer_expire_entry", "hrtimer=00000000abcd function=hrtimer_wakeup now=10019900000", "swapper/0", 0),
            wake(10.0199, 0, event="sched_waking"), wake(10.0199, 0),
            switch(10.0200, 0, 0, "R", GEN, "swapper/0", "python3")] + END
        with Fixture(lines) as result:
            self.assertTrue(result["valid"], result["invalid_reasons"])
            miss = result["late_samples"][0]
            self.assertTrue(miss["miss"])
            self.assertEqual(miss["class"], "sleeping (late wakeup)")
            self.assertAlmostEqual(miss["timer_late_ms"], 18.9, places=3)
            self.assertEqual(result["miss_class_counts"], {"sleeping (late wakeup)": 1})

    def test_runnable_behind_irq130_on_core0(self):
        lines = START + [switch(9.999, 0, GEN, "S", 0, "python3", "swapper/0"), wake(10.0005, 0),
                         *irq(10.001, 0, 130, "eno1-TxRx-1", 10.0195),
                         switch(10.0200, 0, 0, "R", GEN, "swapper/0", "python3")] + END
        with Fixture(lines) as result:
            miss = result["late_samples"][0]
            self.assertEqual(miss["class"], "IRQ/softirq on generator CPU")
            self.assertAlmostEqual(miss["core0_irq130_ms"], 18.5, places=3)
            self.assertTrue(miss["generator_on_core0"])
            self.assertIn("irq 130 eno1-TxRx-1", miss["top_irq_softirq_ms"])

    def test_runnable_behind_another_task_and_idle_cpu(self):
        behind = START + [switch(9.999, 0, GEN, "R+", OTHER, "python3", "worker"),
                          switch(10.0200, 0, OTHER, "S", GEN, "worker", "python3")] + END
        with Fixture(behind) as result:
            miss = result["late_samples"][0]
            self.assertEqual(miss["class"], "runnable behind other tasks")
            self.assertIn("worker", miss["top_other_tasks_ms"])
        idle = START + [switch(9.999, 0, GEN, "S", 0, "python3", "swapper/0"), wake(10.0001, 0),
                        switch(10.0200, 0, 0, "R", GEN, "swapper/0", "python3")] + END
        with Fixture(idle) as result:
            self.assertEqual(result["late_samples"][0]["class"], "runnable, CPU idle")

    def test_generator_busy_on_cpu(self):
        with Fixture(START + END) as result:
            self.assertEqual(result["late_samples"][0]["class"], "generator on CPU")

    def test_fails_closed_on_drops_mapping_and_coverage(self):
        with Fixture(START + END, drops=3) as result:
            self.assertFalse(result["valid"])
            self.assertIn("trace buffer overran or dropped events", result["invalid_reasons"])
        duplicate = [{"pid": 999, "cmdline": ["python3", "udp_probe.py", "--flow", "0"]}]
        with Fixture(START + END, extra_procs=duplicate) as result:
            self.assertFalse(result["valid"])
        with Fixture([switch(10.010, 0, OTHER, "S", GEN, "kworker", "python3")] + END) as result:
            self.assertFalse(result["valid"])
            self.assertIsNone(result["late_samples"][0]["class"])


class IsolationTests(unittest.TestCase):
    def test_rotated_schedule_runs_every_arm_once_per_round(self):
        order = isolation.schedule(("A", "B", "C"))
        self.assertEqual(len(order), 15)
        self.assertEqual([arm for r, arm in order if r == 1], ["A", "B", "C"])
        self.assertEqual([arm for r, arm in order if r == 2], ["B", "C", "A"])
        self.assertEqual(sorted(arm for _, arm in order), sorted("ABC" * 5))

    def test_nic_irqs_and_exact_test(self):
        text = ("           CPU0       CPU1\n 128:  0  0  IR-PCI-MSIX-0000:04:00.0    0-edge      eno1\n"
                " 130:  9  0  IR-PCI-MSIX-0000:04:00.0    2-edge      eno1-TxRx-1\n"
                " 140:  1  0  IR-PCI-MSIX-0000:05:00.0    0-edge      eno2-TxRx-0\n LOC:  5  5  Local timer interrupts\n")
        self.assertEqual(isolation.nic_irqs(text), ["128", "130"])
        self.assertAlmostEqual(isolation.exact_greater([9, 8, 7, 6, 5], [4, 3, 2, 1, 0]), 1 / 252)
        self.assertEqual(isolation.exact_greater([1, 1, 1, 1, 1], [1, 1, 1, 1, 1]), 1.0)

    def test_decision_rules(self):
        high = [{"max_ms": v, "misses": int(v > 20)} for v in (23, 9, 8, 7, 6)]
        low = [{"max_ms": v, "misses": 0} for v in (4, 3, 3.5, 2, 1)]
        result = isolation.decide({"A": high, "B": low}, ("A", "B"))
        self.assertEqual(result["A>B"]["verdict"], "LOWER")
        self.assertEqual(result["runs_with_miss"], {"A": 1, "B": 0})
        self.assertNotIn("B>C", result)
        result = isolation.decide({"A": high, "B": high}, ("A", "B"))
        self.assertEqual(result["A>B"]["verdict"], "NOT SHOWN")
        result = isolation.decide({"A": high, "B": low[:3] + [None, None]}, ("A", "B"))
        self.assertEqual(result["A>B"]["verdict"], "INCONCLUSIVE")

    def test_run_metric_reads_all_flows(self):
        with tempfile.TemporaryDirectory() as directory:
            rig = Path(directory) / "baseline/rig"
            rig.mkdir(parents=True)
            for flow, lags in ((0, [1, 2]), (3, [23.05, .5])):
                (rig / f"udp-{flow}.json").write_text(json.dumps({"samples": [{"generator_lag_ms": v} for v in lags]}))
            metric = isolation.run_metric(directory)
            self.assertEqual((metric["n"], metric["max_ms"], metric["misses"]), (4, 23.05, 1))
            self.assertIsNone(isolation.run_metric(Path(directory) / "missing"))


class ArmTests(unittest.TestCase):
    def test_tracer_never_removes_an_instance_it_did_not_create(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory)
            (fake / "instances" / arm.INSTANCE).mkdir(parents=True)
            with patch.object(arm, "TRACEFS", fake):
                tracer = arm.Tracer(fake)
                with self.assertRaises(SystemExit):
                    tracer.start()
                tracer.remove()
            self.assertTrue((fake / "instances" / arm.INSTANCE).is_dir())

    def test_baseline_arm_code_is_unchanged_since_the_formal_attempt(self):
        root = Path(__file__).resolve().parents[2]
        known = subprocess.run(["git", "-C", str(root), "cat-file", "-e", arm.FORMAL_SHA + "^{commit}"], capture_output=True)
        if known.returncode:
            self.skipTest("formal commit not in this clone")
        changed = subprocess.run(["git", "-C", str(root), "diff", "--name-only", arm.FORMAL_SHA, "HEAD", "--", *arm.UNCHANGED],
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(changed, "")


if __name__ == "__main__":
    unittest.main()
