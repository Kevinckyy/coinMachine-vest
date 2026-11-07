import {useEffect,useRef,useState} from 'react';
import {beijingZone,easternZone,offsetMinutes,offsetZone,readZone,zoneLabel,zonedStamp} from '../timezones';

type Axis='hour'|'weekday'|'week'|'month';
export type TimeBucket={key:string;trades:number;wins:number;active_rounds:number;closes:number;total_pnl:number;mean_round_pnl:number|null;avg_trade_pnl:number|null;pooled_win_rate:number|null;mean_round_win_rate:number|null;avg_holding_seconds:number|null;mean_opens:number|null;mean_closes:number|null;daily_opens:number|null;daily_closes:number|null};
export type TimeStatistics={available:boolean;complete_rounds:number;incomplete_rounds:number;expected_trades:number;recorded_trades:number;start:number|null;end:number|null;basis:string;zones:Record<string,Record<Axis,TimeBucket[]>>};
type Trade={id:number;round_index:number;account_id:number;phase:string;side:string;opened_at:number;closed_at:number;pnl:number};
type TradePage={total:number;expected:number;complete:boolean;rows:Trade[]};
const number=(value:number|null|undefined)=>value==null?'—':value.toLocaleString('en-US',{maximumFractionDigits:2,minimumFractionDigits:2});
const counts=(value:number|null|undefined)=>value==null?'—':value.toLocaleString('en-US',{maximumFractionDigits:4,minimumFractionDigits:2});
const duration=(seconds:number|null|undefined)=>seconds==null?'—':seconds<60?number(seconds)+' 秒':seconds<3600?number(seconds/60)+' 分':number(seconds/3600)+' 小时';
const stamp=zonedStamp;
const label=(axis:Axis,key:string)=>axis==='hour'?key.padStart(2,'0')+':00':axis==='weekday'?['周一','周二','周三','周四','周五','周六','周日'][Number(key)]:axis==='week'?key+' 起':key;
type Series={key:keyof TimeBucket;label:string;color:string};

function BucketChart({rows,axis,series,title,zone,format=number}:{rows:TimeBucket[];axis:Axis;series:Series[];title:string;zone:string;format?:(n:number|null|undefined)=>string}){
  const [hover,setHover]=useState<number|null>(null);
  const container=useRef<HTMLDivElement>(null),[containerWidth,setContainerWidth]=useState(720);
  useEffect(()=>{const element=container.current;if(!element)return;const observer=new ResizeObserver(()=>setContainerWidth(element.clientWidth));observer.observe(element);setContainerWidth(element.clientWidth);return()=>observer.disconnect();},[]);
  useEffect(()=>setHover(null),[rows,series[0]?.key]);
  const width=axis==='hour'||axis==='weekday'?Math.max(240,containerWidth):Math.max(containerWidth,rows.length*28+80),height=238;
  let min=0,max=0;for(const row of rows)for(const s of series){const value=row[s.key];if(typeof value==='number'){min=Math.min(min,value);max=Math.max(max,value);}}
  if(series.every(s=>String(s.key).includes('win_rate')))max=100;
  else if(min===max)max=min+1;
  const range=max-min||1,y=(v:number)=>18+(max-v)/range*170,step=(width-85)/Math.max(1,rows.length),x=(i:number)=>65+step*i;
  const selected=hover==null?null:rows[hover];
  const labelStep=Math.max(1,Math.ceil(rows.length/Math.max(2,Math.floor((width-85)/(axis==='hour'?32:70)))));
  return <div className="time-chart"><h4>{title}</h4><p className="time-chart-zone">时区：{zoneLabel(zone,true)}</p><div className="time-chart-legend">{series.map(s=><span key={s.key} style={{color:s.color}}>{s.label}</span>)}</div><div className="time-chart-scroll" ref={container}><svg role="img" aria-label={title} viewBox={`0 0 ${width} ${height}`} style={{width,height}} onPointerLeave={()=>setHover(null)} onPointerMove={e=>{const rect=e.currentTarget.getBoundingClientRect();setHover(Math.min(rows.length-1,Math.max(0,Math.floor(((e.clientX-rect.left)/rect.width*width-65)/step))));}}>
    {[min,(min+max)/2,max].map((v,i)=><g key={i}><line x1="60" x2={width-15} y1={y(v)} y2={y(v)} stroke="#333945" strokeDasharray="3 4"/><text x="54" y={y(v)+3} textAnchor="end" fill="#8492a4" fontSize="9">{format(v)}</text></g>)}
    {rows.map((row,i)=><g key={row.key}>{series.map((s,j)=>{const value=row[s.key];return typeof value==='number'?<rect key={s.key} data-time-bar={s.key} x={x(i)+j*step*.75/series.length} y={Math.min(y(0),y(value))} width={Math.max(1,step*.65/series.length)} height={Math.max(.8,Math.abs(y(value)-y(0)))} fill={s.color} opacity={hover==null||hover===i?0.9:0.4}><title>{label(axis,row.key)} · {s.label} {format(value)} · {row.trades} 笔</title></rect>:null;})}{i%labelStep===0&&<text x={x(i)} y="211" fill="#8c99aa" fontSize="9">{axis==='week'?row.key:label(axis,row.key)}</text>}</g>)}
  </svg></div><p>{selected?label(axis,selected.key)+' · '+series.map(s=>s.label+' '+format(selected[s.key] as number|null)).join(' / '):'悬停查看数值；较长的时间序列可横向滚动。'}</p></div>;
}

function TradeTimes({id,rounds,zone}:{id:string;rounds:number;zone:string}){
  const [open,setOpen]=useState(false),[offset,setOffset]=useState(0),[page,setPage]=useState<TradePage|null>(null),[loading,setLoading]=useState(false),[error,setError]=useState('');
  useEffect(()=>{
    if(!open)return;const controller=new AbortController();setLoading(true);setError('');
    void fetch('/api/backtests/batches/'+id+'/trade-times?offset='+offset+'&limit=20',{signal:controller.signal}).then(async r=>{const data=await r.json();if(!r.ok)throw new Error(data.error||'开平仓记录读取失败');return data as TradePage;}).then(data=>{if(!controller.signal.aborted)setPage(data);}).catch(e=>{if(!controller.signal.aborted)setError(e.message);}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[id,rounds,offset,open]);
  return <details className="time-trade-records" onToggle={e=>setOpen(e.currentTarget.open)}><summary>开平仓时间记录 · 已完成轮次 · {zoneLabel(zone,true)}</summary>{open&&<><p className="backtest-note">显示时区：{zoneLabel(zone)}。按开仓时间倒序，原始时间以 UTC 时间戳保存；K线内触发通常记在该根结束时，无法还原真实逐笔秒级成交。</p>{error&&<p role="alert" className="backtest-note negative">{error}</p>}{page&&!page.complete&&<p className="backtest-note">已存 {page.total} / {page.expected} 条历史交易记录；缺失的开平仓时间不补造。</p>}
    <div className="table-scroll time-trade-table" aria-busy={loading}><table><thead><tr><th>轮次 / 账号</th><th>阶段 / 方向</th><th>开仓时间（{zoneLabel(zone,true)}）</th><th>平仓时间（{zoneLabel(zone,true)}）</th><th>持仓时长</th><th>净盈亏 USDC</th></tr></thead><tbody>{!loading&&page?.rows.map(t=><tr key={t.round_index+':'+t.id}><td>{t.round_index+1} / {t.account_id}</td><td>{t.phase} / {t.side==='buy'?'多':'空'}</td><td>{stamp(t.opened_at,zone)}</td><td>{stamp(t.closed_at,zone)}</td><td>{duration(t.closed_at-t.opened_at)}</td><td className={t.pnl>=0?'positive':'negative'}>{number(t.pnl)}</td></tr>)}</tbody></table></div>
    {loading&&<p className="backtest-note">正在读取开平仓记录…</p>}{!loading&&page&&!page.rows.length&&<p className="backtest-note">暂无已保存的交易记录。</p>}
    <div className="history-pagination"><span>{page?.total||0} 条 · 每页 20 条</span><button className="button ghost small" disabled={loading||offset===0} onClick={()=>setOffset(Math.max(0,offset-20))}>上一页</button><button className="button ghost small" disabled={loading||!page||offset+20>=page.total} onClick={()=>setOffset(offset+20)}>下一页</button></div></>}</details>;
}

export default function BacktestTimeStatistics({id,rounds,statistics}:{id:string;rounds:number;statistics?:TimeStatistics}){
  const [zone,setZone]=useState(readZone);
  const [offset,setOffset]=useState(()=>String((offsetMinutes(readZone())??0)/60));
  const [zoneData,setZoneData]=useState<Record<string,{rounds:number;data:TimeStatistics}>>({}),[loading,setLoading]=useState(false),[error,setError]=useState(''),[retry,setRetry]=useState(0);
  const commitZone=(next:string)=>{setZone(next);try{sessionStorage.setItem('backtest-time-zone',next);localStorage.setItem('backtest-time-zone',next);}catch{}};
  const offsetValue=Number(offset),offsetValid=offset.trim()!==''&&Number.isFinite(offsetValue)&&offsetValue>=-12&&offsetValue<=14&&Math.abs(offsetValue*4-Math.round(offsetValue*4))<1e-8;
  const selectedStatistics=statistics?.zones[zone]?statistics:zoneData[zone]?.rounds===rounds?zoneData[zone].data:undefined;
  const cachedReady=zoneData[zone]?.rounds===rounds;
  useEffect(()=>{
    setError('');if(zone===easternZone||zone===beijingZone||cachedReady){setLoading(false);return;}
    const controller=new AbortController();setLoading(true);
    void fetch('/api/backtests/batches/'+id+'/time-statistics?'+new URLSearchParams({zone}),{signal:controller.signal}).then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.error||'所选时区统计读取失败');return data as TimeStatistics;}).then(data=>{if(!controller.signal.aborted)setZoneData(old=>({...old,[zone]:{rounds,data}}));}).catch(e=>{if(!controller.signal.aborted)setError(e.message);}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[id,rounds,zone,cachedReady,retry]);
  const [axis,setAxis]=useState<Axis>('hour'),[countBasis,setCountBasis]=useState<'day'|'round'>('day');
  const [outcome,setOutcome]=useState<'mean_round_win_rate'|'pooled_win_rate'|'mean_round_pnl'|'avg_trade_pnl'>('mean_round_win_rate');
  const hours=selectedStatistics?.zones[zone]?.hour||[],rows=selectedStatistics?.zones[zone]?.[axis]||[];
  const labels={mean_round_win_rate:'轮均胜率 %',pooled_win_rate:'合并胜率 %',mean_round_pnl:'轮均净盈亏 USDC',avg_trade_pnl:'单笔平均净盈亏 USDC'};
  return <section className="backtest-time-statistics"><div className="time-stat-heading"><h3>交易时间分布</h3><label>统计时区 <select aria-label="时间统计时区" value={zone===easternZone||zone===beijingZone?zone:'fixed'} onChange={e=>commitZone(e.target.value==='fixed'?offsetZone(offsetValid?offsetValue:0):e.target.value)}><option value="America/New_York">美东 ET · UTC−5 / UTC−4（自动夏令时）</option><option value="Asia/Shanghai">北京时间 · UTC+08:00</option><option value="fixed">自定义 UTC±N · 固定偏移</option></select></label></div>
    {zone!==easternZone&&zone!==beijingZone&&<div className="time-offset-controls"><label>UTC 偏移小时 N <input aria-label="UTC时区偏移小时" type="number" min="-12" max="14" step="0.25" value={offset} onChange={e=>setOffset(e.target.value)}/></label><button type="button" className="button ghost small" disabled={!offsetValid} onClick={()=>commitZone(offsetZone(offsetValue))}>应用时区</button><small>−12 至 +14，支持 0.25 小时（15 分钟）；例如 5.5 = UTC+05:30。</small></div>}
    <p className="time-zone-note" aria-label="当前统计时区">当前统计时区：{zoneLabel(zone)}。本节图表、表格及开平仓时间均按此时区显示；日切规则仍按参数中的美东时间执行。</p>
    {loading&&<p className="backtest-note" role="status">正在按 {zoneLabel(zone,true)} 归集统计，首次读取将建立本地缓存…</p>}
    {error&&<p className="backtest-note negative" role="alert">{error} <button className="text-button" onClick={()=>setRetry(v=>v+1)}>重试</button></p>}
    <p className="backtest-note">持仓时长、胜率与盈亏均按开仓时点分组；跨天交易整体归开仓时段，周按周一开始的自然周，月按自然月。平仓次数单独按平仓时点分组。净盈亏包含双边手续费、滑点及资金费，包含考试阶段虚拟盈亏。</p>
    {!selectedStatistics?.available?<p className="backtest-note">{loading?'请等待所选时区统计加载完成。':error?'可重试读取所选时区统计。':selectedStatistics?.incomplete_rounds?'历史交易时间记录不完整，无法还原时间分布。':'完成至少一轮后显示时间分布。'}</p>:<>
      <p className="backtest-note">覆盖 {selectedStatistics.complete_rounds} 轮，记录 {selectedStatistics.recorded_trades.toLocaleString()} / {selectedStatistics.expected_trades.toLocaleString()} 笔交易。{selectedStatistics.incomplete_rounds>0?`${selectedStatistics.incomplete_rounds} 轮明细不完整，以下仅统计完整轮次，不用预览记录推算。`:''}</p>
      <div className="time-stat-tools"><h4>24 小时交易节奏</h4><select aria-label="开平仓次数平均口径" value={countBasis} onChange={e=>setCountBasis(e.target.value as 'day'|'round')}><option value="day">日均次数</option><option value="round">轮均次数</option></select></div>
      <div className="time-hour-charts"><BucketChart rows={hours} axis="hour" zone={zone} title="24h 平均开平仓次数" format={counts} series={[{key:countBasis==='day'?'daily_opens':'mean_opens',label:'平均开仓次数',color:'#e7b74a'},{key:countBasis==='day'?'daily_closes':'mean_closes',label:'平均平仓次数',color:'#66adf1'}]}/><BucketChart rows={hours} axis="hour" zone={zone} title="24h 平均持仓时间（按开仓时间）" format={duration} series={[{key:'avg_holding_seconds',label:'平均持仓时长',color:'#30c79b'}]}/></div>
      <p className="backtest-note">轮均次数＝该小时总次数 / 完整轮数；日均次数先按每轮回测区间时长折算为每 24 小时次数，再对各轮等权平均，包含休市及行情缺口的时间。持仓时间按交易笔数加权；小时为 00:00–00:59 等自然小时，夏令时重复小时合并。</p>
      <div className="table-scroll time-bucket-table time-hour-table"><table><thead><tr><th>小时（{zoneLabel(zone,true)}）</th><th>轮均开仓</th><th>轮均平仓</th><th>日均开仓</th><th>日均平仓</th><th>平均持仓时间</th><th>开仓样本单数</th></tr></thead><tbody>{hours.map(r=><tr key={r.key}><td>{label('hour',r.key)}</td><td>{counts(r.mean_opens)}</td><td>{counts(r.mean_closes)}</td><td>{counts(r.daily_opens)}</td><td>{counts(r.daily_closes)}</td><td>{duration(r.avg_holding_seconds)}</td><td>{r.trades.toLocaleString()}</td></tr>)}</tbody></table></div>
      <div className="time-stat-tools"><h4>胜率与盈亏分布</h4><select aria-label="收益时间分组" value={axis} onChange={e=>setAxis(e.target.value as Axis)}><option value="hour">24 小时</option><option value="weekday">每周七天</option><option value="week">每周</option><option value="month">每月</option></select><select aria-label="收益时间统计指标" value={outcome} onChange={e=>setOutcome(e.target.value as typeof outcome)}>{Object.entries(labels).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></div>
      <BucketChart rows={rows} axis={axis} zone={zone} title="胜率与盈亏时间分布" series={[{key:outcome,label:labels[outcome],color:outcome.includes('win_rate')?'#66adf1':'#e7b74a'}]}/>
      <div className="table-scroll time-bucket-table time-outcome-table"><table><thead><tr><th>开仓时段（{zoneLabel(zone,true)}）</th><th>样本单数</th><th>有交易轮数</th><th>轮均胜率</th><th>合并胜率</th><th>轮均净盈亏</th><th>单笔平均净盈亏</th><th>累计净盈亏</th></tr></thead><tbody>{rows.map(r=><tr key={r.key}><td>{label(axis,r.key)}</td><td>{r.trades.toLocaleString()}</td><td>{r.active_rounds}</td><td>{r.mean_round_win_rate==null?'—':number(r.mean_round_win_rate)+'%'}</td><td>{r.pooled_win_rate==null?'—':number(r.pooled_win_rate)+'%'}</td><td>{number(r.mean_round_pnl)}</td><td>{number(r.avg_trade_pnl)}</td><td>{number(r.total_pnl)}</td></tr>)}</tbody></table></div>
      <p className="backtest-note">轮均胜率：仅对该时段有交易的轮次等权平均；合并胜率：总盈利单数 / 总单数（零盈亏不算盈利）。轮均净盈亏按全部完整轮次平均，无交易时段计 0；没有成交的胜率和平均持仓时间显示「—」。</p>
    </>}
    <TradeTimes id={id} rounds={rounds} zone={zone}/>
  </section>;
}
