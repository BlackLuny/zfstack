#!/usr/bin/env python3
"""Plot final native-splice RTT observations; ranges are not confidence intervals."""
import argparse
import json
from pathlib import Path
import statistics


def plot(source, destination):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import ScalarFormatter
    data=json.loads(source.read_text());rows=data['rows']
    fig,axes=plt.subplots(2,2,figsize=(11,7.5),layout='constrained')
    panels=[(1,'mbps','Single-connection useful throughput','Mbps',False),
            (200,'user_equal_mean_ms','Loaded interactive-user mean','ms',True),
            (200,'worst_user_p99_ms','Loaded worst interactive-user P99','ms',True),
            (200,'stack_cpu_s_per_gb','Production driver + relay CPU','CPU seconds / GB',False)]
    handles=[];labels=[];missing=set()
    for ax,(n,key,title,ylabel,logy) in zip(axes.flat,panels):
        for cc,color,marker in [('cubic','#2563a6','o'),('bbr','#c56318','s')]:
            for arm,style,label in [('off','--','without aggregate pacing'),('p99','-','contention pacing / 99%')]:
                y=[];lo=[];hi=[]
                for rtt in [1,10,50,100,250,500]:
                    cell=[r for r in rows if (r['cc'],r['rtt'],r['heavy'],r['arm'])==(cc,rtt,n,arm)]
                    if len(cell)!=2 or not all(r['valid'] and r.get(key) is not None for r in cell):
                        y.append(float('nan'));lo.append(0);hi.append(0);missing.add((cc,rtt,n,arm));continue
                    values=[r[key] for r in cell];mid=statistics.median(values)
                    y.append(mid);lo.append(mid-min(values));hi.append(max(values)-mid)
                line=ax.errorbar([1,10,50,100,250,500],y,yerr=[lo,hi],color=color,linestyle=style,marker=marker,markersize=4,capsize=3,linewidth=1.5)
                if ax is axes.flat[0]:handles.append(line);labels.append(cc.upper()+': '+label)
        ax.set(xscale='log',xlabel='Base RTT (ms)',ylabel=ylabel,title=title)
        ax.set_xticks([1,10,50,100,250,500]);ax.xaxis.set_major_formatter(ScalarFormatter());ax.grid(True,alpha=.2)
        if logy:ax.set_yscale('log')
        else:ax.set_ylim(bottom=0)
    note=f'; {len(missing)} invalid cells omitted' if missing else ''
    fig.suptitle('Frozen V3 production Tokio / splice at 100 Mbps; identical FIFO in both arms\nTwo runs per arm/cell; bars show observed range, not CI'+note,fontsize=12)
    fig.legend(handles,labels,loc='outside lower center',ncol=2,frameon=False,fontsize=9)
    for suffix in ['.png','.svg']:
        target=destination.with_suffix(suffix)
        if target.exists():raise FileExistsError(target)
        fig.savefig(target,dpi=160)
        if suffix=='.svg':target.write_text('\n'.join(s.rstrip() for s in target.read_text().splitlines())+'\n')
    plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('destination',type=Path);a=p.parse_args()
    plot(a.source,a.destination)
