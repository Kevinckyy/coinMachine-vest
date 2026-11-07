import {useEffect,useState} from 'react';
type Aggregate={count:number;mean:number;min:number;max:number;stddev:number};
type Point={time:number;value:number};
type Seed=number|string;
type CurveSet={available:boolean;reason:string;rounds:number;best:{seed:Seed;final_cash:number;points:Point[]}|null;worst:{seed:Seed;final_cash:number;points:Point[]}|null;average:Point[]};
export type Batch={id:string;name?:string;custom_name?:string;strategy_name?:string;created_at?:number;status:string;config:{rounds:number;seed:Seed;seed_policy?:string;rng_algorithm?:string;parallel_workers:number;symbol?:string;pool_limits_enabled?:boolean;exam_pool_limit?:number|'infinity';live_pool_limit?:number|'infinity'};
  summary:{completed_rounds:number;requested_rounds:number;parallel_workers:number;duration_seconds:number|null;mean_round_seconds:number|null;metrics:Record<string,Aggregate>;
    expectancy?:{basis:string;complete_rounds:number;incomplete_rounds:number};
    trading:{total_accounts:number;total_trades:number;avg_trades_per_account:number|null;avg_holding_seconds:number|null;stages:{phase:string;accounts:number;trades:number;avg_trades:number|null;avg_holding_seconds:number|null}[]};
    settlement?:{settled_rounds:number;legacy_rounds:number;trading_end:number|null;settlement_end:number|null};dataset?:{symbol:string;source?:string;bars:number;first_time:number;last_time:number};cash_curves:{with_fees:CurveSet;without_fees:CurveSet}}};
export const wallDuration=(seconds:number|null|undefined)=>{
  if(seconds==null||!Number.isFinite(seconds))return '—';
  if(seconds<60)return `${seconds.toFixed(seconds<1?2:1)} 秒`;
  const whole=Math.ceil(seconds),minutes=Math.ceil(seconds/60),hours=Math.ceil(seconds/3600);
  if(seconds<3600)return `${Math.floor(whole/60)} 分 ${whole%60} 秒`;
  if(minutes<1440)return `${Math.floor(minutes/60)} 小时 ${minutes%60} 分`;
  return `${Math.floor(hours/24)} 天 ${hours%24} 小时`;
};
const n=(v:number|null|undefined)=>v==null?'—':v.toLocaleString('en-US',{maximumFractionDigits:2,minimumFractionDigits:2});
const count=(v:number|undefined)=>v==null?'—':v.toLocaleString('en-US',{maximumFractionDigits:0});
const seedLabel=(seed:Seed)=>typeof seed==='string'&&seed.length>20?seed.slice(0,10)+'…'+seed.slice(-8):String(seed);
const stamp=(t:number)=>new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const fields:[string,string,boolean?][]=[['win_rate','胜率',true],['total_pnl','总净盈亏'],['exam_pass_rate','考试通过率',true],['payout_rate','出金率',true],['total_payout','总出金'],['avg_payout_per_account','单号平均出金'],['net_cash','出金减购号成本'],['total_fees','账户内交易手续费'],['settled_after_cutoff','截止后到账分润']];

function CashChart({curves}:{curves:CurveSet}){
  const [hover,setHover]=useState<number|null>(null);
  useEffect(()=>setHover(null),[curves]);
  if(!curves?.available||!curves.best||!curves.worst)return <p className="backtest-note">{curves?.reason||'完成测试后显示资金曲线'}</p>;
  const lines=[{id:'best',label:'最好',color:'#30c79b',seed:curves.best.seed,points:curves.best.points},{id:'worst',label:'最差',color:'#ef7185',seed:curves.worst.seed,points:curves.worst.points},{id:'average',label:'平均',color:'#e7b74a',seed:null,points:curves.average}];
  let min=0,max=0;for(const line of lines)for(const p of line.points){min=Math.min(min,p.value);max=Math.max(max,p.value);}const span=Math.max(1,max-min);
  const first=curves.average[0].time,last=curves.average.at(-1)!.time;
  const hoverPoint=hover!=null?curves.average[hover]:undefined;
  const x=(t:number)=>74+(t-first)/Math.max(1,last-first)*800,y=(v:number)=>20+(max-v)/span*220;
  const path=(points:Point[])=>points.map((p,i)=>(i?`${x(p.time)},${y(points[i-1].value)} `:'')+`${x(p.time)},${y(p.value)}`).join(' ');
  return <div className="backtest-cash-curve"><div className="backtest-cash-legend">{lines.map(l=><span key={l.id} style={{color:l.color}} title={l.seed!=null?'完整种子：'+l.seed:undefined}>{l.label}{l.seed!=null?' · Seed '+seedLabel(l.seed):''}<strong>{n(l.points.at(-1)?.value)} USDC</strong></span>)}</div>
    <svg viewBox="0 0 900 285" role="img" aria-label="最好最差平均资金曲线" onPointerLeave={()=>setHover(null)} onPointerMove={e=>{const box=e.currentTarget.getBoundingClientRect(),t=first+((e.clientX-box.left)/box.width*900-74)/800*(last-first);let best=0;curves.average.forEach((p,i)=>{if(Math.abs(p.time-t)<Math.abs(curves.average[best].time-t))best=i;});setHover(best);}}>
      {[min,(min+max)/2,max].map((v,i)=><g key={i}><line x1="74" x2="874" y1={y(v)} y2={y(v)} stroke="#303641" strokeDasharray="4 5"/><text x="65" y={y(v)+4} textAnchor="end" fill="#8c99aa" fontSize="10">{v.toLocaleString('en-US',{notation:'compact',maximumFractionDigits:1})}</text></g>)}
      {lines.map(l=><polyline key={l.id} data-cash-line={l.id} points={path(l.points)} fill="none" stroke={l.color} strokeWidth={l.id==='average'?2.8:1.7}/>) }
      {[first,(first+last)/2,last].map((t,i)=><text key={i} x={x(t)} y="267" textAnchor={i===0?'start':i===2?'end':'middle'} fill="#8c99aa" fontSize="10">{stamp(t)}</text>)}
      {hoverPoint&&<line x1={x(hoverPoint.time)} x2={x(hoverPoint.time)} y1="20" y2="240" stroke="#9da7b7" strokeDasharray="3 4"/>}
    </svg><p className="backtest-note">{hoverPoint?stamp(hoverPoint.time)+' · '+lines.map(l=>l.label+' '+n(l.points[hover!]?.value)).join(' / '):'将鼠标移到曲线上查看数值。时间为 UTC+8，金额为 USDC。'}</p></div>;
}

export default function BacktestBatchResults({batch,batches,onBatch,showChooser=true}:{batch:Batch|null;batches:{id:string;name?:string;config:{rounds:number;seed:Seed;seed_policy?:string}}[];onBatch:(id:string)=>void;showChooser?:boolean}){
  const [feeMode,setFeeMode]=useState<'with_fees'|'without_fees'>('with_fees');
  useEffect(()=>setFeeMode('with_fees'),[batch?.id]);
  if(!batch&&!batches.length)return null;
  const trading=batch?.summary.trading,curves=batch?.summary.cash_curves;
  return <section className="panel backtest-batch"><div className="panel-heading"><h2>多轮统计</h2>{showChooser&&<select aria-label="已保存的多轮测试" value={batch?.id||''} onChange={e=>onBatch(e.target.value)}><option value="" disabled>选择测试记录</option>{batches.map(b=><option key={b.id} value={b.id}>{b.name||`${b.config.seed_policy==='random_128'?'随机批次':'历史 Seed'} ${seedLabel(b.config.seed)}`} · {b.config.rounds} 轮</option>)}</select>}</div>{batch&&<>
    {batch.name&&showChooser&&<p className="backtest-note">{batch.name} · <a href={'#backtest-history?record='+batch.id}>在历史管理页查看</a></p>}
    <p className="backtest-note">已完成 {batch.summary.completed_rounds} / {batch.config.rounds} 轮 · {batch.summary.parallel_workers} 个并行任务 · 总耗时 {wallDuration(batch.summary.duration_seconds)} · 单轮实测平均 {wallDuration(batch.summary.mean_round_seconds)} · {({running:'运行中',completed:'已完成',cancelled:'已停止',interrupted:'已中断',error:'出错'} as Record<string,string>)[batch.status]||batch.status}</p>
    <p className="backtest-note">{batch.config.rng_algorithm==='pcg64dxsm_v1'?'PCG64DXSM · 每轮独立随机选择128位种子，实际种子已保存':'历史记录 · MT19937，沿用原种子规则'}</p>
    {batch.summary.dataset&&<p className="backtest-note">{batch.summary.dataset.symbol} · {count(batch.summary.dataset.bars)} 根 · {new Date(batch.summary.dataset.first_time*1000).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'})} — {new Date(batch.summary.dataset.last_time*1000).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'})} · {batch.summary.dataset.source}</p>}
    {batch.summary.settlement?.trading_end&&<p className="backtest-note">交易截止 {stamp(batch.summary.settlement.trading_end)} · 分润结算至 {stamp(batch.summary.settlement.settlement_end||batch.summary.settlement.trading_end)}。截止后停止交易，仅结算已有分润申请；最终出金率、总出金和资金曲线包含该结算尾段。</p>}
    {!!batch.summary.settlement?.legacy_rounds&&<p className="backtest-note">{batch.summary.settlement.legacy_rounds} 轮历史记录缺少完整分润明细，仍沿用截止时口径；重新回测后可完整结算。</p>}
    <div className="backtest-batch-averages">{fields.map(([key,label,percent])=>{const metric=batch.summary.metrics[key];if(key==='avg_payout_per_account'){
      const paid=batch.summary.metrics.avg_credited_payout_per_account,pending=batch.summary.metrics.avg_pending_payout_per_account,remaining=batch.summary.metrics.avg_remaining_expectancy_per_account,total=batch.summary.metrics.avg_total_expectancy_per_account;
      const complete=batch.summary.expectancy?.incomplete_rounds===0;
      return <div key={key} className="backtest-expectancy-card"><span>各轮平均单号期望</span><strong className="backtest-expectancy-value" aria-label="统计单号平均期望">{n(paid?.mean)} + {n(pending?.mean)} (+{complete?n(remaining?.mean):'—'})</strong><small>已到账＋已申请待到账（＋剩余期望）</small><small>实际出金 {n(metric?.mean)} · 含估算合计 {complete?n(total?.mean):'—'}</small><small>{complete?`总期望范围 ${n(total?.min)} — ${n(total?.max)} · 标准差 ${n(total?.stddev)}`:'历史期末账号快照不完整，剩余期望需重新回测'}</small></div>;
    }return <div key={key}><span>各轮平均{label}</span><strong>{n(metric?.mean)}{metric&&percent?'%':''}</strong><small>范围 {n(metric?.min)} — {n(metric?.max)} · 标准差 {n(metric?.stddev)}</small></div>;})}</div>
    <p className="backtest-note">期望拆分采用交易截止时点：已到账＋已申请待到账（＋存活账号剩余期望），各项按每轮统计账户数折算后取平均。待到账分润会按原延迟结清；剩余期望按上次分析的零成本公平模型估算，不计入实际出金、净现金或资金曲线。</p>
    {batch.config.pool_limits_enabled&&<p className="backtest-note" aria-label="账号池汇总">账号池上限：考试 {batch.config.exam_pool_limit==='infinity'?'∞':batch.config.exam_pool_limit} · Live {batch.config.live_pool_limit==='infinity'?'∞':batch.config.live_pool_limit}；各轮平均存活峰值：考试 {n(batch.summary.metrics.exam_pool_peak?.mean)} · Live {n(batch.summary.metrics.live_pool_peak?.mean)}；截止时平均等待 Live 名额 {n(batch.summary.metrics.waiting_live_accounts?.mean)} 个。Live 优先选批和下单。</p>}
    <div className="backtest-pooled-stats"><h3>交易统计 · 汇总全部已完成轮次</h3><div className="backtest-time-estimate"><div><span>每号平均单数</span><strong aria-label="统计每号平均单数">{n(trading?.avg_trades_per_account)}</strong></div><div><span>平均持仓时间</span><strong aria-label="统计平均持仓时间">{wallDuration(trading?.avg_holding_seconds)}</strong></div></div><p className="backtest-note">累计 {count(trading?.total_accounts)} 个统计账户，{count(trading?.total_trades)} 笔交易。每号平均单数=总单数/统计账户数；平均持仓时间按已平仓笔数加权。</p></div>
    <div className="table-scroll backtest-stage-statistics"><table><thead><tr><th>阶段</th><th>累计账户数</th><th>累计单数</th><th>各阶段平均单数</th><th>平均持仓时间</th></tr></thead><tbody>{trading?.stages.map(stage=><tr key={stage.phase}><td>{stage.phase}</td><td>{count(stage.accounts)}</td><td>{count(stage.trades)}</td><td>{n(stage.avg_trades)}</td><td>{wallDuration(stage.avg_holding_seconds)}</td></tr>)}</tbody></table></div>
    <p className="backtest-note">各阶段平均单数=该阶段总单数/进入该阶段的累计账户数。固定池初始阶段包含未参与账户；∞ 按实际生成账户统计。</p>
    <div className="backtest-cash-heading"><h3>资金曲线 · 总出金 − 总成本</h3><div className="mode-switch"><button aria-label="资金曲线含手续费" aria-pressed={feeMode==='with_fees'} className={feeMode==='with_fees'?'active':''} onClick={()=>setFeeMode('with_fees')}>含交易手续费</button><button aria-label="资金曲线不含手续费" aria-pressed={feeMode==='without_fees'} disabled={!curves?.without_fees?.available} className={feeMode==='without_fees'?'active':''} onClick={()=>setFeeMode('without_fees')}>不含交易手续费</button></div></div>
    {curves&&<CashChart curves={curves[feeMode]}/>}
    <p className="backtest-note">总出金包含截止后按原延迟全部到账的已申请分润，截止后到账分润已计入总出金。固定池从起点计入全部计划购号成本，∞ 按实际补号时点计入成本。交易手续费只从考核 / Live 账户余额扣除，出金按扣费后的 Live 利润分润，现金曲线不重复扣费。不含手续费按相同种子重算，仅交易手续费置零；滑点、资金费和购号成本保留。切换仅改变资金曲线，上方统计沿用配置的手续费。</p>
    <p className="backtest-note">最好/最差按对应口径最终净额选取整轮曲线，平均线按相同时间点求均值，较早结算完的轮次保持最终金额。历史测试缺少免手续费结算数据时需重新回测。汇总指标按各轮等权取平均，交易统计按账户/交易总数加权。</p>
  </>}</section>;
}
