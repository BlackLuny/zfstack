#!/usr/bin/env python3
"""Plot completed CPU-retake estimates, same-setting noise and placement audit."""
import argparse
import json
from pathlib import Path


def plot(directory):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    data=json.loads((directory/'verdict.json').read_text())
    fig,axes=plt.subplots(2,2,figsize=(11,8),layout='constrained')
    colors={'original':'#2563a6','fixed':'#c56318'}
    def error(ax,value,y,color):
        mid=value['median_pct'];lo,hi=value['bootstrap_95_pct']
        ax.errorbar(mid,y,xerr=[[mid-lo],[hi-mid]],fmt='o',capsize=4,color=color)
    ax=axes[0,0]
    for i,placement in enumerate(['original','fixed']):error(ax,data['decisions'][placement]['cpu_effect'],i,colors[placement])
    ax.axvline(0,color='gray',linewidth=.8);ax.axvline(3,color='#b22d2d',linestyle='--',label='+3% margin')
    ax.axvline(data['historical_median_pct'],color='gray',linestyle=':',label='old two-pair +13.915%')
    ax.set(yticks=[0,1],yticklabels=['original placement','fixed placement'],ylim=(1.5,-.5),xlabel='CPU / useful GB change (%)',title='On/off CPU effect: 16 whole blocks per placement')
    ax.legend(fontsize=8,loc='lower right');ax.grid(axis='x',alpha=.2)
    ax=axes[0,1]
    for i,(placement,setting) in enumerate([(p,s) for p in ['original','fixed'] for s in ['off','on']]):
        a=data['aa'][placement][setting];values=a['values']
        ax.scatter(values,[i+(j%5-2)*.035 for j in range(len(values))],s=18,alpha=.6,color=colors[placement])
        ax.scatter([a['median']],[i],marker='D',s=35,color='black',zorder=3)
    ax.axvline(0,color='gray',linewidth=.8)
    ax.set(yticks=range(4),yticklabels=['original: off/off','original: on/on','fixed: off/off','fixed: on/on'],ylim=(3.5,-.5),
        xlabel='Later / earlier same-setting CPU change (%)',title='Same-setting variation (16 blocks each)');ax.grid(axis='x',alpha=.2)
    ax=axes[1,0]
    for offset,setting,color in [(-.16,'off','#2563a6'),(.16,'on','#c56318')]:
        vals=[data['diagnostics'][p][setting]['driver_observed_migrations'] for p in ['original','fixed']]
        mids=[v['median'] if v else float('nan') for v in vals]
        ax.bar([i+offset for i in range(2)],mids,width=.30,color=color,label=setting)
        for i,value in enumerate(mids):ax.annotate(f'{value:g}',(i+offset,value),ha='center',va='bottom',fontsize=9)
    ax.set(xticks=[0,1],xticklabels=['original placement','fixed placement'],ylabel='Median driver migrations / observed span',title='Post-assignment scheduler counter audit')
    ax.legend(fontsize=8);ax.grid(axis='y',alpha=.2)
    ax=axes[1,1]
    bounds=[]
    for i,p in enumerate(['original','fixed']):
        v=data['decisions'][p]['throughput_effect'];error(ax,v,i,colors[p]);bounds.extend(v['bootstrap_95_pct'])
    ax.axvline(0,color='gray',linewidth=.8);ax.axvline(-3,color='#b22d2d',linestyle='--')
    ax.set(yticks=[0,1],yticklabels=['original placement','fixed placement'],ylim=(1.5,-.5),xlabel='Useful throughput change (%)',
        title='Matched useful work',xlim=(min(-3.5,min(bounds)-.2),max(.5,max(bounds)+.2)));ax.grid(axis='x',alpha=.2)
    fig.suptitle('Frozen V5 / CUBIC / 50 ms / one flow / 100 Mbps FIFO\n8-s warmup, 20-s window; ABBA/BAAB blocks; pointwise whole-round 95% intervals',fontsize=12)
    for suffix in ['png','svg']:
        target=directory/('cpu-retest.'+suffix)
        if target.exists():raise FileExistsError(target)
        fig.savefig(target,dpi=160)
        if suffix=='svg':target.write_text('\n'.join(s.rstrip() for s in target.read_text().splitlines())+'\n')
    plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args();plot(a.directory.resolve())
