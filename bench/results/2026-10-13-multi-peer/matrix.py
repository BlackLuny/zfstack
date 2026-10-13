from run import run

failed = []
for mode in ["fanout", "noisy"]:
    for mtu in [1500, 512]:
        for trim in [0, 1]:
            for pair in range(2):
                result = run(mode, mtu, 2, trim, pair, "matrix")
                if not result or not result["ok"]:
                    failed.append((mode, mtu, trim, pair))
for mtu in [1500, 512]:
    for trim in [0, 1]:
        result = run("fanout", mtu, 2, trim, 0, "decay", extra=["--idle-secs", "12", "--low-secs", "12"])
        if not result or not result["ok"]:
            failed.append(("decay", mtu, trim))
if failed:
    raise RuntimeError(failed)
