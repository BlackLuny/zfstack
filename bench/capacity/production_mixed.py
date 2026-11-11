#!/usr/bin/env python3
"""Exercise the frozen native-splice binary with an external upload peer."""
import argparse
import json
from pathlib import Path
import random
import socket
import struct
import subprocess
import time

import production_pacing as pacing
import rtt_matrix as rig
from run import ROOT, counters, save, sha


def uploader(destination, stop):
    started = time.time()
    cpu = time.process_time()
    observations = []
    result = dict(start_unix=started, errors=[], samples=observations)
    deadline = time.monotonic() + 120
    sock = None
    try:
        # Capture only setup. Do not leave a packet-copy observer on the data path.
        # Outgoing device taps are registered through ETH_P_ALL, not the
        # receive-side ETH_P_IP handler. The triggering header is outgoing.
        tap = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x0003))
        tap.settimeout(.1)
        while True:
            try:
                if not int(Path('/sys/class/net/zfbA/flags').read_text().strip(),16) & 1:
                    raise OSError('TUN not up yet')
                tap.bind(('zfbA', 0))
                break
            except OSError:
                if time.monotonic() >= deadline or stop.exists():
                    raise TimeoutError('TUN did not appear')
                time.sleep(.02)
        while True:
            if time.monotonic() >= deadline or stop.exists():
                raise TimeoutError('heavy download header did not appear')
            try:
                packet = tap.recv(65535)
            except TimeoutError:
                continue
            if len(packet) < 40 or packet[0] >> 4 != 4 or packet[9] != 6 or packet[12:16] != bytes([10,210,1,1]):
                continue
            ip = (packet[0] & 15) * 4
            if len(packet) < ip + 20:
                continue
            tcp = (packet[ip+12] >> 4) * 4
            if packet[ip+tcp:ip+tcp+2] == b'ZD':
                result['heavy_header_unix'] = time.time()
                break
        tap.close()
        subprocess.run(['ip','addr','replace','10.210.5.1/32','dev','zfbA'], check=True)
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 << 20)
        result['socket_sndbuf_bytes'] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        result['tcp_congestion'] = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 32).rstrip(b'\0').decode()
        sock.settimeout(20)
        sock.bind(('10.210.5.1', 0))
        sock.connect(('10.211.0.2',5201))
        sock.sendall(b'ZU' + bytes(14))
        sock.settimeout(.1)
        result['connected_unix'] = time.time()
        payload = memoryview(bytes(range(251)) * 256)
        sent = offset = 0
        next_sample = 0
        while not stop.exists() and time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_sample:
                info = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
                observations.append(dict(at_unix=time.time(), sent=sent,
                    acked=struct.unpack_from('=Q',info,120)[0], cpu_sec=time.process_time()-cpu))
                next_sample = now + .25
            try:
                n = sock.send(payload[offset:])
            except TimeoutError:
                continue
            if not n:
                raise ConnectionError('upload send returned zero')
            sent += n
            offset = (offset + n) % len(payload)
        result['termination'] = 'stop_marker' if stop.exists() else 'bounded_duration'
    except (ConnectionResetError, BrokenPipeError) as error:
        result['termination'] = type(error).__name__
    except Exception as error:
        result['errors'].append(f'{type(error).__name__}: {error}')
    finally:
        if sock is not None:
            sock.close()
        result.update(end_unix=time.time(), cpu_total_sec=time.process_time()-cpu)
        save(destination, result)


def sequence(smoke):
    rng = random.Random(2026102407)
    cells = [('cubic',20,4)] if smoke else [(cc,rtt,200) for cc in ('cubic','bbr') for rtt in (1,20,500)]
    rng.shuffle(cells)
    rows=[]
    for cc,rtt,n in cells:
        arms=['off','p99'];rng.shuffle(arms)
        for rep in range(1 if smoke else 2):
            for pos,arm in enumerate(arms if rep%2==0 else arms[::-1]):
                row=rig.case(cc,rtt,n,20)
                row.update(warmup=max(8,round(40*rtt/1000)), index=len(rows),
                    block=f'{cc}-r{rtt}-n{n}-pair{rep}', position=pos, arm=arm,
                    retry_budget=1, egress_rate=0 if arm=='off' else 12_375_000,
                    tokio_stack=True, splice=True, upload_peer=5)
                rows.append(row)
    return rows


def analyze(out):
    result=pacing.analyze(out)
    for row in result['rows']:
        side=json.loads((out/'upload'/f"{row['index']:03d}.json").read_text())
        raw=json.loads((out/row['name']/'stdout.json').read_text())
        end=json.loads((out/row['name']/'end.json').read_text())
        received=raw['server_app']['rx_bytes']-256*row.get('planned_requests',0)
        samples=side['samples']
        row.update(upload_received_total=received, uploader_cpu_total_sec=side['cpu_total_sec'],
            upload_acked_total=samples[-1]['acked'] if samples else 0)
        recent=[s for s in samples if s['at_unix'] >= samples[-1]['at_unix']-row['measure']+1] if samples else []
        row['upload_recent_acked_bytes']=recent[-1]['acked']-recent[0]['acked'] if len(recent)>1 else 0
        gap=max_gap=0
        for before,after in zip(samples,samples[1:]):
            gap=gap+after['at_unix']-before['at_unix'] if after['acked']==before['acked'] else 0
            max_gap=max(max_gap,gap)
        row['upload_max_observed_no_ack_sec']=max_gap
        row['errors'].extend(side['errors'])
        if received < 1 << 20 or len(samples)<2:
            row['errors'].append('upload did not deliver enough observable data')
        if samples and (samples[-1]['at_unix']-samples[0]['at_unix'] < row['measure'] or side['end_unix'] < end['end_unix']-2):
            row['errors'].append('uploader did not span the loaded interval')
        if side.get('termination')=='bounded_duration':
            row['errors'].append('uploader exceeded its duration bound')
        if row['upload_recent_acked_bytes']<=0:
            row['errors'].append('upload made no acknowledged progress near the loaded interval')
        row['valid']=not row['errors']
        for key in list(row):
            if 'cpu_s_per_gb' in key:del row[key]
    for comparison in result['comparisons']:
        for key in list(comparison['changes']):
            if 'cpu_s_per_gb' in key:del comparison['changes'][key]
        group=[r for r in result['rows'] if (r['cc'],r['rtt'],r['heavy'])==(comparison['cc'],comparison['rtt'],comparison['heavy'])]
        comparison['valid']=all(r['valid'] for r in group)
    result['valid']=sum(r['valid'] for r in result['rows'])
    result['cpu_note']='External upload is absent from the main effective-byte denominator; CPU/GB is intentionally omitted.'
    return result


def run(out, container, smoke):
    out.relative_to(ROOT);out.mkdir(parents=True, exist_ok=False)
    (out/'upload').mkdir()
    binary=ROOT/'target/release/zfbench-production-v3-20261024';digest=sha(binary)
    sources={str(f.relative_to(ROOT)):sha(f) for parent,pattern in [(ROOT/'src','*.rs'),(ROOT/'bench/src','*.rs'),(ROOT/'bench/capacity','*.py'),(ROOT/'docs/experiments/2026-10-24-production-pacing','*.md')] for f in sorted(parent.rglob(pattern))}
    for name in sources:
        dest=out/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    rows=sequence(smoke)
    save(out/'plan.json',dict(phase='mixed-smoke' if smoke else 'mixed', rows=rows,
        binary_sha256=digest,sources=sources,environment=counters(),
        base_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()))
    for row in rows:
        stop=out/'upload'/f"{row['index']:03d}.stop"
        target=out/'upload'/f"{row['index']:03d}.json"
        argv=['docker','exec',container,'taskset','-c','0-3','python3','/work/bench/capacity/production_mixed.py',
            '--sidecar',f'/work/{target.relative_to(ROOT)}','--stop',f'/work/{stop.relative_to(ROOT)}']
        save(out/'upload'/f"{row['index']:03d}-start.json",dict(argv=argv,start_unix=time.time()))
        with (out/'upload'/f"{row['index']:03d}.log").open('x') as log:
            process=subprocess.Popen(argv,stdout=log,stderr=log)
            try:
                rig.execute(out,row,container,binary,digest)
            finally:
                stop.write_text('benchmark ended\n')
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError('external uploader did not stop; no next attempt')
    save(out/'complete.json',dict(end_unix=time.time()))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('out',type=Path,nargs='?');p.add_argument('--smoke',action='store_true')
    p.add_argument('--analyze',action='store_true');p.add_argument('--container',default='zfstack-production-20261024')
    p.add_argument('--sidecar',type=Path);p.add_argument('--stop',type=Path)
    a=p.parse_args()
    if a.sidecar:uploader(a.sidecar,a.stop)
    elif a.analyze:print(json.dumps(analyze(a.out.resolve()),indent=2))
    else:run(a.out.resolve(),a.container,a.smoke)
