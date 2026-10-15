"""Fixed L2 physical paths; linking a transport is never execution coverage."""


def client_nodes(outbound="direct"):
    if outbound not in ("direct", "relay"):
        raise ValueError("L2 outbound must be direct or relay")
    # fwd1 is the Hammer endpoint in relay mode. Giving it a client wg0 route
    # to target would send its forwarded traffic back into the worker.
    return ["client", "fwd4" if outbound == "relay" else "fwd1", "fwd2", "fwd3"]


def inspect_path(result):
    errors, edges = [], {}
    config = result.get("effective_config", {})
    outbound = config.get("outbound", "direct")
    try:
        clients = client_nodes(outbound)
    except ValueError as error:
        return ["WG path: " + str(error)], edges
    if outbound == "relay" and config.get("topology", {}).get("client_nodes") != clients:
        errors.append("WG relay path client roles differ from the frozen topology")
    if outbound == "relay" and config.get("probe_port") != 19002:
        errors.append("WG relay path must separate the health-probe and business ports")
    evidence = result.get("path_evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("before"), dict) or not isinstance(evidence.get("after"), dict):
        return errors + ["missing raw WG path-counter evidence"], edges
    if type(evidence.get("startup_ports")) is not int or evidence["startup_ports"] < 2:
        errors.append("missing production ZFStack startup evidence for both ports")
    for destination, counts in evidence["after"].items():
        if not isinstance(counts, dict):
            errors.append("malformed path counters")
            continue
        for name, row in counts.items():
            before = evidence["before"].get(destination, {}).get(name, {}).get("bytes")
            after = row.get("bytes") if isinstance(row, dict) else None
            if type(before) is not int or type(after) is not int or before < 0 or after < before:
                errors.append(f"missing, negative or reset path counter {destination}/{name}")
            else:
                edges[f"{destination}/{name}"] = after - before
    for client in clients:
        if edges.get(f"target/from_{client}") != 0 or edges.get(f"worker/from_{client}", 0) <= 1000:
            errors.append(f"missing or bypassed WG path for {client}")
    amount = result.get("heavy_bytes")
    required = ("worker/from_target",) if outbound == "direct" else ("fwd1/from_target", "worker/from_fwd1")
    if type(amount) is not int or amount <= 0 or any(edges.get(edge, -1) < amount for edge in required):
        errors.append("declared outbound path does not account for verified payload")
    if outbound == "relay":
        # These dedicated counters match the application port, so harmless
        # health probes to other targets cannot excuse a Direct shortcut.
        dedicated = evidence.get("relay_business")
        if not isinstance(dedicated, dict):
            errors.append("missing relay business path counters")
        else:
            for key in ("worker_direct", "forwarder_target"):
                before, after = dedicated.get("before", {}).get(key), dedicated.get("after", {}).get(key)
                if type(before) is not int or type(after) is not int or before < 0 or after < before:
                    errors.append("missing, negative or reset relay business path counter: " + key)
                elif (key == "worker_direct" and after != before) or (key == "forwarder_target" and after <= before):
                    errors.append("relay business path bypassed or carried no application traffic: " + key)
        if evidence.get("outbound") != "relay" or evidence.get("forwarder_alive") is not True:
            errors.append("relay path lacks the live forwarder execution identity")
    return sorted(set(errors)), edges
