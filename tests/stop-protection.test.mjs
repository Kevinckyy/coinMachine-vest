import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const ctx=vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../extension/stop-protection.js',import.meta.url),'utf8'),ctx);
const {plan}=ctx.VestStopProtection;
test('actual entry, canonical quantity and native stop determine risk for either side',()=>{
  for(const side of ['buy','sell']){
    const sign=side==='buy'?-1:1;
    const fill={side,entry_price:28000,quantity:4,native_sl_price:28000+sign*35,price:27900};
    assert.equal(plan(fill,130).actual_risk,140);
    assert.equal(plan(fill,130).target_price,28000+sign*32.5);
    assert.equal(plan({...fill,native_sl_price:28000+sign*32.5},130).status,'verified');
  }
});
test('unrepresentable risk rounds toward entry and explicit omitted stops stay omitted',()=>{
  for(const side of ['buy','sell']){
    const fill={side,entry_price:28000,quantity:3};
    const p=plan(fill,130),actual=plan({...fill,native_sl_price:p.target_price},130);
    assert.equal(actual.status,'verified');assert.ok(actual.actual_risk<=130&&actual.actual_risk>129.97);
  }
  assert.equal(plan({native_sl_absent:true},null).status,'omitted');
  assert.equal(plan({native_sl_price:100},null).status,'mismatch');
  assert.throws(()=>plan({side:'buy',quantity:0,entry_price:100},130));
});
