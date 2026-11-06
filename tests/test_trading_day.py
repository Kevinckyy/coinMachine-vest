import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from server.engine import Engine, ValidationError
from server.trading_day import trading_day_window


def stamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


class TradingDayTests(unittest.TestCase):
    def test_17_new_york_cutoff_in_winter_and_summer(self):
        cases = [
            ('2026-01-05T21:59:59Z','2026-01-04T22:00:00Z','2026-01-05T22:00:00Z'),
            ('2026-01-05T22:00:00Z','2026-01-05T22:00:00Z','2026-01-06T22:00:00Z'),
            ('2026-07-06T20:59:59Z','2026-07-05T21:00:00Z','2026-07-06T21:00:00Z'),
            ('2026-07-06T21:00:00Z','2026-07-06T21:00:00Z','2026-07-07T21:00:00Z'),
            ('2026-11-04T00:00:00Z','2026-11-03T22:00:00Z','2026-11-04T22:00:00Z'),
        ]
        for now,start,end in cases:
            with self.subTest(now=now):
                window=trading_day_window(stamp(now))
                self.assertEqual(window['start'],stamp(start))
                self.assertEqual(window['end'],stamp(end))
                self.assertEqual(window['cutoff'],'17:00')

    def test_dst_transition_windows_have_23_and_25_hours_including_windows_fallback(self):
        for now,start,end,hours in [
            ('2026-03-08T12:00:00Z','2026-03-07T22:00:00Z','2026-03-08T21:00:00Z',23),
            ('2026-11-01T12:00:00Z','2026-10-31T21:00:00Z','2026-11-01T22:00:00Z',25),
        ]:
            with self.subTest(now=now):
                normal=trading_day_window(stamp(now))
                with patch('server.trading_day.NEW_YORK',None):
                    fallback=trading_day_window(stamp(now))
                self.assertEqual(normal,fallback)
                self.assertEqual(fallback['start'],stamp(start))
                self.assertEqual(fallback['end'],stamp(end))
                self.assertEqual((fallback['end']-fallback['start'])/3600,hours)

    def test_daily_realized_uses_history_account_mode_and_close_time_not_cash_change(self):
        now=stamp('2026-11-04T20:00:00Z')
        engine=Engine(':memory:',seed=False,clock=lambda:now)
        try:
            first=engine.add_account({'name':'First','initial_balance':1000,'profit_target':100,'max_loss':50})['id']
            second=engine.add_account({'name':'Second','initial_balance':1000,'profit_target':100,'max_loss':50})['id']
            engine.data['accounts'][first]['realized_pnl']=999
            start=trading_day_window(now)['start']
            entries=[(first,'paper',start-1,100),(first,'paper',start,49.5),
                     (first,'paper',now-1,-12.3),(first,'paper',now+1,888),
                     (first,'browser',now-2,777),(second,'paper',now-2,-7)]
            engine.data['history']=[{'id':str(i),'account_id':aid,'mode':mode,'closed_at':closed,'pnl':pnl}
                for i,(aid,mode,closed,pnl) in enumerate(entries)]
            accounts={a['id']:a for a in engine.state()['accounts']}
            self.assertEqual(accounts[first]['daily_realized_pnl'],37.2)
            self.assertEqual(accounts[first]['daily_trade_count'],2)
            self.assertEqual(accounts[second]['daily_realized_pnl'],-7)
            now=stamp('2026-11-04T22:00:00Z')
            self.assertEqual(engine.state()['accounts'][0]['daily_realized_pnl'],0)
        finally:
            engine.close()

    def test_expired_auto_account_keeps_money_status_and_timestamp_but_cannot_execute(self):
        now=stamp('2026-11-04T20:00:00Z')
        engine=Engine(':memory:',seed=False,clock=lambda:now)
        try:
            row={'name':'Cached','category':'eval','equity':512.25,'balance':500,
                 'unrealized_pnl':12.25,'current':True,'observed_at':now}
            account=engine.bridge_session({'session_id':'cache-test-session','mode':'readonly','mode_epoch':0,
                'tab_url':'https://next.vestmarkets.com/trade/NQ-PERP','accounts':[row]})['accounts'][0]
            engine.settings({'mode':'browser'})
            engine.strategy_lock_account(account['id'],True)
            now+=40
            saved=engine.state()['accounts'][0]
            self.assertEqual((saved['balance'],saved['equity'],saved['unrealized_pnl']),(500,512.25,12.25))
            self.assertEqual(saved['observed_at'],row['observed_at'])
            self.assertEqual(saved['status'],'active')
            self.assertTrue(saved['strategy_locked'])
            self.assertTrue(saved['data_stale'])
            self.assertTrue(saved['update_pending'])
            self.assertFalse(saved['executable'])
            self.assertFalse(saved['snapshot_ready'])
            with self.assertRaises(ValidationError):
                engine.place_orders({'account_ids':[account['id']],'quantity':1,'side':'buy','symbol':'NQ-PERP',
                    'take_profit':10,'stop_loss':10,'idempotency_key':'cannot-use-cache'})
        finally:
            engine.close()
