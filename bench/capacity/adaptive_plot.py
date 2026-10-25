#!/usr/bin/env python3
"""Standalone scientific figures from the completed, unedited adaptive study."""
import argparse
import json
from pathlib import Path
import statistics

import adaptive_cases as rig
from adaptive_analyze import protected
from adaptive_artifacts import done
from adaptive_dynamic import throughput


def render(directory):
    done();rig.CANDIDATE='a1'
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    def emit(fig,name):
        for ext in ['png','svg']:
            path=directory/(name+'.'+ext)
            if path.exists():raise FileExistsError(path)
            fig.savefig(path,dpi=160,bbox_inches='tight')
            if ext=='svg':path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines())+'\n')
        plt.close(fig)
    d=json.loads((directory/'primary-comparisons.json').read_text());cells=d['cells']
    ordered=sorted(cells,key=lambda k:(cells[k]['heavy'],cells[k]['rtt']))
    fig,axes=plt.subplots(1,2,figsize=(14,8),sharey=True,layout='constrained')
    for row,case in enumerate(ordered):
        c=cells[case];ch=c['changes'];color='#257b59' if c['observed_valid'] and protected(ch) else '#b0791b' if c['observed_valid'] else '#888888'
        for ax,metric in zip(axes,['mbps','stack_cpu_s_per_gb']):
            m=ch[metric];a,b=m['bootstrap_95_pct'];low,high=m['simultaneous_95_pct']
            ax.hlines(row,low,high,color='#c3c9ce',lw=1.6,label='Simultaneous 95% band' if row==0 else None)
            ax.hlines(row,a,b,color=color,lw=3.2,label='Per-cell 95% interval' if row==0 else None)
            ax.plot(m['geometric_mean_pct'],row,'o',color=color,ms=4)
    labels=[f"BBR / {cells[k]['rtt']} ms / "+('single' if cells[k]['heavy']==1 else '8 bulk + 3 interactive')+(' [incomplete]' if not cells[k]['observed_valid'] else '') for k in ordered]
    axes[0].set_yticks(range(len(ordered)),labels);axes[0].invert_yaxis()
    for ax,margin,label in zip(axes,[-3,3],['Useful throughput change (%)','Driver / relay CPU per useful GB change (%)']):
        ax.axvline(0,color='#888',lw=1);ax.axvline(margin,color='#c22',ls='--',lw=1);ax.set_xlabel(label);ax.grid(axis='x',alpha=.18)
    axes[1].legend(loc='best',fontsize=8)
    fig.suptitle('Unknown-capacity adaptive pacing A1: eight balanced pairs per 100-Mb/s case\nGreen: both per-cell guards protected; amber: unconfirmed; grey: incomplete observations',fontsize=12)
    emit(fig,'primary-protection')
    contested=[k for k in ordered if cells[k]['heavy']>1]
    fig,axes=plt.subplots(1,2,figsize=(12,5),sharey=True,layout='constrained')
    for y,k in enumerate(contested):
        for ax,metric in zip(axes,['user_equal_mean_ms','worst_user_p99_ms']):
            m=cells[k]['changes'].get(metric)
            if m is None:ax.text(0,y,'incomplete arrivals',color='#888',va='center');continue
            a,b=m['bootstrap_95_pct'];color='#2866a4' if cells[k]['observed_valid'] else '#888'
            ax.hlines(y,a,b,color=color,lw=2.5);ax.plot(m['geometric_mean_pct'],y,'o',color=color)
    axes[0].set_yticks(range(len(contested)),[f"BBR / {cells[k]['rtt']} ms" for k in contested]);axes[0].invert_yaxis()
    for ax,label in zip(axes,['Interactive user-equal mean change (%)','Worst interactive-user P99 change (%)']):
        ax.axvline(0,color='#888',lw=1);ax.axvline(5,color='#c22',lw=1,ls='--');ax.set_xlabel(label);ax.grid(axis='x',alpha=.2)
    fig.suptitle('Eight bulk connections of one peer + three interactive peers\nPlanned-arrival latency; pointwise whole-round 95% intervals',fontsize=12)
    emit(fig,'primary-latency')
    for phase in ['dynamic','demand']:
        summary=json.loads((directory/(phase+'-summary.json')).read_text());cases=sorted({r['case'] for r in summary['rows']},key=lambda k:next(r['rtt'] for r in summary['rows'] if r['case']==k))
        fig,axes=plt.subplots(len(cases),1,figsize=(12,4*len(cases)),squeeze=False,layout='constrained')
        grid=np.arange(0,48.001,.25)
        for ax,case in zip(axes.flat,cases):
            rtt=next(r['rtt'] for r in summary['rows'] if r['case']==case)
            for arm,color,label in [('off','#356ab7','Off'),('auto','#d77b1f','Adaptive A1')]:
                curves=[]
                for row in summary['rows']:
                    if row['case']!=case or row['arm']!=arm:continue
                    raw=json.loads((rig.output(phase)/rig.name(row)/'stdout.json').read_text());p=raw['results']['heavy']['progress_250ms']
                    curves.append([throughput(p,float(t),float(t+2)) for t in grid])
                values=np.array(curves);ax.plot(grid+2,np.median(values,axis=0),label=label,color=color,lw=1.5)
                low,high=np.quantile(values,[.25,.75],axis=0);ax.fill_between(grid+2,low,high,color=color,alpha=.15)
            if phase=='dynamic':
                ax.step([0,10,25,50],[100,20,100,100],where='post',color='#777',ls='--',label='Configured IP capacity')
                for at in [10,25]:ax.axvline(at,color='#999',lw=.8)
            else:ax.axvspan(8,28,color='#999',alpha=.15,label='Upstream producers paused; buffers may drain')
            ax.set_title(f'BBR / RTT {rtt} ms; four independent pairs');ax.set_xlabel('Useful-window time (s), plotted at end of each two-second interval');ax.set_ylabel('Useful bulk throughput (Mb/s)');ax.grid(alpha=.18);ax.legend(fontsize=8,loc='best')
        fig.suptitle('Native kernel TCP through production Tokio/splice: descriptive pair median and interquartile range\nConfigured IP capacity and useful payload throughput have different byte accounting',fontsize=12)
        emit(fig,phase+'-throughput')
    return ['primary-protection','primary-latency','dynamic-throughput','demand-throughput']


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args();print(json.dumps(render(a.directory.resolve())))
