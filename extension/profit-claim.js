/* Public DOM only. A persisted server intent precedes the one financial click. */
(() => {
  'use strict';
  function units(value) {
    const text=String(value),match=text.match(/^(\d+)(?:\.(\d{1,6}))?$/);
    if(!match)throw new Error('领取金额精度不明确');
    return BigInt(match[1])*1000000n+BigInt((match[2]||'').padEnd(6,'0'));
  }
  function expectedAmount(available,policy) {
    const total=units(available);
    if(policy.live_payout_mode==='all')return Number(total)/1e6;
    if(policy.live_payout_mode==='amount')return Number(units(policy.live_payout_amount))/1e6;
    const text=String(policy.live_payout_percent),m=text.match(/^(\d+)(?:\.(\d+))?(?:e([+-]?\d+))?$/i);
    if(!m)throw new Error('领取比例无效或过小');
    const exponent=Number(m[3]||0)-(m[2]||'').length;
    const scale=10n**BigInt(Math.max(0,-exponent));
    const numerator=BigInt(m[1]+(m[2]||''))*10n**BigInt(Math.max(0,exponent));
    return Number(total*numerator/(100n*scale))/1e6;
  }
  function create(a) {
    let state=null;
    const dialogs=()=>[...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(a.visible);
    function own(loading=false) {
      if(!state)throw new Error('利润领取流程已停止');
      a.guard(state.epoch);
      let identity;
      try{identity=a.identity();}catch(error){if(loading)return false;throw error;}
      if(loading&&!identity.phase&&identity.name===state.claim.name)return false;
      if(identity.name!==state.claim.name||identity.phase!=='live'||!['funded','live'].includes(identity.category))throw new Error('领取期间账户或 Live 阶段发生变化');
      return true;
    }
    function payload(action,extra={}) {
      return {action,claim_id:state.claim.id,account_id:state.claim.account_id,name:state.claim.name,
        identity:state.claim.identity,phase:'live',mode_epoch:state.epoch,...extra};
    }
    function one(selector,scope=document) {
      const found=[...scope.querySelectorAll(selector)].filter(a.visible);
      if(found.length!==1)throw new Error('领取组件不唯一或不可见：'+selector);
      return found[0];
    }
    function modal() {
      const all=dialogs();if(!all.length)return null;
      if(all.length!==1||all[0].getAttribute('role')!=='dialog')throw new Error('领取流程出现未知确认窗口');
      const title=one('[data-slot="dialog-title"]',all[0]);
      if(!/^(领取利润|Claim Profits?)$/i.test(a.text(title)))throw new Error('领取流程出现未识别窗口：'+a.text(title));
      return all[0];
    }
    function received(dialog) {
      const labels=[...dialog.querySelectorAll('p,span,div')].filter(e=>a.visible(e)&&/^(您将收到|You will receive)$/i.test(a.text(e))&&
        ![...e.children].some(c=>/^(您将收到|You will receive)$/i.test(a.text(c))));
      if(labels.length!==1)throw new Error('无法唯一识别到账预览标签');
      let row=labels[0];
      for(let i=0;i<3;i++){
        row=row.parentElement;if(!row||!dialog.contains(row))break;
        const values=[...row.querySelectorAll('p,span,div')].filter(a.visible).map(a.text)
          .filter(t=>/^\$\s*[\d,]+(?:\.\d+)?$/.test(t)).map(a.money);
        const unique=[...new Set(values.filter(v=>v!==null))];
        if(unique.length===1)return unique[0];
      }
      throw new Error('无法唯一读取实际到账预览');
    }
    function quote(dialog) {
      const recipient=[...dialog.querySelectorAll('input[data-slot="input"]')].filter(e=>a.visible(e)&&!e.getAttribute('data-testid')&&!e.name);
      if(recipient.length!==1||recipient[0].value.trim()!==''||
        !/(?:收款人|Recipient)/i.test(a.text(recipient[0].parentElement))||
        ![...recipient[0].parentElement.querySelectorAll('span,div')].some(e=>a.visible(e)&&/^(主账户|Primary Account|Main Account)$/i.test(a.text(e)))) {
        throw new Error('收款人未明确保持为主账户，禁止领取');
      }
      const available=a.money(a.text(one('[data-testid="claim-profit-available"]',dialog)));
      const value=one('input[name="amount"][data-slot="form-control"]',dialog).value;
      const amount=Number(units(value))/1e6,receive=received(dialog);
      if(!(available>0&&amount>0&&amount<=available&&receive>0&&receive<=amount))throw new Error('可领取金额、申请金额或到账预览无效');
      return {available,amount,received:receive,recipient:'main'};
    }
    function portfolioFlat() {
      const banners=[...document.querySelectorAll('div.sticky.top-14')].filter(a.visible);
      if(banners.length!==1)throw new Error('无法唯一核对持仓页的账户占用指标');
      const text=String(banners[0].innerText).replace(/\s+/g,' ').trim();
      const read=pattern=>{const matches=[...text.matchAll(new RegExp(pattern,'gi'))];
        if(matches.length!==1)throw new Error('持仓页的空仓/挂单指标不明确');return Number(matches[0][1].replace(/,/g,''));};
      const leverage=read('(?:杠杆|槓桿|Leverage)\\s*[:：]?\\s*([\\d,.]+)\\s*x');
      const position_margin=read('(?:仓位占用|倉位佔用|Position Margin|Position Utilization)\\s*[:：]?\\s*\\$?\\s*([\\d,.]+)');
      const order_margin=read('(?:订单占用|訂單佔用|Order Margin|Order Utilization)\\s*[:：]?\\s*\\$?\\s*([\\d,.]+)');
      if(leverage!==0||position_margin!==0||order_margin!==0)throw new Error('持仓页显示持仓或挂单占用，禁止领取');
      return {leverage,position_margin,order_margin,observed_at:Date.now()/1000};
    }
    function navigate(path,pattern) {
      own();if(dialogs().length)throw new Error('存在额外弹窗，禁止切换领取页面');
      const links=[...document.querySelectorAll('a[href]')].filter(e=>a.visible(e)&&e.origin===location.origin&&path.includes(e.pathname)&&pattern.test(a.text(e)));
      const link=globalThis.VestBridgeCore.sameDestinationNavigation(links);
      if(!link)throw new Error('领取流程的导航目标地址不唯一');
      a.click(link);
    }
    function edit(input,value) {
      if(!(input instanceof HTMLInputElement)||input.disabled||input.readOnly)throw new Error('领取输入框不可编辑');
      own();input.focus();
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,String(value));
      input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
    }
    return {
      get active(){return !!state;},
      heartbeat(){return state?.reserved?payload('heartbeat'):null;},
      dispose(){state=null;},
      async begin(claim,epoch) {
        if(state)throw new Error('已有领取流程尚未核验');
        state={claim,epoch,stage:'reserve',started:Date.now()};
        try{
          a.guard(epoch);await a.select(claim.name,epoch);own();
          const snapshot=await a.flat(claim.name,epoch);own();
          const reply=await a.call('profit-claim',payload('reserve',{snapshot}));own();
          if(reply.claim?.status!=='preparing'||reply.claim.mode_epoch!==epoch)throw new Error('领取任务已提交或授权变化，不重复执行');
          state.claim=reply.claim;state.reserved=true;state.stage='go-portfolio';
        }catch(error){if(error.code==='EXECUTION_CONNECTION_PENDING'&&!state?.reserved)state=null;await this.fail(error);throw error;}
      },
      async fail(error) {
        if(!state||error.code==='EXECUTION_CONNECTION_PENDING')return;
        try{await a.call('profit-claim',payload('fail',{error:error.message}));}catch{}
      },
      async run(epoch) {
        if(!state)return false;
        try{
          if(epoch!==state.epoch)throw new Error('利润领取授权轮次已变化');
          a.guard(epoch);if(Date.now()-state.started>90000)throw new Error('利润领取超过 90 秒未完成核验；禁止重试提交');
          if(!own(['portfolio','return','go-return'].includes(state.stage)))return true;
          if(state.stage==='go-portfolio'){
            navigate(['/portfolio'],/^(持仓|Portfolio|Positions)$/i);state.stage='portfolio';return true;
          }
          if(state.stage==='portfolio'){
            if(location.pathname!=='/portfolio')return true;
            if(dialogs().length)throw new Error('打开领取前出现未知弹窗');
            const button=one('button[data-testid="withdraw-open"]');
            if(button.type!=='button')throw new Error('领取入口类型变化');
            state.stage='dialog';a.click(button);return true;
          }
          if(state.stage==='dialog'){
            const dialog=modal();if(!dialog)return true;
            const policy=state.claim.policy;
            if(policy.live_payout_recipient!=='main')throw new Error('领取收款参数不是主账户');
            if(policy.live_payout_mode==='amount')edit(one('input[name="amount"]',dialog),policy.live_payout_amount);
            else edit(one('[data-testid="claim-profit-amount-slider-percentage"]',dialog),policy.live_payout_mode==='all'?100:policy.live_payout_percent);
            state.stage='preview';return true;
          }
          if(state.stage==='preview'){
            const dialog=modal();if(!dialog)throw new Error('领取预览意外关闭');
            const q=quote(dialog),expected=expectedAmount(q.available,state.claim.policy);
            if(q.amount!==expected)throw new Error('页面联动金额与策略参数不符');
            const button=one('button[data-testid="claim-profit-submit"]',dialog);
            if(button.type!=='submit'||button.disabled)throw new Error('领取提交按钮尚不可用');
            const reply=await a.call('profit-claim',payload('intent',{preview:{...q,flat:portfolioFlat()}}));
            // Reserve locally before any possible click. Losing this response
            // or changing modes cannot turn into a second financial request.
            state.stage='submitted';
            if(!reply.authorize_click)throw new Error('该领取已送达或状态不明，禁止重复点击');
            const key='profit-claim-clicks:'+state.claim.account_id;
            const history=(await chrome.storage.local.get(key))[key]||[];
            if(history.includes(state.claim.id))throw new Error('本领取已尝试，禁止重放');
            await chrome.storage.local.set({[key]:[...history.slice(-499),state.claim.id]});
            own();const fresh=modal(),proof=fresh&&quote(fresh);
            if(!proof||JSON.stringify(proof)!==JSON.stringify(q))throw new Error('提交前领取预览或收款状态变化');
            portfolioFlat();
            a.click(one('button[data-testid="claim-profit-submit"]',fresh));return true;
          }
          if(state.stage==='submitted'){
            if(modal())return true;
            state.stage='go-return';return true;
          }
          if(state.stage==='go-return'){
            navigate(['/trade','/trade/NQ-PERP','/trade/NDX-USD-PERP'],/^(交易|Trade)$/i);state.stage='return';return true;
          }
          if(state.stage==='return'){
            if(!a.isTrade(location.pathname))return true;
            if(dialogs().length)throw new Error('返回交易页存在额外确认窗口');
            const snapshot=await a.flat(state.claim.name,epoch);own();
            const reply=await a.call('profit-claim',payload('verify',{snapshot}));own();
            if(!reply.verified&&reply.claim?.status!=='verified')throw new Error('领取后余额尚未核验');
            state=null;return true;
          }
          return true;
        }catch(error){await this.fail(error);throw error;}
      },
    };
  }
  globalThis.VestProfitClaimFlow={create,expectedAmount};
})();
