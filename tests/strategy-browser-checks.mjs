import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import path from 'node:path';

export async function runStrategyChecks({base,root,run,command,evaluate,clickText,until,sleep,restartServer}) {
  const page=(fn,arg)=>evaluate('('+fn.toString()+')('+JSON.stringify(arg)+')');
  const value=label=>page(label=>document.querySelector('[aria-label='+CSS.escape(label)+']').value,label);
  const edit=values=>page(values=>{
    for(const [label,value] of Object.entries(values)) {
      const el=document.querySelector('[aria-label='+CSS.escape(label)+']');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,String(value));
      el.dispatchEvent(new Event('input',{bubbles:true}));
    }
  },values);
  const profile=async()=> (await (await fetch(base+'/api/state')).json()).preferences.strategy_config;
  const visit=async()=>{
    await command('Page.navigate',{url:base});
    await until(()=>evaluate('document.querySelector(\'h1\')?.textContent.includes(\'交易工作台\')'),'settings app');
    await clickText('自动策略','aside button');
    await until(()=>evaluate('document.querySelector(\'#strategy-form fieldset\')?.disabled===false && document.querySelector(\'.strategy-save-status\')?.textContent.includes(\'已自动保存\')'),'form hydration');
  };
  const compatibility=await command('Page.addScriptToEvaluateOnNewDocument',{source:'('+(()=>{
    const realFetch=window.fetch.bind(window);
    window.fetch=async(...args)=>{
      const response=await realFetch(...args);
      if(String(args[0])!=='/api/state' || !response.ok)return response;
      const state=await response.json();delete state.preferences.strategy_config;
      return new Response(JSON.stringify(state),{status:response.status,headers:response.headers});
    };
  }).toString()+')()'});
  await command('Page.navigate',{url:base});
  await until(()=>evaluate('document.querySelector(\'h1\')?.textContent.includes(\'交易工作台\')'),'old-server compatibility page');
  await clickText('自动策略','aside button');
  await until(()=>evaluate('document.querySelector(\'.strategy-save-status\')?.textContent.includes(\'重启本地服务\')'),'old server requires upgrade');
  assert.equal(await evaluate('document.querySelector(\'#strategy-form fieldset\').disabled'),true);
  assert.equal(await evaluate('document.querySelector(\'#strategy-form button[type=submit]\').disabled'),true);
  await command('Page.removeScriptToEvaluateOnNewDocument',{identifier:compatibility.identifier});
  await visit();
  assert.ok(await evaluate(`document.querySelector('.strategy-rule').textContent.includes('美东 20:00')`));
  assert.ok(await evaluate(`document.querySelector('.strategy-summary').textContent.includes('Live 优先')`));
  assert.equal(await value('剩余最大回撤阈值'),'200');
  assert.equal(await evaluate('document.querySelector(\'.strategy-stop-toggle input\').checked'),false);
  await edit({'每批账户数量':4,'策略止损金额':177.5,'账户下单最大延迟秒':21,'账户下单最小延迟秒':9,'批次最大间隔秒':71,'批次最小间隔秒':31});
  await page(()=>{
    document.querySelector('[aria-label='+CSS.escape('仓位大小 75%')+']').click();
    const el=document.querySelector('[aria-label='+CSS.escape('策略下单方向')+']');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(el,'sell');
    el.dispatchEvent(new Event('change',{bubbles:true}));
    document.querySelector('.strategy-stop-toggle input').click();
  });
  await until(()=>evaluate('document.querySelector(\'.strategy-stop-rule input[type=number]\').disabled===false'),'threshold enabled');
  await edit({'剩余最大回撤阈值':222.5});
  const expected={batch_size:4,stop_loss:177.5,size_percent:75,side:'sell',order_delay_min:9,order_delay_max:21,
    batch_delay_min:31,batch_delay_max:71,skip_stop_loss_on_low_drawdown:true,drawdown_stop_loss_threshold:222.5};
  await until(async()=>{const p=await profile();return Object.entries(expected).every(([key,val])=>p[key]===val);},'durable parameter save');
  assert.equal((await (await fetch(base+'/api/state')).json()).strategy.status,'idle');
  await visit();
  for(const [label,val] of Object.entries({'每批账户数量':'4','策略止损金额':'177.5','账户下单最小延迟秒':'9','账户下单最大延迟秒':'21','批次最小间隔秒':'31','批次最大间隔秒':'71','剩余最大回撤阈值':'222.5'})) assert.equal(await value(label),val);
  assert.equal(await evaluate('document.querySelector(\'.strategy-stop-toggle input\').checked'),true);
  assert.equal(await page(()=>document.querySelector('[aria-label='+CSS.escape('仓位大小 75%')+']').getAttribute('aria-pressed')),'true');
  await page(()=>{
    const el=document.querySelector('[aria-label='+CSS.escape('策略止损金额')+']');
    for(const val of ['178','179','180']) {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,val);
      el.dispatchEvent(new Event('input',{bubbles:true}));
    }
  });
  await until(async()=> (await profile()).stop_loss===180,'latest rapid edit wins');
  await fetch(base+'/api/strategy/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stop_loss:181})});
  await sleep(2200);
  assert.equal(await value('策略止损金额'),'180','polling preserves the current edit');
  assert.equal((await profile()).stop_loss,181,'polling cannot write an older form');
  await edit({'剩余最大回撤阈值':0});
  await until(()=>evaluate('document.querySelector(\'.strategy-save-status\').textContent.includes(\'参数未保存\')'),'invalid settings feedback');
  assert.equal((await profile()).drawdown_stop_loss_threshold,222.5);
  await edit({'剩余最大回撤阈值':223});
  await until(async()=> (await profile()).drawdown_stop_loss_threshold===223,'corrected settings saved');
  await restartServer();
  await visit();
  assert.equal(await value('策略止损金额'),'180');
  assert.equal(await value('剩余最大回撤阈值'),'223');
  assert.equal((await (await fetch(base+'/api/state')).json()).strategy.status,'idle');
  const screenshot=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  const screenshotPath=path.join(run,'strategy-settings.png');
  await writeFile(screenshotPath,Buffer.from(screenshot.data,'base64'));
  const settings={ok:true,checks:['unstarted form auto-save','all execution parameters and stop-rule values','refresh restores values',
    'latest rapid edit wins','polling preserves edits','invalid settings preserve saved values','service restart restores values without starting strategy','old server cannot silently ignore the new rule'],screenshot:screenshotPath};
  await command('Page.navigate',{url:base+'/api/health'});
  await until(()=>evaluate('document.readyState===\'complete\' && location.pathname===\'/api/health\''),'stop-rule fixture page');
  const fixture=await readFile(path.join(root,'tests','bridge-fixture.html'),'utf8');
  await page(html=>{document.open();document.write(html);document.close();},fixture.replace(/<script>[\s\S]*?<\/script>/,''));
  await evaluate(fixture.match(/<script>([\s\S]*?)<\/script>/)[1]);
  await evaluate(await readFile(path.join(root,'extension','core.js'),'utf8'));
  await evaluate(await readFile(path.join(root,'extension','content.js'),'utf8'));
  const stopRule=await evaluate('runStopRuleFixtureTests()',60000);
  assert.equal(stopRule.failed,0,JSON.stringify(stopRule));
  return{settings,stopRule};
}
