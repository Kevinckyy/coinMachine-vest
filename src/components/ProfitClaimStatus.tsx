import {useState} from 'react';

export type ProfitClaimRecord={id:string;name:string;status:string;target_balance:number;amount?:number;
  received_preview?:number;balance_after?:number;observed_balance?:number;error?:string;reentry_scheduled?:boolean};
const labels:Record<string,string>={queued:'等待领取',preparing:'准备领取',submitted:'提交中，等待余额核验',unknown:'领取结果待核验',
  balance_mismatch:'余额未回到账号规模',verified:'余额已核验',cancelled:'已取消未提交领取'};

export default function ProfitClaimStatus({claims,running,onAction}:{claims:ProfitClaimRecord[];running:boolean;
  onAction:(id:string,action:'reconcile'|'cancel')=>Promise<unknown>}){
  const [busy,setBusy]=useState('');
  if(!claims.length)return null;
  const pending=claims.filter(c=>!['verified','cancelled'].includes(c.status));
  const shown=pending.length?pending:claims.slice(-1);
  const act=async(id:string,action:'reconcile'|'cancel')=>{setBusy(id);try{await onAction(id,action);}finally{setBusy('');}};
  return <section className="panel profit-claim-status" aria-label="Live 利润领取状态">
    <div className="panel-heading"><h2>Live 利润领取</h2><span>{pending.length?`${pending.length} 个账号待完成`:'余额核验已完成'}</span></div>
    {shown.map(c=><div className="profit-claim-row" key={c.id}>
      <strong>{c.name} · {labels[c.status]||c.status}</strong>
      <p>初始规模 {c.target_balance.toLocaleString()} USDC{c.amount!=null?` · 申请 ${c.amount} USDC`:''}
        {c.received_preview!=null?` · 预览到账 ${c.received_preview} USDC`:''}</p>
      {(c.observed_balance??c.balance_after)!=null&&<p>实际核对余额：{c.observed_balance??c.balance_after} USDC</p>}
      <p>{c.error|| (c.status==='verified'?'返回交易页余额核验通过；实际到账以平台为准。':'领取未核验完成前暂停新开仓，已有持仓继续受保护。')}</p>
      {['submitted','unknown','balance_mismatch'].includes(c.status)&&<>
        <small>在插件只读模式选择此 Live 号，返回 NQ 交易页并保持活动持仓面板可见，再核对余额。此按钮不会重新提交领取。</small>
        <button type="button" className="button ghost small" disabled={!!busy} onClick={()=>void act(c.id,'reconcile')}>核对领取后余额</button>
      </>}
      {['queued','preparing'].includes(c.status)&&!running&&<button type="button" className="button ghost small" disabled={!!busy}
        onClick={()=>void act(c.id,'cancel')}>取消未提交领取</button>}
    </div>)}
  </section>;
}
