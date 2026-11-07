import {useEffect,useRef,useState,type FormEvent} from 'react';
import {RefreshCw,Search} from 'lucide-react';
import BacktestBatchResults,{type Batch} from './BacktestBatchResults';

type Filters={root:string;strategy_id:string;time_basis:string;from:string;to:string;pattern:string};
type RecordRow={id:string;name:string;custom_name:string;strategy_name:string;strategy_id:string;root:string;symbol:string;created_at:number;from:number;to:number;rounds:number;status:string};
type Records={rows:RecordRow[];total:number;offset:number;limit:number};
type Detail=Batch&{config:Batch['config']&Record<string,unknown>};
const defaults:Filters={root:'',strategy_id:'',time_basis:'created',from:'',to:'',pattern:''};
const statusNames:Record<string,string>={running:'运行中',completed:'已完成',cancelled:'已停止',interrupted:'已中断',error:'出错'};
const stamp=(value:number)=>Number.isFinite(value)?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
const selectedFromUrl=()=>new URLSearchParams(window.location.hash.split('?')[1]||'').get('record')||'';
const initialFilters=()=>{try{return {...defaults,...JSON.parse(sessionStorage.getItem('backtest-history-filters')||'{}')};}catch{return defaults;}};
const groups:[string,[string,string][]][]=[
  ['行情与测试规模',[['root','品种'],['symbol','行情合约'],['interval','K线周期（秒）'],['from','开始时间'],['to','结束时间'],['account_count','模拟账户总数'],['initial_phase','起始阶段'],['rounds','随机种子轮数'],['parallel_workers','并行任务数']]],
  ['策略与账号池',[['batch_size','每批账户数'],['side','方向'],['size_percent','仓位比例 %'],['leverage','杠杆'],['sizing','数量模式'],['stop_loss','每笔止损 USDC'],['skip_stop_loss_on_low_drawdown','低回撤时不设止损'],['drawdown_stop_loss_threshold','低回撤阈值 USDC'],['repeat_daily','每日重复'],['strategy_reset_hour','开仓额度日切（美东时）'],['order_delay_min','下单最小间隔秒'],['order_delay_max','下单最大间隔秒'],['batch_delay_min','批次最小间隔秒'],['batch_delay_max','批次最大间隔秒'],['pool_limits_enabled','账号池限制'],['exam_pool_limit','考试号上限 m'],['live_pool_limit','Live号上限 n']]],
  ['账户方案与出金',[['initial_capital','初始资金 USDC'],['account_fee','单号购买成本'],['phase1_goal','第一阶段目标'],['phase2_goal','第二阶段目标'],['max_drawdown','固定最大回撤'],['daily_loss_percent','日亏损比例 %'],['risk_reset_hour','风控日切（美东时）'],['live_profit_target','Live出金平仓目标'],['payout_min_profit','最低可申请利润'],['profit_split_percent','分润比例 %'],['payout_delay_hours','分润到账延迟小时']]],
  ['费用与随机设置',[['fee_bps','单边交易手续费 bps'],['slippage_mode','滑点单位'],['slippage_ticks','每边滑点 tick'],['slippage_usd','整笔订单每边滑点 USD'],['slippage_bps','历史每边滑点 bps'],['funding_bps_hourly','每小时资金费 bps'],['cost_source','费用依据'],['seed','首轮随机种子'],['rng_algorithm','随机数算法'],['seed_policy','种子选择规则']]],
];
const valueText=(key:string,value:unknown)=>{
  if(value==null)return '—';if(typeof value==='boolean')return value?'开启':'关闭';
  if((key==='strategy_reset_hour'||key==='risk_reset_hour')&&typeof value==='number')return String(value).padStart(2,'0')+':00（美东）';
  if((key==='from'||key==='to')&&typeof value==='number')return stamp(value)+'（UTC+8）';
  const labels:Record<string,string>={infinity:'∞',buy:'仅做多',sell:'仅做空',random:'随机做多 / 做空',ticks:'tick',usd:'USD（整笔每边）',bps:'bps',micro_contracts:'整数微型合约等值数量',vest_equivalent:'允许小数的 Vest 等值数量',pcg64dxsm_v1:'PCG64DXSM',mt19937_v1:'MT19937',random_128:'每轮独立随机 128 位',fixed:'固定种子'};
  return labels[String(value)]||String(value);
};
async function api<T>(path:string,signal?:AbortSignal,body?:unknown):Promise<T>{
  const r=await fetch(path,{signal,method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await r.json();if(!r.ok)throw new Error(data.error||'回测历史读取失败');return data;
}

export default function BacktestHistory(){
  const [draft,setDraft]=useState<Filters>(initialFilters),[filters,setFilters]=useState<Filters>(initialFilters);
  const [records,setRecords]=useState<Records>({rows:[],total:0,offset:0,limit:20}),[offset,setOffset]=useState(0),[revision,setRevision]=useState(0);
  const [selected,setSelected]=useState(selectedFromUrl),[detail,setDetail]=useState<Detail|null>(null),[strategies,setStrategies]=useState<{id:string;name:string}[]>([]);
  const [loading,setLoading]=useState(false),[detailLoading,setDetailLoading]=useState(false),[error,setError]=useState(''),[detailError,setDetailError]=useState('');
  const [detailRevision,setDetailRevision]=useState(0);
  const [name,setName]=useState(''),[saving,setSaving]=useState(false),[renameStatus,setRenameStatus]=useState('');
  const selectedRef=useRef(selected);selectedRef.current=selected;
  useEffect(()=>{const controller=new AbortController();void api<{strategies:{id:string;name:string}[]}>('/api/strategies',controller.signal).then(r=>setStrategies(r.strategies)).catch(()=>{});return()=>controller.abort();},[]);
  useEffect(()=>{const changed=()=>setSelected(selectedFromUrl());window.addEventListener('hashchange',changed);return()=>window.removeEventListener('hashchange',changed);},[]);
  useEffect(()=>{
    const controller=new AbortController();setLoading(true);setError('');
    const query=new URLSearchParams({root:filters.root,strategy_id:filters.strategy_id,time_basis:filters.time_basis,pattern:filters.pattern,offset:String(offset),limit:'20'});
    for(const key of ['from','to']as const)if(filters[key])query.set(key,String(Date.parse(filters[key]+':00+08:00')/1000));
    void api<Records>('/api/backtests/history?'+query,controller.signal).then(r=>{if(!controller.signal.aborted)setRecords(r);}).catch(e=>{if(!controller.signal.aborted){setRecords({rows:[],total:0,offset:0,limit:20});setError(e.message);}}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[filters,offset,revision]);
  useEffect(()=>{
    setDetail(null);setDetailError('');setRenameStatus('');setDetailLoading(false);if(!selected)return;
    const controller=new AbortController();setDetailLoading(true);
    void api<Detail>('/api/backtests/batches/'+encodeURIComponent(selected)+'?summary_only=1',controller.signal).then(r=>{if(!controller.signal.aborted){setDetail(r);setName(r.custom_name||'');}}).catch(e=>{if(!controller.signal.aborted)setDetailError(e.message);}).finally(()=>{if(!controller.signal.aborted)setDetailLoading(false);});
    return()=>controller.abort();
  },[selected,detailRevision]);
  const select=(id:string)=>{setSelected(id);window.history.replaceState(null,'','#backtest-history'+(id?'?record='+encodeURIComponent(id):''));};
  const apply=(event?:FormEvent)=>{event?.preventDefault();setFilters({...draft});setOffset(0);sessionStorage.setItem('backtest-history-filters',JSON.stringify(draft));select('');};
  const rename=async(event:FormEvent)=>{
    event.preventDefault();if(!detail)return;const id=detail.id;setSaving(true);setDetailError('');setRenameStatus('');
    try{const r=await api<{name:string;custom_name:string}>('/api/backtests/history/'+id+'/name',undefined,{custom_name:name});setDetail(old=>old?.id===id?{...old,...r}:old);setRevision(v=>v+1);if(selectedRef.current===id){setName(r.custom_name);setRenameStatus('名称已保存');}}
    catch(e){if(selectedRef.current===id)setDetailError((e as Error).message);}finally{setSaving(false);}
  };
  return <div className="backtest-history-page">
    <section className="panel backtest-intro"><div><span className="status-pill muted"><i/>本地历史</span><h2>回测记录管理</h2><p>名称格式：策略名－自定义名称－北京时间戳。单次与多轮测试统一保存，选择记录查看统计、资金曲线和当时的完整参数。</p></div><a className="button ghost small" href="#backtest">新建回测</a></section>
    <section className="panel history-filter-panel"><div className="panel-heading"><h2><Search size={15}/>筛选记录</h2><button className="text-button" disabled={loading||detailLoading||saving} onClick={()=>{setRevision(v=>v+1);setDetailRevision(v=>v+1);}}><RefreshCw size={13}/>刷新</button></div>
      <form className="backtest-body history-filter-grid" onSubmit={apply}>
        <label className="field"><span className="field-label">品种</span><select aria-label="历史品种" value={draft.root} onChange={e=>setDraft({...draft,root:e.target.value})}><option value="">全部品种</option>{['MNQ','MES','MGC','MCL'].map(root=><option key={root}>{root}</option>)}</select></label>
        <label className="field"><span className="field-label">策略</span><select aria-label="历史策略" value={draft.strategy_id} onChange={e=>setDraft({...draft,strategy_id:e.target.value})}><option value="">全部策略</option>{strategies.map(s=><option value={s.id} key={s.id}>{s.name}</option>)}</select></label>
        <label className="field"><span className="field-label">时间口径</span><select aria-label="历史时间口径" value={draft.time_basis} onChange={e=>setDraft({...draft,time_basis:e.target.value})}><option value="created">记录创建时间</option><option value="data">回测行情区间（有交集）</option></select></label>
        {(['from','to']as const).map(key=><label className="field" key={key}><span className="field-label">{key==='from'?'开始':'结束'}时间 · UTC+8</span><input aria-label={'历史'+(key==='from'?'开始':'结束')+'时间'} type="datetime-local" value={draft[key]} onChange={e=>setDraft({...draft,[key]:e.target.value})}/></label>)}
        <label className="field history-pattern"><span className="field-label">名称正则</span><input aria-label="历史名称正则" placeholder="例如：十月|低回撤 或 ^随机下单-" maxLength={200} value={draft.pattern} onChange={e=>setDraft({...draft,pattern:e.target.value})}/><small>匹配完整名称，默认忽略大小写；直接填写表达式，无需 / /。</small></label>
        <div className="history-filter-actions"><button className="button primary small" type="submit" disabled={loading}>筛选</button><button className="button ghost small" type="button" onClick={()=>{setDraft({...defaults});setFilters({...defaults});setOffset(0);select('');sessionStorage.removeItem('backtest-history-filters');}}>重置条件</button></div>
      </form>{error&&<div className="error-banner" role="alert">{error}</div>}
    </section>
    <div className="history-record-layout"><section className="panel history-list-panel"><div className="panel-heading"><h2>历史记录 <span className="count-badge">{records.total}</span></h2><span className="dim">按创建时间倒序</span></div>
      <div className="history-record-list" aria-busy={loading}>{loading?<p className="backtest-note">正在查找记录…</p>:records.rows.length?records.rows.map(row=><button type="button" className={'history-record'+(selected===row.id?' selected':'')} key={row.id} data-record-id={row.id} aria-pressed={selected===row.id} aria-label={'查看回测 '+row.name} onClick={()=>select(row.id)}><strong>{row.name}</strong><span>{row.root} · {row.strategy_name} · {row.rounds} 轮 <b>{statusNames[row.status]||row.status}</b></span><small>创建 {stamp(row.created_at)} · UTC+8</small><small>行情 {stamp(row.from)} — {stamp(row.to)}</small></button>):<p className="backtest-note">{error?'请调整筛选条件后重试。':'没有符合条件的回测记录。'}</p>}</div>
      <div className="history-pagination"><span>{records.total?`${offset+1}–${Math.min(offset+20,records.total)} / ${records.total}`:'0 条'}</span><button className="button ghost small" disabled={loading||offset===0} onClick={()=>setOffset(Math.max(0,offset-20))}>上一页</button><button className="button ghost small" disabled={loading||offset+20>=records.total} onClick={()=>setOffset(offset+20)}>下一页</button></div>
    </section><div className="history-detail" aria-busy={detailLoading}>
      {!selected?<section className="panel backtest-empty"><Search size={32}/><h2>选择一条回测记录</h2><p>查看三部分单号期望、各阶段交易统计、资金曲线及已保存参数。</p></section>:detailLoading?<section className="panel backtest-loading">正在读取回测详情…</section>:null}
      {detailError&&<div className="error-banner" role="alert">{detailError}</div>}
      {detail&&<><section className="panel history-record-header"><h2>{detail.name}</h2><p>{detail.strategy_name} · {detail.config.symbol} · {detail.created_at!=null?stamp(detail.created_at):'—'} · UTC+8</p><form className="history-rename" onSubmit={rename}><label className="field"><span className="field-label">修改自定义名称</span><input aria-label="历史记录自定义名称" value={name} maxLength={80} onChange={e=>setName(e.target.value)} placeholder="未命名" disabled={saving}/></label><button className="button ghost small" disabled={saving||name===(detail.custom_name||'')} type="submit">{saving?'正在保存…':'保存名称'}</button></form><p role="status">{renameStatus||'策略名和创建时间保持不变，只修改自定义名称。'}</p></section>
        <BacktestBatchResults batch={detail} batches={[]} onBatch={()=>{}} showChooser={false}/>
        <section className="panel history-parameters"><div className="panel-heading"><h2>本次回测参数</h2><span className="dim">保存时的配置</span></div>{groups.map(([title,fields])=><details key={title} open><summary>{title}</summary><dl>{fields.map(([key,label])=><div key={key}><dt>{label}</dt><dd>{valueText(key,detail.config[key])}</dd></div>)}</dl></details>)}</section>
      </>}
    </div></div>
  </div>;
}
