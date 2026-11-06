import copy
import time
import unittest

from server.backtesting import simulate,validate_config
from server.backtest_service import BacktestService
from server.engine import ValidationError
from test_backtesting import BASE,INSTRUMENT,bar,config


class DollarSlippageTests(unittest.TestCase):
    def test_whole_order_cost_is_exact_and_independent_of_contract_quantity(self):
        quantities=[]
        for capital in (1000,5000):
            r=simulate(config(initial_capital=capital,initial_phase='live',live_profit_target=0,
                slippage_mode='usd',slippage_usd=4),[bar(),bar(BASE+60)],INSTRUMENT)
            t=r['trades'][0];quantities.append(t['quantity'])
            self.assertEqual(t['entry_price'],100)
            self.assertEqual(t['exit_price'],100)
            self.assertEqual(t['fixed_slippage_usd'],8)
            self.assertEqual(t['pnl'],-8)
            self.assertEqual(r['metrics']['total_fixed_slippage_usd'],8)
            self.assertEqual(r['accounts'][0]['balance'],capital-8)
        self.assertNotEqual(*quantities)

    def test_both_directions_pay_both_sides_without_changing_fee_rate(self):
        for side in ('buy','sell'):
            r=simulate(config(initial_capital=5000,leverage=50,size_percent=50,side=side,
                initial_phase='live',live_profit_target=0,fee_bps=.25,slippage_mode='usd',slippage_usd=4),
                [bar(open=20000,high=20000,low=20000,close=20000),bar(BASE+60,open=20000,high=20000,low=20000,close=20000)],INSTRUMENT)
            t=r['trades'][0]
            self.assertEqual(t['quantity'],3)
            self.assertEqual(t['fees'],6)
            self.assertEqual(t['pnl'],-14)
            self.assertEqual(r['accounts'][0]['balance'],4986)

    def test_phase_and_payout_targets_reserve_the_exit_dollar_cost(self):
        for side in ('buy','sell'):
            r=simulate(config(side=side,slippage_mode='usd',slippage_usd=4,payout_delay_hours=0),
                [bar(BASE+i*60,high=120,low=100) if side=='buy' else bar(BASE+i*60,high=100,low=80) for i in range(3)],INSTRUMENT)
            self.assertEqual([t['pnl'] for t in r['trades']],[10,6,4])
            self.assertEqual(r['metrics']['exam_pass_rate'],100)
            self.assertEqual(r['metrics']['total_payout'],3.2)
            self.assertEqual(r['metrics']['total_fixed_slippage_usd'],24)
        r=simulate(config(slippage_mode='usd',slippage_usd=4),[bar(high=105),bar(BASE+60,high=105)],INSTRUMENT)
        self.assertEqual(r['metrics']['passed_accounts'],0)

    def test_cash_costs_affect_risk_and_insufficient_margin_never_charges_slippage(self):
        r=simulate(config(initial_phase='live',live_profit_target=0,daily_loss_percent=1,
            slippage_mode='usd',slippage_usd=6),[bar(),bar(BASE+60)],INSTRUMENT)
        self.assertEqual(r['metrics']['failed_accounts'],1)
        self.assertEqual(r['metrics']['total_pnl'],-12)
        r=simulate(config(slippage_mode='usd',slippage_usd=251),[bar(),bar(BASE+60)],INSTRUMENT)
        self.assertEqual(r['metrics']['total_trades'],0)
        self.assertEqual(r['metrics']['total_fixed_slippage_usd'],0)
        self.assertEqual(r['metrics']['skipped_insufficient_margin'],1)

    def test_fee_free_comparison_keeps_fixed_dollar_slippage(self):
        r=simulate(config(initial_phase='live',live_profit_target=0,fee_bps=.25,
            slippage_mode='usd',slippage_usd=4),[bar(),bar(BASE+60)],INSTRUMENT)
        self.assertLess(r['metrics']['total_pnl'],-8)
        self.assertEqual(r['cash_curve_without_fees'][-1]['pnl'],-8)

    def test_invalid_dollar_costs_are_rejected(self):
        for value in (-1,True,None,float('nan'),float('inf'),1e7+1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValidationError,'每笔每边滑点 USD'):
                    validate_config(config(slippage_mode='usd',slippage_usd=value))
        self.assertEqual(validate_config(config(slippage_mode='usd',slippage_usd=0))['slippage_usd'],0)

    def test_legacy_bps_form_converts_using_a_reference_order_and_keeps_original(self):
        service=BacktestService(':memory:')
        legacy=config(initial_capital=5000,size_percent=50,leverage=50,fee_bps=.25,
            slippage_mode='bps',slippage_bps=4)
        try:
            service.store.save_chunk(INSTRUMENT,60,[bar(open=20000,high=20000,low=20000,close=20000)])
            service.store.setting('form',legacy);before=copy.deepcopy(service.store.setting('form'))
            meta=service.metadata();converted=meta['saved_form']
            self.assertEqual(converted['slippage_mode'],'usd')
            self.assertEqual(converted['slippage_usd'],48)
            self.assertIn('初始买入仓位 3 份',meta['slippage_migration'])
            self.assertEqual(service.store.setting('form'),before)
            self.assertEqual(validate_config(legacy)['slippage_mode'],'bps')
        finally:service.close()

    def test_missing_reference_leaves_dollar_input_empty_instead_of_guessing(self):
        service=BacktestService(':memory:')
        try:
            service.store.setting('form',config(slippage_mode='bps',slippage_bps=4))
            meta=service.metadata()
            self.assertIsNone(meta['saved_form']['slippage_usd'])
            self.assertIn('缺少可用换算依据',meta['slippage_migration'])
        finally:service.close()

    def test_observed_percentage_uses_quote_notional_for_dollar_conversion(self):
        service=BacktestService(':memory:')
        try:
            q=service.observe_costs(dict(symbol='NQ-PERP',fee_bps=.25,slippage_bps=.1,
                notional=40000,observed_at=time.time()))['quote']
            self.assertEqual(q['slippage_usd'],.4)
            q=service.observe_costs(dict(symbol='NQ-PERP',fee_bps=.25,slippage_bps=.1,
                observed_at=time.time()))['quote']
            self.assertNotIn('slippage_usd',q)
        finally:service.close()
