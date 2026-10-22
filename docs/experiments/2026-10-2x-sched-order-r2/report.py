#!/usr/bin/env python3
"""Render frozen comparisons, including negative samples and model sensitivity."""
import collections,csv,gzip,json,pathlib,statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
OUT=pathlib.Path(__file__).resolve().parent
load=lambda n:json.loads((OUT/n).read_text())
ins=load('instruction-comparisons.json');cpu=load('cpu-summary.json');attr=load('attribution.json')
with gzip.open(OUT/'user-summary.jsonl.gz','rt') as f:users=[json.loads(x) for x in f]
token=list(csv.DictReader((OUT/'C-token-users.csv').open()))
fmt=lambda x,n=2:'N/A' if x is None else f'{x:+.{n}f}%'
num=lambda x,n=3:'N/A' if x is None else f'{x:.{n}f}'
C={(r['phase'],r['case'],r['comparison'],r['tick_us']):r for r in cpu}
I={(r['comparison'],r['case'],r['tick_us']):r for r in ins}

def lights(case,mode,tick,reference=False):
 if reference:return [u for u in users if u['phase']=='C' and u['case']==case and u['stage']=='control' and u['user_class']=='light']
 return [u for u in users if u['phase']=='B' and u['case']==case and u['mode']==mode and u['tick_us']==tick and u['user_class']=='light']
def p99(us):return max(u['p99_us'] for u in us)
curve=[]
for case in ['mixed-1','mixed-16','mixed-64']:
 for tick in [1000,100]:
  if case=='mixed-1' and tick==100:continue
  for mode in ['drr-16k','adaptive-16k','adaptive-32k']:
   base=lights(case,'drr-max',tick);target=lights(case,mode,tick)
   c=C[('cpu-B',case,mode if tick==1000 else 'fine-'+mode,tick)];i=I[('B-'+mode,case,tick)]
   gain=(p99(base)-p99(target))/1000
   curve.append(dict(reference='DRR-max',case=case,tick_us=tick,mode=mode,p99_reference_by_peer=json.dumps({u['peer']:u['p99_us'] for u in base}),p99_policy_by_peer=json.dumps({u['peer']:u['p99_us'] for u in target}),worst_p99_reduction_ms=gain,cpu_delta_pct=c['cpu_sec_per_GB_delta_pct'],cpu_ci_low_pct=c['cpu_sec_per_GB_ci_low_pct'],cpu_ci_high_pct=c['cpu_sec_per_GB_ci_high_pct'],AA_max_abs_pct=c['AA_max_abs_pct'],cpu_attribution=c['cpu_attribution'],Ir_delta_pct=i['mean_delta_pct'],CPU_pct_per_1ms_point=c['cpu_sec_per_GB_delta_pct']/gain if gain>0 else None))
for case in ['mixed-16','mixed-64']:
 for mode in ['drr-max','drr-16k','adaptive-16k','adaptive-32k']:
  base=lights(case,'off',100,True);target=lights(case,mode,100);c=C[('cpu-C',case,'fine-control-'+mode,100)];i=I[('C-control-vs-'+mode,case,100)];gain=(p99(base)-p99(target))/1000
  curve.append(dict(reference='main+e21',case=case,tick_us=100,mode=mode,p99_reference_by_peer=json.dumps({u['peer']:u['p99_us'] for u in base}),p99_policy_by_peer=json.dumps({u['peer']:u['p99_us'] for u in target}),worst_p99_reduction_ms=gain,cpu_delta_pct=c['cpu_sec_per_GB_delta_pct'],cpu_ci_low_pct=c['cpu_sec_per_GB_ci_low_pct'],cpu_ci_high_pct=c['cpu_sec_per_GB_ci_high_pct'],AA_max_abs_pct=c['AA_max_abs_pct'],cpu_attribution=c['cpu_attribution'],Ir_delta_pct=i['mean_delta_pct'],CPU_pct_per_1ms_point=c['cpu_sec_per_GB_delta_pct']/gain if gain>0 else None))
with (OUT/'tradeoff.csv').open('w') as f:w=csv.DictWriter(f,fieldnames=list(curve[0]));w.writeheader();w.writerows(curve)
colors={'drr-16k':'#4169a1','adaptive-16k':'#26856b','adaptive-32k':'#c66c34','drr-max':'#777777'}
fig,axes=plt.subplots(2,2,figsize=(12,8),layout='constrained')
for row,tick in enumerate([1000,100]):
 for col,case in enumerate(['mixed-16','mixed-64']):
  ax=axes[row,col];points=[r for r in curve if r['reference']=='DRR-max' and r['case']==case and r['tick_us']==tick]
  aa=points[0]['AA_max_abs_pct']
  if aa is not None:ax.axhspan(-aa,aa,color='#888888',alpha=.12,label='A/A max-abs range')
  for j,r in enumerate(points):
   ax.errorbar(r['worst_p99_reduction_ms'],r['cpu_delta_pct'],yerr=[[r['cpu_delta_pct']-r['cpu_ci_low_pct']],[r['cpu_ci_high_pct']-r['cpu_delta_pct']]],fmt='o',color=colors[r['mode']],capsize=4,label=r['mode']+' (Ir '+fmt(r['Ir_delta_pct'])+')')
  ax.scatter([0],[0],marker='x',color='black',label='DRR-max reference');ax.legend(loc='upper left',fontsize=8,framealpha=.85);ax.axhline(0,color='black',lw=.6);ax.set_title(f'{case}, sink refill {tick} us (reset model)');ax.set_xlabel('Worst light-peer P99 reduction vs DRR-max (ms)');ax.set_ylabel('CPU / effective GB change (%)');ax.grid(alpha=.2);ax.margins(x=.35,y=.3)
fig.suptitle('L0 CPU / tail-latency tradeoff; error bars: paired 95% CI')
fig.savefig(OUT/'tradeoff.svg');fig.savefig(OUT/'tradeoff.png',dpi=150);plt.close(fig)
# A separate plot keeps the true baseline and driver model explicit.
fig,axes=plt.subplots(1,2,figsize=(11,4),layout='constrained')
for ax,case in zip(axes,['mixed-16','mixed-64']):
 for j,r in enumerate(x for x in curve if x['reference']=='main+e21' and x['case']==case):
  ax.errorbar(r['worst_p99_reduction_ms'],r['cpu_delta_pct'],yerr=[[r['cpu_delta_pct']-r['cpu_ci_low_pct']],[r['cpu_ci_high_pct']-r['cpu_delta_pct']]],fmt='o',color=colors[r['mode']],capsize=4,label=r['mode'])
 ax.legend(loc='upper left',fontsize=8);ax.axhline(0,color='black',lw=.6);ax.axvline(0,color='black',lw=.6);ax.set_title(case+', reset100us');ax.set_xlabel('Worst P99 reduction vs main+counter (ms)');ax.set_ylabel('CPU / effective GB change (%)');ax.grid(alpha=.2);ax.margins(x=.5,y=.2)
fig.savefig(OUT/'fine-control-reference.svg');plt.close(fig)
fig,axes=plt.subplots(1,2,figsize=(11,4.5),layout='constrained')
policies=[('token-control','off','control'),('token-adaptive','drr-max','DRR-max'),('token-adaptive','drr-16k','DRR16'),('token-adaptive','adaptive-16k','adaptive16'),('token-adaptive','adaptive-32k','adaptive32')]
for ax,case in zip(axes,['mixed-16','mixed-64']):
 for j,(stage,mode,label) in enumerate(policies):
  rs=[r for r in token if r['case']==case and r['stage']==stage and r['mode']==mode and r['user_class']=='light' and r['repeat']=='0']
  rs.sort(key=lambda r:int(r['peer']));values=[float(r['p99_us'])/1000 for r in rs]
  ax.bar([k+(j-2)*.15 for k in range(3)],values,width=.15,label=label)
 ax.set_xticks([0,1,2],['Peer2','Peer3','Peer4']);ax.set_ylabel('P99 (virtual ms)');ax.set_ylim(0,3.4);ax.set_title(case+', 100us credit carry');ax.grid(axis='y',alpha=.2)
axes[1].legend(fontsize=8,ncols=2);fig.suptitle('Fixed64KiB token bucket, exact64KiB/ms; three identical repeats')
fig.savefig(OUT/'token-fine-p99.svg');fig.savefig(OUT/'token-fine-p99.png',dpi=150);plt.close(fig)

A=[r for r in ins if r['comparison']=='A-gate'];quiet=[r for r in ins if r['comparison'] in ['B-adaptive-16k','B-adaptive-32k'] and r['baseline_has_zero_local_pressure']]
D=[r for r in ins if r['comparison']=='D-counters']
parts=['<!-- GENERATED RESULTS -->','## 确定性成本、重复波动与决定','']
parts += [f'A 的 39 项中，0.5% 指令非劣效条件在三次观测极值内全部满足；严格下降条件仅 {sum(r["A_instruction_gate_pass"] for r in A)}/39 通过。以下七项虽然减少白调度，总 Ir/有效 GB 仍上升，因此不作为默认行为候选。CPU 的辅助结果不用于推翻这一门禁。','', '| 场景 | 原 serve_zero | Ir/GB 三次均值变化 |','|---|---:|---:|']
for r in A:
 if not r['A_instruction_gate_pass']:parts.append(f"| {r['case']} | {r['serve_zero_A']} | {fmt(r['mean_delta_pct'],4)} |")
parts += ['', '新增长度检查的三次诊断构建均值如下。调用次数和函数成本为两端合计；主门槛仍用正常优化构建。诊断 noinline 改变优化形态，不把下表成本直接从主构建 Ir 中相减；完整 schedule/serve 调用数、self/inclusive Ir/次及全程序摊销 Ir/次见 [attribution.csv](attribution.csv)。空服务被删除后，剩余 serve 的平均 Ir/次可上升，这不等于有效发送变贵。','', '| 场景 | 新长度检查调用/次运行 | 其中 wants_tx 调用 | wants_tx 中 Ir/调用 | 正常构建净 Ir/GB |','|---|---:|---:|---:|---:|']
for case in ['gate-cwnd-small','gate-rwnd-zero','gate-peers-64','gate-peers-1024']:
 rs=[r for r in attr if r['case']==case and r['stage']=='gate' and r['function']=='new_data_len'];nc=sum(r['calls'] for r in rs)/3;wc=sum(r['from_wants_tx_calls'] for r in rs)/3;wi=sum(r['from_wants_tx_Ir'] for r in rs)/sum(r['from_wants_tx_calls'] for r in rs)
 r=next(r for r in A if r['case']==case);parts.append(f'| {case} | {nc:,.0f} | {wc:,.0f} | {wi:.2f} | {fmt(r["mean_delta_pct"],4)} |')
parts += ['', f'B 的安静对比中，包流/完成结果 46/46 一致，指令极值条件 {sum(r["complete"] and r["increase_le_0_5pct"] for r in quiet)}/46 通过。以下重复范围跨越 0.5%：这是非劣效未获证明，不把噪声样本判成可归因退化，也不删掉它们。故放弃本轮 B 候选晋升；实现只作为默认关闭的复现实验开关保留。','', '| 模式 / 场景 | 三次均值 | 观测范围上沿 |','|---|---:|---:|']
for r in quiet:
 if not r['increase_le_0_5pct']:parts.append(f'| {r["comparison"]} / {r["case"]} | {fmt(r["mean_delta_pct"],4)} | {fmt(r["worst_repeat_increase_pct"],4)} |')
parts += ['', '部分原矩阵的进程随机性没有被虚拟时钟 seed 消除：A 控制/门控重复变化最高约 0.325%，无计数 bare 最高约 0.863%；B 的上述约 0.88% 上沿主要不能与这些重复变化区分。逐次值、极值和变化率均在 instruction-runs/summary 中，不声称所有 Ir 完全确定。','', '## CPU 与尾延迟的价格（重置式出口模型）','', '下表以同二进制 DRR-max 为参考，三位轻用户每人每次各 128 个请求；每行最差值只用于价格坐标，逐用户数据完整分列在 light-users.csv / formal-user-ranges。1 ms 模型为二十对，100 µs 为六对。CPU 区间跨零或均值落入相应 A/A 最大绝对差内写“无可归因变化”。单位价格为点估计的百分点/1 ms；无可归因变化的价格不视作已证实的成本/节省，没有正的 P99 改善时为 N/A。','', '| case / 放行周期 | 模式 | 最差 P99 降低 ms | CPU/GB 均值 [95% CI] | Ir/GB | CPU百分点/降低1ms | CPU判读 |','|---|---|---:|---:|---:|---:|---|']
for r in curve:
 if r['reference']!='DRR-max':continue
 parts.append(f'| {r["case"]} / {r["tick_us"]}µs | {r["mode"]} | {r["worst_p99_reduction_ms"]:.3f} | {fmt(r["cpu_delta_pct"])} [{fmt(r["cpu_ci_low_pct"])}, {fmt(r["cpu_ci_high_pct"])}] | {fmt(r["Ir_delta_pct"])} | {num(r["CPU_pct_per_1ms_point"])} | {dict(increase="可归因上升",decrease="可归因下降").get(r["cpu_attribution"],"无可归因变化")} |')
parts += ['', '敏感性对照仅覆盖持续拥塞的 mixed-16/64，三组小档均为16KiB。它们未表现出额外尾部差异；这组输入并不能充分识别不同W/H退出速度，不能据相同结果断言参数普遍等价。', '', '| case / 模式 (W,K,H) | 最差轻用户P99 ms | 最大核心间隔 ms | CPU/GB [95% CI] | Ir/GB |', '|---|---:|---:|---:|---:|']
for case in ['mixed-16','mixed-64']:
 for mode,par in [('adaptive-fast','1ms,2,4ms'),('adaptive-slow','4ms,2,16ms'),('adaptive-k4','2ms,4,8ms')]:
  us=lights(case,mode,1000);c=C[('cpu-B',case,'sensitivity-'+mode,1000)];i=I[('B-'+mode,case,1000)]
  parts.append(f'| {case} / {par} | {p99(us)/1000:.1f} | {max(u["max_pending_gap_ns"] for u in us)/1e6:.1f} | {fmt(c["cpu_sec_per_GB_delta_pct"])} [{fmt(c["cpu_sec_per_GB_ci_low_pct"])},{fmt(c["cpu_sec_per_GB_ci_high_pct"])}] | {fmt(i["mean_delta_pct"])} |')
parts += ['', '![CPU与尾延迟价格](tradeoff.svg)', '', 'mixed-64、1 ms 中，adaptive16 的 CPU/GB 为 **+3.41% [2.71%,4.11%]**，该 case A/A 最大绝对差 2.93%，可归因上升；adaptive32 为 **+0.43% [-0.39%,1.24%]**，无可归因变化，但 Ir 仍约 +0.74%。不把后者写成零 CPU 成本。相对真正 main+计数的细模型比较另见 [fine-control-reference.svg](fine-control-reference.svg) 和 tradeoff.csv；有 CPU 下降样本，也有更差的 Peer 尾延迟/服务间隔，不能称严格帕累托。','', '## 保留额度的细粒度对照','', 'reset100us 的名义速率只差 0.0092%，但 6,553 B 放行后可遗留不足一个大包的额度；每次清零会损失更多实际容量。这使原 fine 结果成为服务模型敏感性，不能当作纯时钟量化检验。为控制这个混淆，另在测量前冻结 C-token-plan：初始及桶容量均为64KiB，按精确64KiB/ms补充，保留余量和整数余数，100µs唤醒。核心栈源码不改，模型补丁仅在隔离工作树使用；原 reset 数据全部保留。','', '每组重复三次，完整包流均相同。该组开启全包捕获，只用于虚拟延迟/容量诊断，不将其 CPU 当作性能门槛。','', '| case / 模式 | Peer2 P99/间隔 ms | Peer3 P99/间隔 ms | Peer4 P99/间隔 ms |','|---|---:|---:|---:|']
for case in ['mixed-16','mixed-64']:
 for stage,mode,label in policies:
  rs=[r for r in token if r['case']==case and r['stage']==stage and r['mode']==mode and r['user_class']=='light' and r['repeat']=='0'];rs.sort(key=lambda r:int(r['peer']))
  vals=[f'{float(r["p99_us"])/1000:.1f} / {float(r["max_pending_gap_ns"])/1e6:.1f}' for r in rs];parts.append('| '+case+' / '+label+' | '+' | '.join(vals)+' |')
parts += ['', '![保留额度的逐用户P99](token-fine-p99.svg)', '', '保留额度后，16KiB/Adaptive16 的三位 P99 仍为2.1ms，与真正对照相同；mixed-64 的 adaptive32 Peer4 则为2.5ms。16KiB 的 Peer3/4 最大间隔为0.2ms，对照为0.1ms，平均延迟虽降低，尾部机会未全面改善。mixed-16/64 的 bulk 完成在此组各模式分别同为0.2704s/1.0693s；完整单流记录仍保留。时钟本身不是1ms取整，原3→2ms收益依赖出口分组与Full持有visit过程，不能外推成真实WG的固定1ms收益。','', '## 账本、丢包、压力与生产计数的限制','', '每个 case 的 goodput、完成（最后一条 bulk flow 的有序收尾）、账本峰值、丢包和 serve_zero/Full/轮预算比例见 functional-cases.csv、cpu-summary.json；DUT server 账本不含客户端预算和原有未计费容器元数据，不等于 RSS。每个正式样本及每用户分母另见 formal-user-ranges，缺失/失败/删失不以完成子集替代。','']
parts += ['同一重置模型下，以DRR-max为参考，mixed-16/64 的两种adaptive bulk完成时间均未改变；但以真正main+计数为参考，mixed-16由0.272s到0.273s，仍增加约0.368%。bulk-16:1:1则有约0.329%的完成缩短，作为模型/反馈下的具体样本保留，不泛化为免费的吞吐增益。', '', '账本有明确不利值：mixed-64的adaptive32峰值由63,038,624B增到63,544,192B；mixed-16的adaptive16由16,161,280B增到16,329,952B。每iface常数字段不等于实际TX占用峰值不变；既有预算公式未动。', '']
# Concrete adverse samples from the actual CPU pairs.
adverse=[]
for r in cpu:
 if r['phase']=='cpu-B' and r['comparison'] in ['adaptive-16k','adaptive-32k']:
  ds=r['cpu_sec_per_GB_paired_deltas_pct']
  if ds:adverse.append((max(ds),r))
adverse.sort(key=lambda x:x[0],reverse=True)
parts += ['| 不利 CPU 样本 | 最大配对变化 | 均值 [95% CI] | 判读 |','|---|---:|---:|---|']
for peak,r in adverse[:3]:parts.append(f'| {r["case"]} / {r["comparison"]} | {fmt(peak)} | {fmt(r["cpu_sec_per_GB_delta_pct"])} [{fmt(r["cpu_sec_per_GB_ci_low_pct"])}, {fmt(r["cpu_sec_per_GB_ci_high_pct"])}] | {r["cpu_attribution"]} |')
parts += ['', f'D 的整进程计数/报告差分点估计范围为 {fmt(min(r["mean_delta_pct"] for r in D),3)} 至 {fmt(max(r["mean_delta_pct"] for r in D),3)}，最高为 matrix/noisy-cross-shard。它包括计数、一次报告读取和 pending 扫描；负样本不能用来宣称计数器免费或加速。API 可供之后 zfc/zf-worker 接入，但常开代价与 Slot 容量增量均非零，尚无真实节点的可忽略开销证明。','', 'peers-64/1024-open 是 round-only 压力（无 Full），也会触发小档；这只证明就绪工作超过一次 run 的限额，不能认定是链路拥塞。CPU/wall 与 CPU/虚拟完成时间逐 case 保留，但 L0 不真实等待出口释放，无法据此判定 WG 节点 CPU 是否饱和。该类场景增加 CPU 可能影响真实 goodput，须在 L1/L2 看真实运行/等待和出口繁忙时间。','', '## 数据完整性与入口','', '- [instruction-summary.json](instruction-summary.json) / [instruction-comparisons.csv](instruction-comparisons.csv)：全部三次 Ir/GB、波动与门禁。','- [attribution.csv](attribution.csv)：全部39个case、三种源码、三次重复的schedule/serve/wants_tx/new_data_len调用数与指令成本。','- [cpu-summary.json](cpu-summary.json) / [tradeoff.csv](tradeoff.csv)：全部配对差值、95%区间、A/A及价格，不删除极端样本。','- [user-summary.jsonl.gz](user-summary.jsonl.gz) / [formal-user-ranges.jsonl.gz](formal-user-ranges.jsonl.gz)：逐用户点值和所有正式重复范围；多iface别名间隔明确N/A，bulk P99明确N/A。','- [C-token-cases.csv](C-token-cases.csv) / [C-token-users.csv](C-token-users.csv) / [C-model-capacity.csv](C-model-capacity.csv)：保留额度模型与原模型的诊断。','- [validation.json](validation.json)、[binary-identities.json](binary-identities.json)、[reproduction-sources.json](reproduction-sources.json)：完整性和可重建源码身份。','- 各阶段 *-raw.jsonl.gz 保留完整输出、失败、warmup、argv；profiles/ 为全部1182份Callgrind，packets/为全部430次完整有序捕获。','', '全计 **7,604次执行**：831次主Ir、351次函数归因、5,992次辅助CPU（其中正式5,672、warmup320）、430次全包功能捕获。最初十二次无效输入预检失败保留，修正后没有意外失败/超时。原21/21回归、独立A的327项库回归、最终workspace339项库/33项harness/19项bench/6项集成和5项示例通过；另补的第八项迟滞边界回归通过。source replay共九个快照、十六份完整源码补丁均应用并验哈希通过；十六个二进制哈希均已核对。948组正式重复的流结果和链路trace各自一致；包捕获组另外保存完整字节与SHA。','', '<!-- END GENERATED RESULTS -->']
text='\n'.join(parts)+'\n'
p=OUT/'README.md';s=p.read_text();start=s.find('<!-- GENERATED RESULTS -->')
if start>=0:s=s[:start]+s[s.index('<!-- END GENERATED RESULTS -->',start)+len('<!-- END GENERATED RESULTS -->'):]
anchor='## 数据与统计入口' if '## 数据与统计入口' in s else '## 适用范围与复现'
s=s.replace(anchor,text+'\n'+anchor)
s=s.replace('| A 独立门控 | 确定性指令与 20 对辅助 CPU 正在统计 | 按冻结门槛决定；未晋升 |','| A 独立门控 | 32/39门禁通过；7项虽减少空服务，Ir/GB仍上升 | 不作为默认候选；保留独立实验patch |')
s=s.replace('| B 自适应 quantum | 无本地压力的 46 项包流/完成结果对比通过；指令数尚在测量 | 默认关闭；不引入稀疏队列 |','| B 自适应 quantum | 安静输出46/46一致；Ir极值43/46通过，3项非劣效未证明 | 放弃本轮候选晋升；仅保留默认关闭实验开关 |')
s=s.replace('| D 生产计数 | 公共 API 编译/运行回归通过；单独评估开销 | 导出字段与语义见下文；未修改 zfc |','| D 生产计数 | 公共API可读；整进程差分最高+1.667%，Slot增加8B | 可导出限定语义字段；常开不宣称零成本，未修改zfc |')
s=s.replace('十四份','十六份').replace('七项边界/布局回归','七项初始边界/布局回归').replace('接口与时间。所有公平性','接口与时间。所有公平性')
s=s.replace('| C 分辨率 | 核心时钟为 ns；本地 sink 从 1 ms 改为 100 µs 补充可行 | 细模型中 off 与 16 KiB 均为 2.1 ms；32 KiB 的 mixed-16 Peer3/4 为 2.4 ms，不能沿用原 3→2 ms 的完整收益 |','| C 分辨率 | 时钟本身ns；保留额度的100µs模型下，16KiB与真正对照P99同为2.1ms，32KiB可更差 | 不宣称稳定的1ms收益；按出口服务模型解释 |')
p.write_text(s)
(OUT/'gate-decisions.json').write_text(json.dumps({'promotions':[],'A':{'decision':'reject default candidate qualification','instruction_gate_passed':32,'total':39,'reason':'7 nonzero-serve_zero cases increase net instructions'},'B':{'decision':'abandon candidate promotion; default-false experiment code retained for reproducibility','quiet_output_passed':46,'quiet_output_total':46,'quiet_Ir_gate_passed':43,'quiet_Ir_total':46,'reason':'three repeat envelopes cannot prove0.5% noninferiority'},'C':{'decision':'ns clock; latency sensitive to full/credit service model','fine_token16k_P99_vs_control':'same2.1ms','fine_token32k_adverse':'mixed64 Peer4 P99=2.5ms'},'D':{'decision':'public stats API usable with documented nonzero cost and limited v1 noop semantics','max_observed_joint_Ir_increase_pct':max(r['mean_delta_pct'] for r in D),'zfc_modified':False},'scope':'L0 only; production CPU/RSS/throughput need L1/L2'},indent=2)+'\n')
print(json.dumps({'tradeoff_rows':len(curve),'A_pass':32,'B_quiet_Ir_pass':43,'promotions':[]}))
