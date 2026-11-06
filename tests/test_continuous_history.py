import contextlib
import gzip
import io
import json
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from server import continuous_history as history
from server.market_history import BacktestStore


class ContinuousHistoryTests(unittest.TestCase):
    def test_ny_end_timestamps_convert_to_correct_utc_open_across_dst(self):
        self.assertEqual(history.ny_timestamp(datetime(2026,1,20,0,1))-60,int(datetime(2026,1,20,5,0,tzinfo=timezone.utc).timestamp()))
        self.assertEqual(history.ny_timestamp(datetime(2026,7,20,0,1))-60,int(datetime(2026,7,20,4,0,tzinfo=timezone.utc).timestamp()))
        with self.assertRaises(ValueError):history.ny_timestamp(datetime(2026,3,8,2,30))
        with self.assertRaises(ValueError):history.ny_timestamp(datetime(2026,11,1,1,30))

    def test_observed_volume_roll_and_additive_adjustment_preserve_real_movement(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            root=Path(tmp);cache=root/'cache.sqlite'
            with patch.object(history,'DATA_ROOT',root),contextlib.closing(history.open_source_db()) as db:
                reference=history.ny_timestamp(datetime(2026,3,10,16,59));switch=history.ny_timestamp(datetime(2026,3,10,18))
                with db:
                    opening=history.ny_timestamp(datetime(2026,3,9,18))
                    db.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',[(symbol,opening+i*60,'2026-03-09',price,price,price,price,volume,'test') for symbol,price,volume in [('MNQ 03-26',100,1),('MNQ 06-26',110,2)] for i in range(1000)])
                    db.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',[
                        ('MNQ 03-26',reference,'2026-03-09',100,100,100,100,100,'test'),
                        ('MNQ 06-26',reference,'2026-03-09',110,110,110,110,200,'test'),
                        ('MNQ 06-26',switch,'2026-03-10',110,111,110,111,200,'test')])
                with contextlib.redirect_stdout(io.StringIO()):history.build_continuous(db,cache)
                rolls=json.loads((root/'rolls.json').read_text());self.assertEqual(rolls[0]['switch_utc'],switch);self.assertEqual(rolls[0]['difference'],10)
                store=BacktestStore(cache)
                try:
                    bars=store.bars('MNQ CONTINUOUS BA',60,reference,switch)
                    self.assertEqual([b['close'] for b in bars],[110,111])
                    self.assertTrue(store.instrument('MNQ CONTINUOUS BA')['continuous'])
                finally:store.close()
                with gzip.open(root/'MNQ_CONTINUOUS_BACKADJUSTED_1m.csv.gz','rt') as stream:self.assertEqual(len(stream.readlines()),1003)

    def test_truncated_source_handoff_keeps_observed_reference_and_audit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            root=Path(tmp)
            with patch.object(history,'DATA_ROOT',root),contextlib.closing(history.open_source_db()) as db:
                reference=history.ny_timestamp(datetime(2026,3,12,23,42));last=reference+17*60
                with db:db.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',[
                    ('MNQ 03-26',reference,'2026-03-12',100,100,100,100,10,'test'),
                    ('MNQ 06-26',reference,'2026-03-12',110,110,110,110,1,'test'),
                    ('MNQ 03-26',last,'2026-03-12',100,100,100,100,10,'test'),
                    ('MNQ 06-26',last+60,'2026-03-12',111,111,111,111,10,'test')])
                with contextlib.redirect_stdout(io.StringIO()):history.build_continuous(db,root/'cache.sqlite')
                roll=json.loads((root/'rolls.json').read_text())[0]
                self.assertEqual(roll['rule'],'observed_source_coverage_handoff')
                self.assertEqual(roll['reference_age_seconds'],17*60)
                self.assertEqual(roll['switch_utc'],last+60)

    def test_missing_quarter_is_rejected_instead_of_silently_skipped(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'runtime') as tmp:
            with patch.object(history,'DATA_ROOT',Path(tmp)),contextlib.closing(history.open_source_db()) as db:
                with db:db.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',[(symbol,1,'2026-03-01',100,100,100,100,1,'test') for symbol in ('MNQ 03-26','MNQ 09-26')])
                with self.assertRaisesRegex(ValueError,'季度合约缺失'):history.build_continuous(db,Path(tmp)/'cache.sqlite')
