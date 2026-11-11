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
test('NQ stops use the nearest quarter point, with exact midpoints rounded toward entry',()=>{
  for(const side of ['buy','sell']){
    const sign = side === 'sell' ? 1 : -1;
    for (const [quantity, target, distance, risk] of [[3, 130, 43.25, 129.75], [6, 130, 21.75, 130.5], [4, 130.5, 32.5, 130]]) {
      const price = 28000 + sign * distance;
      const fill = {side, entry_price: 28000, quantity, native_sl_price: price};
      const result = plan(fill, target);
      assert.equal(result.target_price, price);
      assert.equal(result.actual_risk, risk);
      assert.equal(result.status, 'verified');
      assert.equal(plan({...fill, native_sl_price: price + sign * 0.01}, target).status, 'mismatch');
    }
  }
});
test('explicitly omitted stops stay omitted',()=>{
  assert.equal(plan({native_sl_absent:true},null).status,'omitted');
  assert.equal(plan({native_sl_price:100},null).status,'mismatch');
  assert.throws(()=>plan({side:'buy',quantity:0,entry_price:100},130));
});
