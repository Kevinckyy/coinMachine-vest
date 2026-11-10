import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(new URL('../extension/profit-claim.js',import.meta.url),'utf8'),context);
const amount=context.VestProfitClaimFlow.expectedAmount;
test('claim policies preserve micro-USDC precision and round percentage claims down',()=>{
  assert.equal(amount(40.564943,{live_payout_mode:'all'}),40.564943);
  assert.equal(amount(40.564943,{live_payout_mode:'percent',live_payout_percent:50}),20.282471);
  assert.equal(amount(40.564943,{live_payout_mode:'amount',live_payout_amount:10}),10);
  assert.equal(amount(5000,{live_payout_mode:'percent',live_payout_percent:1e-7}),.000005);
  assert.equal(amount(.000001,{live_payout_mode:'percent',live_payout_percent:50}),0);
});
test('claim amount calculations reject an amount that exceeds supported precision',()=>{
  assert.throws(()=>amount(40.1234567,{live_payout_mode:'all'}));
  assert.throws(()=>amount(40,{live_payout_mode:'amount',live_payout_amount:.0000001}));
});
