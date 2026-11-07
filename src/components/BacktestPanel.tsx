import {useEffect,useRef,useState} from 'react';
import {Activity,Download,Play,RefreshCw} from 'lucide-react';
import BacktestBatchResults,{type Batch,wallDuration} from './BacktestBatchResults';

type Config={strategy_id:string;record_name:string;root:string;symbol:string;interval:number;from:number;to:number;account_count:number|'infinity';seed:number|string;rng_algorithm:string;seed_policy:string;rounds:number;parallel_workers:number;
  initial_capital:number;phase1_goal:number;phase2_goal:number;max_drawdown:number;daily_loss_percent:number;profit_split_percent:number;
  account_fee:number;payout_min_profit:number;live_profit_target:number;payout_delay_hours:number;risk_reset_hour:number;strategy_reset_hour:number;
  initial_phase:string;repeat_daily:boolean;pool_limits_enabled:boolean;exam_pool_limit:number|'infinity';live_pool_limit:number|'infinity';batch_size:number;side:string;size_percent:number;stop_loss:number;skip_stop_loss_on_low_drawdown:boolean;
  drawdown_stop_loss_threshold:number;order_delay_min:number;order_delay_max:number;batch_delay_min:number;batch_delay_max:number;
  fee_bps:number;slippage_mode:string;slippage_ticks:number;slippage_usd:number;slippage_bps:number;funding_bps_hourly:number;leverage:number;sizing:string;cost_source:string};
type NumericKey={[K in keyof Config]:Config[K] extends number?K:never}[keyof Config];
type Inventory={symbol:string;root:string;interval:number;bars:number;first_time:number;last_time:number;metadata:string};
type Quote={symbol:string;fee_bps:number;slippage_bps?:number;slippage_usd?:number;observed_at:number;notional?:number;source_text:string};
type Estimate={config:Config;per_round_seconds:number;total_seconds:number;source:string;note:string;samples:number};
type Job={id:string;kind:string;status:string;detail:{root?:string;completed?:number;total?:number;bars?:number;accounts?:number;trades?:number;error?:string;run_id?:string;batch_id?:string;estimate?:Estimate;rounds_completed?:number;rounds_total?:number;parallel_workers?:number;elapsed_seconds?:number;remaining_seconds?:number;estimated_total_seconds?:number;errors?:{root:string;error:string}[]}};
type Meta={strategies:{id:string;name:string;description:string}[];instruments:Record<string,{name:string;vest_symbol:string;leverage:number}>;
  inventory:Inventory[];defaults:Partial<Config>;saved_form:Partial<Config>|null;slippage_migration?:string|null;current_profile:{source:string}|null;
  runs:{id:string;created_at:number;config:Config;metrics:Record<string,number|null>}[];jobs?:Job[];execution_quotes:Record<string,Quote>;sources:{title:string;url:string}[];parallelism_limit:number;batches:{id:string;config:{rounds:number;seed:number|string;seed_policy?:string}}[]};

async function api<T>(url:string,body?:unknown,timeout=15000):Promise<T>{
  const r=await fetch(url,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(timeout)});
  const data=await r.json();if(!r.ok)throw new Error(data.error||'回测服务请求失败');return data;
}
const number=(n:number|null|undefined,d=2)=>n==null?'—':n.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const stamp=(t:number)=>new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const localDate=(t:number)=>new Date(t*1000+8*3600000).toISOString().slice(0,16);
const timingKey=(config:Config)=>JSON.stringify(Object.fromEntries(Object.keys(config).sort().filter(key=>!['seed','seed_policy','rounds','parallel_workers','record_name'].includes(key)).map(key=>[key,config[key as keyof Config]])));

export default function BacktestPanel(){
  const [meta,setMeta]=useState<Meta|null>(null),[form,setForm]=useState<Config|null>(null),[error,setError]=useState('');
  const [busy,setBusy]=useState(false),[job,setJob]=useState<Job|null>(null);
  const [slippageNotice,setSlippageNotice]=useState('');
  const [roots,setRoots]=useState(['MNQ','MES','MGC','MCL']),[contracts,setContracts]=useState<Record<string,string>>({});
  const restored=useRef(false);
  const controls=useRef<HTMLDivElement>(null);
  const finiteAccountCount=useRef(10000);
  const [estimate,setEstimate]=useState<Estimate|null>(null),[batch,setBatch]=useState<Batch|null>(null),[cancelRequested,setCancelRequested]=useState(false);
  const batchSelection=useRef({id:''});
  const formTimingKey=form?timingKey(form):'';
  const loadBatch=async(id:string)=>{batchSelection.current={id};const b=await api<Batch>('/api/backtests/batches/'+id+'?summary_only=1',undefined,120000);if(batchSelection.current.id===id)setBatch(b);};
  useEffect(()=>{
    if(!form?.symbol)return;let gone=false;
    const timer=setTimeout(()=>{if([...controls.current?.querySelectorAll<HTMLInputElement>('input[type="number"]')||[]].some(input=>!input.checkValidity()))return;
      void api<{estimate?:Estimate}>('/api/backtests/estimate',{config:form,cached_only:true}).then(r=>{if(!gone)setEstimate(r.estimate||null);}).catch(()=>{if(!gone)setEstimate(null);});},600);
    return()=>{gone=true;clearTimeout(timer);};
  },[formTimingKey,job?.status]);
  const running=job && ['queued','running'].includes(job.status);
  const refresh=async()=>{
    const m=await api<Meta>('/api/backtests/meta');setMeta(m);
    if(!restored.current){
      restored.current=true;
      setSlippageNotice(m.slippage_migration||'');
      const active=m.jobs?.find(j=>['queued','running'].includes(j.status));
      if(m.batches?.length)void loadBatch(m.batches[0].id).catch(e=>setError('读取统计失败：'+e.message));
      if(active)setJob(active);
    }
    setForm(old=>{
      if(old){if(!old.symbol){const i=m.inventory.find(x=>x.root===old.root&&x.interval===old.interval);if(i)return{...old,symbol:i.symbol};}return old;}
      const saved=m.saved_form||{},root=saved.root||'MNQ',interval=saved.interval||60;
      const inv=m.inventory.find(x=>x.root===root&&x.interval===interval),end=Math.floor(Date.now()/60000)*60-60;
      return {...m.defaults,root,symbol:inv?.symbol||'',interval,from:end-30*86400,to:end,leverage:m.instruments[root].leverage,...saved} as Config;
    });
    return m;
  };
  useEffect(()=>{void refresh().catch(e=>setError('回测接口尚未就绪，请更新并重启本地服务：'+e.message));},[]);
  useEffect(()=>{
    if(!job || !['queued','running'].includes(job.status))return;
    let gone=false;
    const poll=async()=>{
      try{
        const next=await api<Job>('/api/backtests/jobs/'+job.id);if(gone)return;
        if(next.detail.batch_id)await loadBatch(next.detail.batch_id);
        if(next.detail.estimate&&!gone)setEstimate(next.detail.estimate);
        if(!['queued','running'].includes(next.status)){
          await refresh();
          if(next.status==='error')setError(next.detail.error||next.detail.errors?.map(x=>x.root+'：'+x.error).join('；')||'任务失败');
        }
        if(!gone){setJob(next);if(!['queued','running'].includes(next.status))setCancelRequested(false);}
      }catch(e){if(!gone)setError((e as Error).message);}
    };
    void poll();const timer=setInterval(poll,1000);return()=>{gone=true;clearInterval(timer);};
  },[job?.id,job?.status]);
  const update=(patch:Partial<Config>)=>{if(Object.keys(patch).some(k=>['slippage_mode','slippage_ticks','slippage_usd'].includes(k)))setSlippageNotice('');setForm(old=>old?{...old,...patch,...(!('cost_source'in patch)&&Object.keys(patch).some(k=>['fee_bps','slippage_mode','slippage_ticks','slippage_usd'].includes(k))?{cost_source:'用户设置的固定手续费和不利滑点假设'}:{})}:old);};
  const submit=async(kind:'sync'|'run'|'estimate')=>{
    if(!form)return;setError('');
    if(kind!=='sync'){
      const invalid=[...(controls.current?.querySelectorAll<HTMLInputElement>('input[type="number"]')||[])].find(input=>!input.checkValidity());
      if(invalid){
        const label=invalid.closest('label')?.querySelector('.field-label')?.textContent||'回测参数';
        const kind=invalid.step==='1'?'整数':'有效数字';
        const range=invalid.min&&invalid.max?`${invalid.min}–${invalid.max} 之间的${kind}`:invalid.min?`不小于 ${invalid.min} 的${kind}`:kind;
        setError(`${label}必须为${range}`);invalid.focus();invalid.scrollIntoView({block:'center'});invalid.reportValidity();return;
      }
    }
    setBusy(true);
    try{
      const body=kind==='run'?form:kind==='estimate'?{config:form}:{roots,contracts:Object.fromEntries(Object.entries(contracts).filter(([,v])=>v.trim())),interval:form.interval,from:form.from,to:form.to};
      const r=await api<{job_id:string;kind:string;status:string;estimate?:Estimate}>('/api/backtests/'+kind,body);
      if(r.estimate)setEstimate(r.estimate);else{setJob({id:r.job_id,kind:r.kind||kind,status:r.status,detail:{}});setCancelRequested(false);}
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  };
  if(!meta||!form)return <section className="panel backtest-loading"><Activity size={28}/><p>{error||'正在读取本地行情与回测配置…'}</p><button className="button ghost" onClick={()=>void refresh().catch(e=>setError(e.message))}>重新连接</button></section>;
  const inventory=meta.inventory.filter(x=>x.root===form.root&&x.interval===form.interval);
  const selected=inventory.find(x=>x.symbol===form.symbol),quote=meta.execution_quotes[meta.instruments[form.root].vest_symbol];
  const quoteUsd=quote?.slippage_usd??(quote?.notional!=null&&quote.slippage_bps!=null?quote.notional*quote.slippage_bps/10000:null);
  const numeric=(key:NumericKey,label:string,options:{min?:number;max?:number;step?:number;hint?:string}={})=><label className="field" key={key}><span className="field-label">{label}</span><input aria-label={'回测'+label} type="number" required min={options.min??0} max={Number.isFinite(options.max)?options.max:undefined} step={options.step??'any'} value={Number.isFinite(form[key])?form[key]:''} onChange={e=>update({[key]:e.target.valueAsNumber})}/>{options.hint&&<small>{options.hint}</small>}</label>;
  const metadata=(item:Inventory)=>{try{return JSON.parse(item.metadata) as {continuous?:boolean;source_description?:string;roll_count?:number;tick_size?:number;point_value?:number};}catch{return {};}};
  const dataLabel=(item:Inventory)=>metadata(item).continuous?item.root+' 连续 · 后复权':item.symbol;
  const chooseSeries=(symbol:string)=>{const next=meta.inventory.find(x=>x.symbol===symbol&&x.interval===form.interval);let range:Partial<Config>={};if(next&&(form.from>next.last_time||form.to<next.first_time))range={from:Math.max(next.first_time,next.last_time-30*86400),to:next.last_time};update({symbol,...range});};
  const chooseRoot=(root:string)=>{const options=meta.inventory.filter(x=>x.root===root&&x.interval===form.interval);const next=options.find(x=>metadata(x).continuous)||options[0];update({root,symbol:next?.symbol||'',leverage:meta.instruments[root].leverage});};
  const accountCountField=<label className="field"><span className="field-label">模拟账户数</span><input aria-label="回测模拟账户数" type="number" min="1" max="1000000" step="1" required={form.account_count!=='infinity'} disabled={form.account_count==='infinity'} placeholder={form.account_count==='infinity'?'∞':''} value={typeof form.account_count==='number'&&Number.isFinite(form.account_count)?form.account_count:''} onChange={e=>update({account_count:e.target.valueAsNumber})}/><small>支持 1–1,000,000 个整数；也可选择 ∞ 持续补号。</small></label>;
  const poolLimit=(key:'exam_pool_limit'|'live_pool_limit',label:string)=><label className="field"><span className="field-label">{label}</span><input aria-label={'回测'+label} type="number" min="1" max="1000000" step="1" required={form.pool_limits_enabled&&form[key]!=='infinity'} disabled={!form.pool_limits_enabled||form[key]==='infinity'} placeholder={form[key]==='infinity'?'∞':''} value={typeof form[key]==='number'&&Number.isFinite(form[key])?form[key]:''} onChange={e=>update({[key]:e.target.valueAsNumber})}/><span><input aria-label={label+'不限'} type="checkbox" disabled={!form.pool_limits_enabled} checked={form[key]==='infinity'} onChange={e=>update({[key]:e.target.checked?'infinity':key==='live_pool_limit'?10:100})}/> ∞ 不限</span></label>;
  const currentEstimate=estimate&&timingKey(estimate.config)===formTimingKey?estimate:null;
  const workers=Math.min(form.parallel_workers,form.rounds);
  const estimatedTotal=currentEstimate?Math.ceil(form.rounds/workers)*currentEstimate.per_round_seconds*(workers>1?1.15:1)+(workers>1?1.5:0):null;
  return <div className="backtest-page">
    <section className="panel backtest-intro"><div><span className="status-pill positive"><i/>独立回测</span><h2>历史行情 × 自动策略 × Vest 账户规则</h2><p>只读读取 8090 的行情，模拟账户与结果单独保存。回测不会向 NT8 或 Vest 发送交易操作。</p></div><button className="button ghost small" onClick={()=>void refresh().catch(e=>setError(e.message))}><RefreshCw size={14}/>刷新本地数据</button></section>
    {error&&<div className="error-banner" role="alert">{error}</div>}
    <div className="backtest-layout"><div className="backtest-controls" ref={controls}>
      <section className="panel"><div className="panel-heading"><h2>01 · 历史数据</h2><span className="outline-badge">127.0.0.1:8090 · GET</span></div><div className="backtest-body">
        <div className="two-columns"><label className="field"><span className="field-label">回测品种</span><select aria-label="回测品种" value={form.root} onChange={e=>chooseRoot(e.target.value)}>{Object.entries(meta.instruments).map(([root,s])=><option value={root} key={root}>{root} · {s.name}</option>)}</select></label>
          <label className="field"><span className="field-label">K线周期</span><select aria-label="回测K线周期" value={form.interval} onChange={e=>{const interval=Number(e.target.value),i=meta.inventory.find(x=>x.root===form.root&&x.interval===interval);update({interval,symbol:i?.symbol||''});}}>{[60,300,900,3600].map(x=><option value={x} key={x}>{x/60} 分钟</option>)}</select></label></div>
        <label className="field"><span className="field-label">本地行情数据（连续 / 月份）</span><select aria-label="回测月份合约" value={form.symbol} onChange={e=>chooseSeries(e.target.value)}><option value="">请先拉取该品种行情</option>{inventory.map(x=><option value={x.symbol} key={x.symbol}>{dataLabel(x)} · {number(x.bars,0)} 根</option>)}</select></label>
        <div className="two-columns">{(['from','to']as const).map(key=><label className="field" key={key}><span className="field-label">{key==='from'?'开始':'结束'}时间 · UTC+8</span><input aria-label={key==='from'?'回测开始时间':'回测结束时间'} type="datetime-local" value={localDate(form[key])} onChange={e=>{const time=Date.parse(e.target.value+':00+08:00')/1000;if(Number.isFinite(time))update({[key]:time});}}/></label>)}</div>
        <details className="backtest-import"><summary>拉取品种与月份</summary><p>默认读取平台当前原生月份，不拼接换月，也不补造缺口。</p>{Object.keys(meta.instruments).map(root=><div className="backtest-contract-row" key={root}><label><input type="checkbox" checked={roots.includes(root)} onChange={e=>setRoots(old=>e.target.checked?[...old,root]:old.filter(x=>x!==root))}/>{root}</label><input aria-label={root+' 历史合约'} placeholder="留空使用平台当前月份" value={contracts[root]||''} onChange={e=>setContracts(old=>({...old,[root]:e.target.value}))}/></div>)}</details>
        <button className="button ghost backtest-wide" disabled={busy||!!running||!roots.length} onClick={()=>void submit('sync')}><Download size={15}/>拉取K线并保存到本地</button>
        <div className="backtest-cache-list">{meta.inventory.map(x=><div key={x.symbol+':'+x.interval}><strong>{dataLabel(x)}</strong><span>{x.interval/60}m · {number(x.bars,0)} 根</span><small>{x.first_time?stamp(x.first_time)+' — '+stamp(x.last_time):'尚无有效K线'}</small></div>)}</div>
        {selected&&<p className="form-footnote">当前缓存 {number(selected.bars,0)} 根；回测可离线运行。{metadata(selected).continuous&&<>本数据为本地构建的连续合约，{metadata(selected).roll_count} 次换月及复权记录已保存。{metadata(selected).source_description}。历史源存在缺口，休市与缺失时段不补造 K 线。</>}</p>}
      </div></section>
      <section className="panel"><div className="panel-heading"><h2>02 · 策略与执行参数</h2><button type="button" className="text-button" onClick={()=>update(Object.fromEntries(['strategy_id','batch_size','side','size_percent','stop_loss','skip_stop_loss_on_low_drawdown','drawdown_stop_loss_threshold','order_delay_min','order_delay_max','batch_delay_min','batch_delay_max'].map(k=>[k,meta.defaults[k as keyof Config]])))}>沿用已保存参数</button></div><div className="backtest-body">
        <label className="field"><span className="field-label">自动策略</span><select aria-label="回测自动策略" value={form.strategy_id} onChange={e=>update({strategy_id:e.target.value})}>{meta.strategies.map(s=><option value={s.id} key={s.id}>{s.name}</option>)}</select></label>
        <label className="backtest-check"><input aria-label="回测无限账户" type="checkbox" checked={form.account_count==='infinity'} onChange={e=>{if(e.target.checked){if(typeof form.account_count==='number'&&Number.isFinite(form.account_count))finiteAccountCount.current=form.account_count;update({account_count:'infinity'});}else update({account_count:finiteAccountCount.current});}}/>∞ 不限账户总数，账户不足一批时补充新号，持续至结束时间</label><div className="two-columns">{accountCountField}{numeric('batch_size','每批账户数',{min:1,max:form.account_count==='infinity'?10000:Math.min(form.account_count,10000),step:1})}<label className="field"><span className="field-label">方向</span><select aria-label="回测方向" value={form.side} onChange={e=>update({side:e.target.value})}><option value="random">随机做多 / 做空</option><option value="buy">仅做多</option><option value="sell">仅做空</option></select></label></div>
        <div className="two-columns"><label className="field"><span className="field-label">仓位大小</span><select aria-label="回测仓位大小" value={form.size_percent} onChange={e=>update({size_percent:Number(e.target.value)})}>{[25,50,75,100].map(x=><option value={x} key={x}>{x}%</option>)}</select></label>{numeric('leverage','杠杆',{min:1,max:meta.instruments[form.root].leverage,hint:'默认使用官方市场上限，可按订单界面的实际杠杆修改。'})}</div>
        <label className="backtest-check"><input aria-label="回测账号池限制" type="checkbox" checked={form.pool_limits_enabled} onChange={e=>update({pool_limits_enabled:e.target.checked})}/>限制同时存活的账号池</label>
        <div className="two-columns">{poolLimit('exam_pool_limit','考试号上限 m')}{poolLimit('live_pool_limit','Live号上限 n')}</div>
        <p className="form-footnote">考试 1/2、2/2 合计占用 m；失败释放名额。Live 满时，二阶段已通过的账号保留资格并暂停交易，按通过顺序等待空位。Live 优先选批和下单，开启限制时允许不足一批执行。未启用的计划号不占池位。</p>
        <label className="field"><span className="field-label">数量模式</span><select value={form.sizing} onChange={e=>update({sizing:e.target.value})}><option value="micro_contracts">整数微型合约等值数量</option><option value="vest_equivalent">允许小数的 Vest 等值数量（回测假设）</option></select></label>
        <div className="two-columns">{numeric('stop_loss','每笔止损 USDC',{min:.01})}{numeric('drawdown_stop_loss_threshold','不设止损阈值 USDC',{min:.01})}</div>
        <label className="backtest-check"><input type="checkbox" checked={form.skip_stop_loss_on_low_drawdown} onChange={e=>update({skip_stop_loss_on_low_drawdown:e.target.checked})}/>剩余最大回撤小于阈值时不设置止损</label>
        <div className="two-columns">{numeric('order_delay_min','下单最小间隔秒')}{numeric('order_delay_max','下单最大间隔秒')}{numeric('batch_delay_min','批次最小间隔秒')}{numeric('batch_delay_max','批次最大间隔秒')}</div>
        <label className="backtest-check"><input type="checkbox" checked={form.repeat_daily} onChange={e=>update({repeat_daily:e.target.checked})}/>每个交易日重新启动一轮随机策略</label><p className="form-footnote">关闭后同一账户每阶段只在本轮参与一次；秒级等待会向下一根K线开盘取整。</p>
      </div></section>
      <section className="panel"><div className="panel-heading"><h2>03 · Vest 方案与出金模拟</h2></div><div className="backtest-body"><p className="form-footnote">{meta.current_profile?.source||'默认 5K 两阶段方案：10% / 5% 目标、3% 日亏损、6% 固定回撤、80% 分润。'}</p>
        <div className="two-columns">{numeric('initial_capital','初始资金',{min:1})}{numeric('account_fee','单号购买成本')}{numeric('phase1_goal','第一阶段目标',{min:.01})}{numeric('phase2_goal','第二阶段目标',{min:.01})}{numeric('max_drawdown','固定最大回撤',{min:.01})}{numeric('daily_loss_percent','日亏损比例 %',{max:100,hint:'填 0 表示方案没有日亏损限制。'})}{numeric('risk_reset_hour','风控日切（美东时）',{max:23,step:1,hint:'默认美东20:00，可填写0–23整点；按所填时间回测。'})}{numeric('strategy_reset_hour','开仓额度日切（美东时）',{max:23,step:1,hint:'默认美东20:00，可填写0–23整点；保存后沿用自定义时间。'})}</div>
        <label className="field"><span className="field-label">起始阶段</span><select value={form.initial_phase} onChange={e=>update({initial_phase:e.target.value})}><option value="1/2">第一阶段（完整考试）</option><option value="2/2">第二阶段</option><option value="live">live（不统计考试通过率）</option></select></label>
        <div className="two-columns">{numeric('live_profit_target','live 出金平仓目标',{hint:'模拟退出条件，非平台强制目标；填 0 只在自然平仓后申请分润。'})}{numeric('payout_min_profit','最低可申请利润',{min:.01})}{numeric('profit_split_percent','分润比例 %',{max:100})}{numeric('payout_delay_hours','分润到账延迟小时')}</div>
      </div></section>
      <section className="panel"><div className="panel-heading"><h2>04 · 手续费与滑点</h2></div><div className="backtest-body"><div className="two-columns">{numeric('fee_bps','单边手续费 bps',{hint:'官方指数和商品默认 0.25 bps = 0.0025%。'})}<div className="field"><span className="field-label">滑点单位 · 开平仓各一次</span><div className="mode-switch" role="group" aria-label="回测滑点单位"><button type="button" aria-label="回测滑点单位 tick" aria-pressed={form.slippage_mode==='ticks'} className={form.slippage_mode==='ticks'?'active':''} onClick={()=>update({slippage_mode:'ticks'})}>tick</button><button type="button" aria-label="回测滑点单位 USD" aria-pressed={form.slippage_mode==='usd'} className={form.slippage_mode==='usd'?'active':''} onClick={()=>update({slippage_mode:'usd'})}>USD</button></div><small>两种单位分别保留填写值。</small></div>{form.slippage_mode==='ticks'?numeric('slippage_ticks','每边滑点 tick',{max:1000,hint:selected?`1 tick = ${number(metadata(selected).tick_size,4)} 点；金额随合约数量变化。`:'按品种最小跳动计算。'}):numeric('slippage_usd','每笔每边滑点 USD',{max:1e7,hint:'整笔订单的总金额；开仓、平仓各扣一次，不乘合约数量。'})}{numeric('funding_bps_hourly','每小时资金费 bps',{min:-100,hint:'默认 0；CME K线不包含历史 Vest 资金费。'})}</div>
        {slippageNotice&&<p className="form-footnote" role="status">{slippageNotice}</p>}
        <div className="backtest-quote"><strong>{quote?'插件已读取订单界面费用':'尚无当前订单界面的插件报价'}</strong><p>{quote?`手续费 ${quote.fee_bps} bps；整笔订单每边滑点 ${quoteUsd==null?'缺少名义金额，无法换算 USD':number(quoteUsd,4)+' USD'} · ${stamp(quote.observed_at)}`:'插件可只读采集 NQ 订单面板费用，滑点结合报价时订单名义金额换算为 USD；也可手动填写。'}</p>{quote&&<button className="text-button" onClick={()=>update({fee_bps:quote.fee_bps,...(quoteUsd!=null?{slippage_mode:'usd',slippage_usd:quoteUsd}:{}),cost_source:'Vest 订单界面报价 '+stamp(quote.observed_at)+'；USD 滑点仅代表报价时整笔订单数量的估计'})}>{quoteUsd==null?'使用页面手续费':'使用这份页面报价'}</button>}</div>
        <p className="form-footnote">{form.cost_source}。当前盘口滑点和滑点容忍度都不能当成历史成交滑点；回测始终按保存的假设计算。</p>
      </div></section>
      <section className="panel backtest-plan"><div className="panel-heading"><h2>05 · 多轮随机种子与耗时</h2></div><div className="backtest-body">
        <label className="field"><span className="field-label">记录自定义名称</span><input aria-label="回测记录自定义名称" maxLength={80} placeholder="例如：十月低回撤对照" value={form.record_name} onChange={e=>update({record_name:e.target.value})}/><small>保存为“{meta.strategies.find(s=>s.id===form.strategy_id)?.name||form.strategy_id}－{form.record_name.trim()||'未命名'}－时间戳”，时间戳为提交时的北京时间。可在历史页修改自定义部分。</small></label>
        <div className="two-columns">{numeric('rounds','测试轮数',{min:1,max:4294967296,step:1,hint:'自由填写正整数，每轮重新运行所选时间范围。'})}{numeric('parallel_workers','并行任务数',{min:1,max:meta.parallelism_limit||1,step:1,hint:`最多 ${meta.parallelism_limit||1} 个 CPU 工作进程；实际并行数不超过轮数，并行越多内存占用越高。`})}</div>
        <p className="form-footnote" aria-label="回测随机规则">每次开始自动随机选择起始种子，每轮从系统随机源独立抽取 128 位种子，同批不重复。策略随机数采用 PCG64DXSM，实际种子与算法版本保存以便复现。每轮独立重置账户，∞ 设置作用于每一轮的账户池。</p>
        <div className="backtest-time-estimate" aria-label="回测耗时预估"><div><span>单轮预估</span><strong>{wallDuration(currentEstimate?.per_round_seconds)}</strong></div><div><span>{workers} 个任务并行 · 总预估</span><strong>{wallDuration(estimatedTotal)}</strong></div></div>
        <p className="form-footnote">{currentEstimate?`${currentEstimate.source}；粗估范围 ${wallDuration((estimatedTotal||0)*.65)} — ${wallDuration((estimatedTotal||0)*1.5)}。${currentEstimate.note}`:'尚无当前配置的测时。可先测量单轮，得到单轮和总耗时预估；该测量不添加回测结果。'}</p>
        <button className="button ghost backtest-wide" disabled={busy||!!running||!selected} onClick={()=>void submit('estimate')}><RefreshCw size={14}/>测量单轮耗时</button>
        <button className="button primary backtest-wide" disabled={busy||!!running||!selected} onClick={()=>void submit('run')}><Play size={15}/>{form.rounds>1?`开始 ${number(form.rounds,0)} 轮随机种子回测`:'使用本地K线开始回测'}</button>
      </div></section>
    </div><div className="backtest-results">
      {job&&<section className="panel backtest-job" role="status"><strong>{{sync:'历史读取',estimate:'耗时测量',batch:'多轮种子回测',run:'策略回测'}[job.kind]||'策略回测'} · {{queued:'排队中',running:'处理中',completed:'已完成',partial:'部分完成',cancelled:'已停止',error:'失败'}[job.status]||job.status}</strong><p>{job.detail.root||''} {job.detail.total?`${job.detail.completed||0} / ${job.detail.total}`:''} {job.detail.bars?`· 已读取 ${number(job.detail.bars,0)} 根`:''}</p>{job.detail.rounds_total!=null&&<p className="backtest-round-progress">完成 {job.detail.rounds_completed||0} / {job.detail.rounds_total} 轮 · {job.detail.parallel_workers} 个并行任务<br/>已用 {wallDuration(job.detail.elapsed_seconds)} · 剩余约 {wallDuration(job.detail.remaining_seconds)} · 总计约 {wallDuration(job.detail.estimated_total_seconds)}</p>}{running&&job.kind!=='sync'&&<button className="button ghost small" disabled={cancelRequested} onClick={()=>{setCancelRequested(true);void api('/api/backtests/jobs/'+job.id+'/cancel',{}).catch(e=>{setCancelRequested(false);setError(e.message);});}}>{cancelRequested?'正在停止…':'停止本次测试'}</button>}{job.detail.accounts!=null&&<p>已生成 {number(job.detail.accounts,0)} 个账户 · 已平仓 {number(job.detail.trades,0)} 笔</p>}{job.detail.errors?.map(e=><p key={e.root} className="negative">{e.root}：{e.error}</p>)}</section>}
      <BacktestBatchResults batch={batch} batches={meta.batches||[]} onBatch={id=>{void loadBatch(id).catch(e=>setError(e.message));}}/>
      {!batch&&!(meta.batches||[]).length&&<section className="panel backtest-empty"><Activity size={38}/><h2>多轮统计</h2><p>运行后显示汇总表现、各阶段平均单数、每号平均单数、平均持仓时间，以及最好 / 最差 / 平均资金曲线。</p></section>}
    </div></div>
  </div>;
}
