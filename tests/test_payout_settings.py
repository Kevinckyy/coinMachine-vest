import copy
import tempfile
import unittest
from pathlib import Path

from server.engine import Engine, STRATEGY_FORM_DEFAULTS, ValidationError


class PayoutSettingsTests(unittest.TestCase):
    def test_defaults_and_updates_only_save_preferences(self):
        e=Engine(':memory:',seed=False)
        try:
            cfg=e.state()['preferences']['strategy_config']
            self.assertEqual((cfg['live_payout_recipient'],cfg['live_payout_mode'],cfg['live_payout_percent']),('main','all',100))
            before=copy.deepcopy({k:e.data[k] for k in ('strategy','positions','jobs','history','execution_sessions')})
            cfg=e.save_strategy_config(dict(live_payout_mode='amount',live_payout_amount=12.345678))
            self.assertEqual(cfg['live_payout_amount'],12.345678)
            cfg=e.save_strategy_config(dict(live_payout_mode='percent',live_payout_percent=37.5))
            self.assertEqual(cfg['live_payout_percent'],37.5)
            self.assertEqual({k:e.data[k] for k in before},before)
        finally:e.close()

    def test_settings_and_legacy_defaults_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'payout.sqlite'
            e=Engine(db,seed=False)
            try:
                # A database written before payout settings were introduced.
                for key in list(e.data['preferences']['strategy_config']):
                    if key.startswith('live_payout_'):e.data['preferences']['strategy_config'].pop(key)
                e._save()
            finally:e.close()
            e=Engine(db,seed=False)
            try:
                for key in STRATEGY_FORM_DEFAULTS:
                    if key.startswith('live_payout_'):self.assertEqual(e.data['preferences']['strategy_config'][key],STRATEGY_FORM_DEFAULTS[key])
                e.save_strategy_config(dict(live_payout_mode='percent',live_payout_percent=66.25,live_payout_amount=10))
            finally:e.close()
            e=Engine(db,seed=False)
            try:
                cfg=e.state()['preferences']['strategy_config']
                self.assertEqual((cfg['live_payout_mode'],cfg['live_payout_percent'],cfg['live_payout_amount']),('percent',66.25,10))
                self.assertFalse(e.data['jobs']);self.assertFalse(e.data['positions'])
            finally:e.close()

    def test_invalid_values_and_non_primary_recipients_are_rejected_atomically(self):
        e=Engine(':memory:',seed=False)
        try:
            before=copy.deepcopy(e.data['preferences']['strategy_config'])
            invalid=[{'live_payout_recipient':x} for x in ('external','Account 41','',None,True)]
            invalid += [dict(live_payout_mode=x) for x in ('withdraw','',None,True)]
            invalid += [dict(live_payout_amount=x) for x in (0,-1,True,float('nan'),float('inf'),'bad',.0000001,1.1234567)]
            invalid += [dict(live_payout_percent=x) for x in (0,-1,100.01,True,float('nan'),float('inf'),'bad')]
            for patch in invalid:
                with self.subTest(patch=patch),self.assertRaises(ValidationError):e.save_strategy_config(patch)
                self.assertEqual(e.data['preferences']['strategy_config'],before)
        finally:e.close()

    def test_strategy_carries_saved_payout_policy_without_creating_fund_actions(self):
        e=Engine(':memory:',seed=False)
        try:
            a=e.add_account(dict(name='Exam',initial_balance=5000,profit_target=250,max_loss=300))
            e.save_strategy_config(dict(live_payout_mode='percent',live_payout_percent=25))
            run=e.start_strategy(dict(account_ids=[a['id']],batch_size=1,size_percent=25,stop_loss=130,
                order_delay_min=0,order_delay_max=0,batch_delay_min=0,batch_delay_max=0))
            self.assertEqual((run['config']['live_payout_mode'],run['config']['live_payout_percent']),('percent',25))
            self.assertEqual(run['config']['live_payout_recipient'],'main')
            self.assertFalse(e.data['jobs']);self.assertFalse(e.data['positions'])
        finally:e.close()
