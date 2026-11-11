# Inspection of the exact frozen executable

Executable SHA256:
`96695f880dd600172e3dfa5b32304da0fa296579357785b5f83dbb87ea1a26c5`.
GNU objdump 2.44; commands `objdump -dC -Mintel <binary>` and
`objdump -tC <binary>`. Full old/V5 disassembly, symbol tables, hashes and
all four call-site excerpts are preserved under the preparation directory in
the evidence archive. These were captured between preflight and formal runs.
They are actual fat-LTO V5 instructions, not a separately optimized mock.

## Inactive entrance avoids the cold pointer

In the service instance starting at `0x14c000`, the scheduler entrance includes:

```asm
14ebc5: mov    rbx,QWORD PTR [r14+0x18] ; ready peer count
14ebc9: mov    r15,QWORD PTR [r14+0x68] ; configured rate
14ebcd: cmp    rbx,0x2
14ebd1: setae  al
14ebd4: xor    ebp,ebp                 ; aggregate-active verdict = false
14ebd6: mov    rcx,0xffffffffffffffff  ; unlimited allowance
14ebdd: test   r15,r15
14ebe0: je     14ee83                 ; rate zero bypass
14ebe6: or     al,BYTE PTR [r14+0x78]  ; competing OR cached active
14ebea: test   al,0x1
14ebec: je     14ee83                 ; configured but inactive bypass
14ebf2: mov    r12,QWORD PTR [r14+0x70]; cold pointer, only after that gate
```

The two bypasses join the same block with unlimited allowance and a false
aggregate-active verdict. The configured/inactive path in **this entrance**
executes three additional instructions (`or`, `test`, `je`) versus rate zero,
including a hot-state byte load. It does not follow the optional clock pointer.
The cold allocation is further down at `0x14ec6a`: `mov edi,0x60`, followed by
`malloc` at `0x14ec6f`, consistent with the 96-byte lazy clock.

This does not count all instructions of an entire request, quantify cycles,
compare cache behavior with the pre-change executable, or establish an exact
three-instruction end-to-end overhead. The rate branch also takes a different
direction. In particular this is evidence against a hidden allocation or
128-bit pacing arithmetic on the inactive entrance, not proof of zero CPU.

## Paced loops remain outlined

The executable contains four concrete `Shard::serve_paced` specializations:

| Address | Size (bytes) | Calling instruction |
|---|---:|---|
| `0x15ec60` | 5,041 | `0x14f14c` |
| `0x160020` | 5,098 | `0x155321` |
| `0x162890` | 5,253 | `0x15b647` |
| `0x161410` | 5,238 | `0x166359` |

The first three callers are native/adapter `Driver::service` instances; the
fourth is the bare-stack benchmark poll path. For the first instance:

```asm
14f114: test   bpl,bpl
14f11c: je     14f4a1  ; inlined unpaced service
...
14f14c: call   15ec60  ; outlined aggregate service
```

The exact code therefore retains the intended separation: inactive service
selects the ordinary inlined loop, while aggregate wire-byte accounting stays
behind the active verdict. Source-level constant specialization explains the
unpaced loop's lack of aggregate accounting; symbol presence/size alone would
not establish that property. The old executable has no `serve_paced` symbol;
the different monomorphizations and layout prevent attributing an old/V5 cycle
delta merely from comparing function sizes.

## No hardware instruction-count claim

A setup-only `perf_event_open` probe for user-space retired instructions
returned `ENOENT` in this managed kernel. The probe and error are retained;
no PMU collection, privilege/sysctl change or sampling ran during benchmarks.
Static code inspection and measured window CPU remain separate evidence.
