/* Post-fill native stop calibration. One scoped amendment; never resubmits entry. */
(() => {
  'use strict';
  const NQ_PRICE_TICK = 0.25;

  function plan(position, target) {
    if (target == null) return {status:position.native_sl_absent === true ? 'omitted' : 'mismatch',target_risk:null,actual_risk:null,target_price:null};
    const {entry_price:entry,quantity,side,native_sl_price:stop}=position;
    if (![entry,quantity,target].every(v=>typeof v==='number'&&Number.isFinite(v)&&v>0)||!['buy','sell'].includes(side)) throw Error('止损校验缺少真实成交均价、数量或方向');
    const sign = side === 'sell' ? 1 : -1;
    const ticks = (entry + sign * target / quantity) / NQ_PRICE_TICK;
    if (!Number.isFinite(ticks)) throw Error('无法计算有效止损触发价');
    // At the midpoint, choose the tick with lower risk.
    const nearestTick = side === 'sell' ? Math.ceil(ticks - 0.5) : Math.floor(ticks + 0.5);
    const price = nearestTick * NQ_PRICE_TICK;
    if (!(price>0&&sign*(price-entry)>0)) throw Error('止损金额小于当前数量可表示的最小价格间距');
    const actual=typeof stop==='number'&&Number.isFinite(stop)&&stop>0?sign*(stop-entry)*quantity:null;
    return {status:actual>0&&Math.abs(stop-price)<1e-6?'verified':'mismatch',target_risk:target,
      actual_risk:actual==null?null:Math.round(actual*1e8)/1e8,target_price:price};
  }
  async function correct(adapter, job, filled) {
    const a=adapter, core=globalThis.VestBridgeCore;
    let dialog=null, submitted=false;
    const check=()=>{
      a.guard(job); a.noDialogs(dialog);
      const current=a.read();
      if(!core.matchesPosition(current,filled)||!core.sameQuantity(current.entry_price,filled.entry_price)) throw Error('止损校正期间账户、持仓或成交均价变化');
      return current;
    };
    try {
      let current=check(), expected=plan(current,job.stop_loss);
      if (expected.status==='verified'||expected.status==='omitted') return {...current,stop_check:expected};
      if (job.stop_loss==null) throw Error('本单要求不设止损，但持仓列表仍有止损，请人工核对');
      a.click(a.one(`[data-testid="tpsl-edit-open-${CSS.escape(filled.position_id)}"]`,document,true));
      const until=Date.now()+4000;
      while(Date.now()<until){await a.delay(100);a.guard(job);
        const dialogs=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(a.visible);
        if(dialogs.length){if(dialogs.length!==1)throw Error('止损编辑窗口不唯一');dialog=dialogs[0];break;}}
      if(!dialog)throw Error('止损编辑窗口未打开');
      const one=id=>a.one(`[data-testid="${id}"]`,dialog,true);
      const text=e=>core.normalized(e.textContent);
      const state=()=>[...dialog.querySelectorAll('input,button[role="combobox"],button[data-testid*="unit-"]')].map(e=>[e.dataset.testid,e instanceof HTMLInputElement?e.value:text(e)]);
      const validate=()=>{
        const live=check();
        if(!(live.price>0)||(live.side==='buy'?live.price<=expected.target_price:live.price>=expected.target_price))throw Error('行情已越过目标止损价，停止修改并保留实际仓位供核对');
        const heading=dialog.querySelector('h2');
        if(!/^(?:编辑 NQ 止盈\/止损|Edit NQ (?:TP\/SL|Take Profit\/Stop Loss))$/i.test(text(heading)))throw Error('无法确认 NQ 止损编辑窗口');
        if(!core.sameQuantity(core.parseMoney(text(one('position-entry-'+filled.position_id))),filled.entry_price))throw Error('编辑窗口成交均价不匹配');
        if(dialog.querySelectorAll('[data-testid^="tpsl-edit-sl-input-"]').length!==1)throw Error('多级止损不能自动合并，请人工核对');
        if(!/^(?:价格|Price)$/i.test(text(one('tpsl-edit-sl-unit-0')))||text(one('tpsl-edit-sl-size-unit-0'))!=='%'||
          !core.sameQuantity(core.parseMoney(one('tpsl-edit-sl-qty-0').value),100)||
          !/^(?:市价|Market)$/i.test(text(one('tpsl-edit-sl-execution-0'))))throw Error('止损必须是价格触发、100% 持仓的市价止损');
        const quantityText=text(one('tpsl-edit-sl-size-equiv-0'));
        if(!/\bNQ$/.test(quantityText)||!core.sameQuantity(core.parseMoney(quantityText),filled.quantity))throw Error('编辑窗口止损数量不匹配');
      };
      validate();
      const original=state(), sl=one('tpsl-edit-sl-input-0');
      if(!(sl instanceof HTMLInputElement)||sl.disabled||sl.readOnly)throw Error('止损价格不可编辑');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(sl,String(expected.target_price));
      sl.dispatchEvent(new Event('input',{bubbles:true}));sl.dispatchEvent(new Event('change',{bubbles:true}));
      await a.delay(180); validate();
      const unchanged=()=>JSON.stringify(state().filter(([id])=>id!=='tpsl-edit-sl-input-0'))===JSON.stringify(original.filter(([id])=>id!=='tpsl-edit-sl-input-0'));
      const accepted=()=>core.sameQuantity(core.parseMoney(one('tpsl-edit-sl-input-0').value),expected.target_price)&&one('tpsl-edit-sl-input-0').getAttribute('aria-invalid')!=='true';
      if(!unchanged()||!accepted())throw Error('止盈或其他保护设置变化，未保存止损');
      // A durable intent makes a lost reply non-repeatable. Recheck immediately
      // after IPC so revocation, a user edit or a closed position cannot submit.
      const intent=await a.intent(job,check(),expected.target_price);
      validate();if(!unchanged()||!accepted())throw Error('保存前保护参数变化');
      if(intent.authorize_click!==true||!core.sameQuantity(intent.target_price,expected.target_price))throw Error('止损校正已提交或结果未知，禁止重复保存');
      submitted=true; a.click(one('tpsl-edit-submit'));
      const deadline=Date.now()+8000;
      while(Date.now()<deadline){await a.delay(150);a.guard(job);
        if(dialog.isConnected&&a.visible(dialog))continue;
        dialog=null;current=check();expected=plan(current,job.stop_loss);
        if(expected.status==='verified')return {...current,stop_check:{...expected,corrected:true}};
      }
      throw Error('止损校正后未确认持仓列表已更新，请核对；禁止重复提交');
    } catch(error) {
      // A confirmed fill remains a fill even if the separate amendment fails.
      // The server records it, freezes new entries and retains the actual risk.
      if(dialog&&!submitted){try{const buttons=[...dialog.querySelectorAll('button[data-slot="dialog-close"]')].filter(a.visible);
        const cancel=buttons.find(e=>/^(?:取消|Cancel)$/.test(core.normalized(e.textContent)));if(cancel){a.guard(job);a.click(cancel);await a.delay(100);}}catch{}}
      let observed=filled;try{observed=a.read();if(!core.matchesPosition(observed,filled))observed=filled;}catch{}
      return {...observed,stop_check:{status:'failed',error:String(error.message).slice(0,250),submitted}};
    }
  }
  globalThis.VestStopProtection={plan,correct};
})();
