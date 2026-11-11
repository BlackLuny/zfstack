#!/usr/bin/env python3
"""Plot V5 run-level paired intervals; no request-level pseudo-replication."""
import argparse
import json
from pathlib import Path


def plot(directory):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    peak=json.loads((directory/'peak-summary.json').read_text())
    loaded=json.loads((directory/'loaded-summary.json').read_text())
    fig,axes=plt.subplots(2,2,figsize=(11,8),layout='constrained')
    def point(ax,y,value,color,label=None):
        mid=value['median_pct'];lo,hi=value['bootstrap_95_pct']
        ax.errorbar(mid,y,xerr=[[mid-lo],[hi-mid]],fmt='o',color=color,capsize=4,label=label)
    for ax,key,title,limit in [(axes[0,0],'mbps','Physical peak useful throughput',-3),
            (axes[0,1],'service_cpu_s_per_gb','Physical proxy CPU per useful GB',3)]:
        for i,n in enumerate([1,2]):
            for left,offset,color,label in [('old',-.12,'#2563a6','old -> V5'),('v4',.12,'#9a4ca5','V4 -> V5')]:
                c=next(c for c in peak['comparisons'] if c['case']==f'client_down{n}_shared' and (c['left'],c['right'])==(left,'v5'))
                point(ax,i+offset,c['changes'][key],color,label if i==0 else None)
        ax.axvline(0,color='gray',linewidth=.8);ax.axvline(limit,color='#b22d2d',linestyle='--',label=f'{limit:+d}% guard')
        ax.set(yticks=[0,1],yticklabels=['1 download','2 downloads'],xlabel='Paired median change (%)',title=title,ylim=(1.5,-.5))
        ax.grid(axis='x',alpha=.2);ax.legend(fontsize=8,loc='lower right')
    ax=axes[1,0]
    metrics=[('user_equal_mean_ms','User mean'),('worst_user_p99_ms','Worst user P99'),
        ('stack_cpu_s_per_gb','Driver + relay CPU/GB'),('process_cpu_s_per_gb','Benchmark process CPU/GB')]
    for i,(key,label) in enumerate(metrics):
        for cc,offset,color in [('cubic',-.12,'#2563a6'),('bbr',.12,'#c56318')]:
            c=next(c for c in loaded['comparisons'] if c['cc']==cc)
            point(ax,i+offset,c['changes'][key],color,cc.upper() if i==0 else None)
    ax.axvline(0,color='gray',linewidth=.8)
    ax.set(yticks=range(4),yticklabels=[label for _,label in metrics],xlabel='Paired median change (%)',
        title='V5 loaded off/on: latency and CPU',ylim=(3.5,-.5));ax.grid(axis='x',alpha=.2);ax.legend(fontsize=8)
    ax=axes[1,1]
    for i,(cc,color) in enumerate([('cubic','#2563a6'),('bbr','#c56318')]):
        c=next(c for c in loaded['comparisons'] if c['cc']==cc);point(ax,i,c['changes']['mbps'],color)
    ax.axvline(0,color='gray',linewidth=.8);ax.axvline(-3,color='#b22d2d',linestyle='--')
    ax.set(yticks=[0,1],yticklabels=['CUBIC','BBR'],xlabel='Paired median useful-throughput change (%)',
        title='V5 loaded throughput cost',xlim=(-3.5,.5),ylim=(1.5,-.5));ax.grid(axis='x',alpha=.2)
    fig.suptitle('V5 cold state / outlined service: independent comparisons\n12 blocks or pairs per case; whole-run bootstrap 95% intervals',fontsize=13)
    for suffix in ['png','svg']:
        target=directory/('confirmation.'+suffix)
        if target.exists():raise FileExistsError(target)
        fig.savefig(target,dpi=160)
        if suffix=='svg':target.write_text('\n'.join(line.rstrip() for line in target.read_text().splitlines())+'\n')
    plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args();plot(a.directory.resolve())
