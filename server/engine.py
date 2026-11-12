"""Durable paper trading and a fail-closed, user-controlled browser bridge.

No private Vest endpoints or user credentials are used by this service.
All public methods serialize under a single reentrant lock; SQLite commits each
mutation atomically. A delivered browser action is never delivered a second time.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import random
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from .trading_day import trading_day_window,entry_day_window
from .phases import PhaseAccounting, PHASES, phase_rank
from .strategies import catalogue,require_strategy,choose_live_first,choose_batch_side
from .profit_claims import ProfitClaims
from .stop_protection import stop_plan

NQ_TRADE_PATHS = {'/trade/NQ-PERP','/trade/NDX-USD-PERP'}
STRATEGY_FORM_DEFAULTS = dict(strategy_id='random_entry',batch_size=2, order_delay_min=2, order_delay_max=8,
    batch_delay_min=15, batch_delay_max=45, size_percent=25, take_profit=None,
    stop_loss=130, side='random', symbol='NQ-PERP',
    skip_stop_loss_on_low_drawdown=False, drawdown_stop_loss_threshold=200,
    live_default_locked=True, live_side='random', live_size_percent=25,
    live_take_profit=None, live_stop_loss=130,
    live_skip_stop_loss_on_low_drawdown=False, live_drawdown_stop_loss_threshold=200,
    live_payout_recipient='main',live_payout_mode='all',live_payout_amount=1,live_payout_percent=100)

class ValidationError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def positive(value, label, maximum=1_000_000_000):
    if isinstance(value, bool):
        raise ValidationError(f"{label}必须是正数")
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValidationError(f"{label}必须是正数") from None
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise ValidationError(f"{label}必须在 0 到 {maximum:g} 之间")
    return value


def nonnegative(value, label, maximum=86400):
    if isinstance(value, bool):
        raise ValidationError(f"{label}必须是非负数")
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValidationError(f"{label}必须是非负数") from None
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValidationError(f"{label}必须在 0 到 {maximum:g} 之间")
    return value


def observed_time(value):
    try:
        if isinstance(value, str) and not value.replace('.', '', 1).isdigit():
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        value = float(value)
        return value / 1000 if value > 1e12 else value
    except (ValueError, TypeError, OverflowError):
        return 0


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def execution_excluded(account):
    """Primary accounts are display-only, independently of daily strategy locks."""
    name = ' '.join(str(account.get('name', '')).split()).casefold()
    category = str(account.get('category', '')).strip().casefold()
    return bool(account.get('execution_excluded') or category in {'default','main','primary','master'}
                or name in {'主账户','主账号','主帳戶','主帳號','main account','primary account','master account'})


class Engine(PhaseAccounting, ProfitClaims):
    ACTIVE = {'running', 'waiting'}
    UNRESOLVED = {'queued', 'delivered', 'unknown'}
    CAPITAL_METRICS = ('daily_loss','drawdown','goal','health')
    MIN_ACCOUNT_HEALTH = 25

    def __init__(self, db_path='runtime/vest.sqlite', *, seed=True, clock=None, rng=None):
        self.db_path=str(db_path)
        self.clock = clock or time.time
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        if db_path != ':memory:':
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        row = self.db.execute("SELECT value FROM state WHERE key='engine'").fetchone()
        if row:
            self.data = json.loads(row[0])
            self.data.setdefault('browser_candles', [])
            self.data.setdefault('browser_market', None)
            self.data.setdefault('audit', [])
            self.data.setdefault('read_only_observation', {'last_seen':0,'error':'','accounts':[]})
            self.data.setdefault('execution_sessions', {})
            for execution in self.data['execution_sessions'].values():
                execution['mode'] = 'readonly'
                execution['last_seen'] = 0
            # Persist the last DOM snapshot, never a live connection. A browser
            # must send a new observation after every service restart.
            self.data['read_only_observation']['last_seen'] = 0
            self.data['bridge_sessions'] = {}
            for account in self.data['accounts'].values():
                if account['mode']=='browser' and not account.get('auto_discovered'):
                    account['legacy'] = True
                    account['enabled'] = False
            # A process restart can never silently resume submitting orders.
            if self.data['strategy']['status'] in self.ACTIVE:
                self.data['strategy']['status'] = 'paused'
                self.data['strategy']['next_action_at'] = None
                self._log('warning', '服务已重启；策略暂停，已有持仓继续受保护，请人工确认后重新启动。')
            for job in self.data['jobs'].values():
                if job['status'] == 'queued':
                    job['status'] = 'cancelled'
                elif job['status'] == 'delivered':
                    job['status'] = 'unknown'
                    self._freeze(job['account_id'], '服务重启时浏览器操作结果未知，请到 Vest 核对。')
        else:
            self.data = self._initial(seed)
        self.data.setdefault('preferences',{}).setdefault('auto_archive_failed',True)
        self._init_profit_claims()
        self.data['strategy']['upcoming_account_ids'] = []
        self.data['strategy']['preparation'] = {'status':'inactive','accounts':{}}
        self._deduplicate_browser_accounts()
        if 'strategy_config' not in self.data['preferences']:
            legacy = {k:v for k,v in self.data['strategy'].get('config',{}).items() if k in STRATEGY_FORM_DEFAULTS}
            legacy['take_profit'] = None
            try:
                self.data['preferences']['strategy_config'] = self._strategy_form_config(legacy)
            except ValidationError:
                self.data['preferences']['strategy_config'] = copy.deepcopy(STRATEGY_FORM_DEFAULTS)
        self.data['preferences']['strategy_config']=self._strategy_form_config(self.data['preferences']['strategy_config'])
        for account in self.data['accounts'].values():
            if account.get('merged_into'):
                continue
            self._strategy_lock_defaults(account)
            self._exclude_primary_account(account)
            self._enforce_retired_failure(account)
            self._enforce_archived_account(account)
            if account.get('retired_failed') and 'archived' not in account:
                account.update({'archived':True,'archived_at':account.get('failure_confirmed_at',self.clock()),
                                'archive_source':account.get('failure_confirmation_source','user')})
            if account.get('status') == 'passed' or account.get('category') == 'passed':
                self._lock_strategy(account, '评估阶段已通过', 'evaluation_passed')
        self._roll_strategy_locks()
        token_row = self.db.execute("SELECT value FROM state WHERE key='bridge_token'").fetchone()
        self.bridge_token = token_row[0] if token_row else secrets.token_urlsafe(32)
        self.db.execute("INSERT OR REPLACE INTO state VALUES ('bridge_token', ?)", (self.bridge_token,))
        self._save()

    def _initial(self, seed):
        now = self.clock()
        candles = []
        prng = random.Random(91)
        price = 23560.0
        for i in range(240):
            opening = price
            price = round(opening + prng.gauss(0.3, 9), 2)
            candles.append({'time': int(now // 60) * 60 - (239-i)*60, 'open': opening,
                            'high': round(max(opening, price)+prng.uniform(1, 9), 2),
                            'low': round(min(opening, price)-prng.uniform(1, 9), 2),
                            'close': price, 'volume': prng.randint(80, 900)})
        data = {'mode': 'paper', 'accounts': {}, 'positions': {}, 'history': [],
                'browser_candles': [], 'browser_market': None,
                'audit': [],
                'read_only_observation': {'last_seen':0,'error':'','accounts':[]},
                'jobs': {}, 'idempotency': {}, 'bridge_sessions': {}, 'candles': candles,
                'execution_sessions': {},
                'market': {'symbol': 'NQ-PERP', 'price': price, 'change': round(price-23560, 2),
                           'change_pct': round((price/23560-1)*100, 2),
                           'high': max(x['high'] for x in candles), 'low': min(x['low'] for x in candles),
                           'volume': sum(x['volume'] for x in candles), 'source': 'paper',
                           'point_value': 1, 'updated_at': now},
                'strategy': {'status': 'idle', 'batch_number': 0, 'batch_account_ids': [],
                             'completed_accounts': [], 'remaining_accounts': [],
                             'next_action_at': None, 'config': {}, 'phase': 'idle', 'order_queue': []},
                'logs': [], 'created_at': now}
        if seed:
            for i in range(8):
                aid = f'paper-{i+1:03d}'
                initial = 25000.0 if i < 5 else 50000.0
                data['accounts'][aid] = {'id': aid, 'name': f'Vest {i+1:02d}',
                    'balance': initial, 'initial_balance': initial, 'profit_target': initial*.08,
                    'max_loss': initial*.05, 'realized_pnl': 0, 'unrealized_pnl': 0,
                    'equity': initial, 'status': 'active', 'enabled': True, 'mode': 'paper',
                    'created_at': now, 'lock_reason': None}
                for day in range(14):
                    pnl = round(prng.gauss(39 if i % 3 else -2, 78), 2)
                    close = now - (14-day)*86400 + i*300
                    entry = round(23200 + prng.uniform(-100, 500), 2)
                    side = 'buy' if (i+day) % 2 else 'sell'
                    data['history'].append({'id': uuid.uuid4().hex, 'account_id': aid,
                        'account_name': f'Vest {i+1:02d}', 'symbol': 'NQ-PERP', 'side': side,
                        'quantity': 2, 'entry_price': entry,
                        'exit_price': round(entry + pnl/2*(1 if side=='buy' else -1), 2),
                        'pnl': pnl, 'reason': 'take_profit' if pnl >= 0 else 'stop_loss',
                        'opened_at': close-600, 'closed_at': close, 'mode': 'paper', 'source': 'demo'})
                    account = data['accounts'][aid]
                    account['balance'] = round(account['balance']+pnl, 2)
                    account['realized_pnl'] = round(account['balance']-initial, 2)
                    account['equity'] = account['balance']
        return data

    def _save(self):
        for account in self.data['accounts'].values():
            if not account.get('merged_into'):
                self._sync_phase_book(account)
        self.db.execute("INSERT OR REPLACE INTO state VALUES ('engine', ?)",
                        (json.dumps(self.data, ensure_ascii=False, allow_nan=False),))
        self.db.commit()

    def _log(self, level, message):
        self.data['logs'].append({'id': uuid.uuid4().hex, 'time': self.clock(), 'level': level, 'message': message})
        self.data['logs'] = self.data['logs'][-200:]

    def _freeze(self, aid, reason):
        account = self.data['accounts'].get(aid)
        already_logged = account and account.get('lock_reason') == reason
        if account:
            account['status'] = 'locked'
            account['lock_reason'] = reason
        strategy = self.data['strategy']
        if strategy['status'] in self.ACTIVE:
            strategy['status'] = 'paused'
            strategy['next_action_at'] = None
        for pending in self.data['jobs'].values():
            same_scope = not account or not account.get('auto_discovered') or pending.get('session_id') == account['session_id']
            if same_scope and pending['type'] == 'open' and pending['status'] == 'queued':
                pending['status'] = 'cancelled'
        if account and account.get('auto_discovered'):
            execution = self.data['execution_sessions'].get(account['session_id'])
            if execution:
                self._revoke_execution(execution,reason)
        if not already_logged:
            self._log('error', reason)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name='vest-engine')
            self.thread.start()

    def _run(self):
        while not self.stop_event.wait(0.5):
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    self.data['strategy']['status'] = 'paused'
                    self.data['strategy']['next_action_at'] = None
                    self._log('error', f'交易引擎暂停：{type(exc).__name__}')
                    self._save()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(3)
        with self.lock:
            self._save()
            self.db.close()

    def _account(self, aid):
        if not isinstance(aid, str) or aid not in self.data['accounts']:
            raise ValidationError('账户不存在', 404)
        if self.data['accounts'][aid].get('merged_into'):
            raise ValidationError('该账户记录已归并，请刷新账户列表',409)
        return self.data['accounts'][aid]

    @staticmethod
    def _account_scope(sid):
        """Diagnostic browser scope only; account identity is the full name."""
        scope = sid
        parts = str(sid).split(':')
        if len(parts)==2:
            try:
                if all(str(uuid.UUID(part))==part.lower() for part in parts):
                    scope = parts[0].lower()
            except (ValueError,AttributeError):
                pass
        return hashlib.sha256(str(scope).encode()).hexdigest()[:24]

    def _deduplicate_browser_accounts(self):
        """Merge equal full names globally; retain originals and trading history."""
        groups = {}
        aliases = {}
        for account in self.data['accounts'].values():
            if not account.get('auto_discovered') or account.get('merged_into'):
                continue
            scope = self._account_scope(account.get('session_id',''))
            account['account_scope'] = scope
            groups.setdefault(account['name'],[]).append(account)
        for name, group in groups.items():
            if len(group)<2:
                continue
            group.sort(key=lambda a:(a.get('created_at',0),a['id']))
            protected = [a for a in group if self._position(a['id']) or self._pending(a['id'])]
            canonical = protected[0] if len(protected)==1 else group[0]
            latest = max(group,key=lambda a:(a.get('observed_at') or 0,a.get('created_at',0)))
            phased=[a for a in group if a.get('phase') in PHASES]
            finance_group=group
            if phased:
                rank=max(phase_rank(a['phase']) for a in phased)
                finance_group=[a for a in phased if phase_rank(a['phase'])==rank]
                latest=max(finance_group,key=lambda a:(a.get('phase_observed_at') or 0,a.get('observed_at') or 0))
                if canonical in protected and canonical.get('phase') and canonical['phase']!=latest['phase']:
                    canonical['phase_pending']={'phase':latest['phase'],'observed_at':latest.get('phase_observed_at'),
                                                'reason':'合并记录待当前页确认新阶段空仓'}
                    finance_group=[a for a in phased if a['phase']==canonical['phase']]
                    latest=canonical
                # Copy the new phase as a unit; absent values never inherit an
                # older phase's cash baseline or platform risk limits.
                if canonical.get('phase')!=latest['phase']:
                    canonical.update({k:copy.deepcopy(latest.get(k)) for k in self.PHASE_FIELDS})
                    if canonical.get('strategy_lock_source')!='manual':
                        canonical.update({k:copy.deepcopy(latest.get(k)) for k in
                            ('strategy_locked','strategy_lock_reason','strategy_lock_source','strategy_locked_at')})
                canonical.update({k:copy.deepcopy(latest.get(k)) for k in ('phase','phase_started_at','phase_observed_at')})
                merged={}
                for item in group:
                    for phase,book in item.get('phase_books',{}).items():
                        if phase not in merged or (book.get('observed_at') or 0)>=(merged[phase].get('observed_at') or 0):
                            merged[phase]=copy.deepcopy(book)
                canonical['phase_books']=merged
                scopes=[a['legacy_phase_scope'] for a in group if a.get('legacy_phase_scope') in PHASES]
                if scopes:canonical['legacy_phase_scope']=min(scopes,key=phase_rank)
            owner = protected[0] if len(protected)==1 else latest
            canonical.update({k:latest.get(k) for k in ('identity','category','available','external_position')})
            canonical.update({'session_id':owner.get('session_id'),'account_scope':self._account_scope(owner.get('session_id',''))})
            known = [a for a in finance_group if a.get('initial_balance') is not None]
            if known:
                canonical['initial_balance'] = known[0]['initial_balance']
            readings = [a for a in finance_group if a.get('observed_at') is not None
                and all(number(a.get(k)) is not None for k in ('balance','equity','unrealized_pnl'))]
            if readings:
                reading = max(readings,key=lambda a:(a['observed_at'],a.get('created_at',0)))
                canonical.update({k:reading.get(k) for k in ('balance','equity','unrealized_pnl','observed_at')})
            for key in self.CAPITAL_METRICS:
                metrics = [a.get('capital_metrics',{}).get(key) for a in finance_group]
                metrics = [m for m in metrics if isinstance(m,dict) and number(m.get('observed_at')) is not None]
                if metrics:
                    canonical.setdefault('capital_metrics',{})[key] = copy.deepcopy(max(metrics,key=lambda m:m['observed_at']))
            if canonical.get('balance') is not None and canonical.get('initial_balance') is not None:
                canonical['realized_pnl'] = round(canonical['balance']-canonical['initial_balance'],2)
            canonical['enabled'] = all(a.get('enabled',True) for a in group)
            health_unlock_at = max((a.get('health_unlock_at') or 0) for a in group)
            if health_unlock_at:
                canonical['health_unlock_at'] = health_unlock_at
            failed=next((a for a in group if a.get('retired_failed')),None)
            if failed:
                canonical.update({k:failed.get(k) for k in ('retired_failed','failure_confirmed_at','failure_confirmation_source','failure_evidence','terminal_status')})
            archived=next((a for a in group if a.get('archived')),None)
            if archived:
                canonical.update({k:archived.get(k) for k in ('archived','archived_at','archive_source','archive_kind')})
            if any(execution_excluded(a) for a in group):
                canonical['execution_excluded'] = True
            for field in ('profit_target','max_loss'):
                values = [a[field] for a in finance_group if a.get(field) is not None]
                if values:
                    canonical[field] = min(values)
            risky = next((a for a in finance_group if a.get('lock_reason') or a.get('status')=='locked'),None)
            if risky:
                canonical.update({'status':'locked','lock_reason':risky.get('lock_reason')})
            elif any(a.get('status')=='passed' for a in finance_group):
                canonical['status']='passed'
            if len(protected)>1:
                canonical.update({'dedup_conflict':True,'ambiguous':True,'status':'locked',
                    'lock_reason':'同名账户存在多个持仓或待确认操作，记录已合并，请人工核对'})
            locked = [a for a in group if a.get('strategy_locked') and (a in finance_group or a.get('strategy_lock_source')=='manual')]
            if locked:
                chosen = max(locked,key=lambda a:(a.get('strategy_lock_source')=='evaluation_passed',a.get('strategy_locked_at') or 0))
                canonical.update({k:chosen.get(k) for k in ('strategy_locked','strategy_lock_reason','strategy_lock_source','strategy_locked_at')})
            passed = [a['evaluation_passed_at'] for a in finance_group if number(a.get('evaluation_passed_at')) is not None]
            if passed:
                canonical['evaluation_passed_at'] = min(passed)
            for other in (a for a in group if a is not canonical):
                aliases[other['id']] = canonical['id']
                other['merged_into'] = canonical['id']
                other['merged_at'] = self.clock()
        if not aliases:
            return
        for collection in (self.data['history'],list(self.data['jobs'].values()),list(self.data['positions'].values())):
            for record in collection:
                record['account_id'] = aliases.get(record['account_id'],record['account_id'])
        strategy = self.data['strategy']
        for key in ('batch_account_ids','completed_accounts','remaining_accounts','order_queue','upcoming_account_ids'):
            if isinstance(strategy.get(key),list):
                strategy[key] = list(dict.fromkeys(aliases.get(a,a) for a in strategy[key]))
        if strategy.get('config',{}).get('account_ids'):
            strategy['config']['account_ids'] = list(dict.fromkeys(aliases.get(a,a) for a in strategy['config']['account_ids']))
        for session in self.data['execution_sessions'].values():
            session['account_ids'] = list(dict.fromkeys(aliases.get(a,a) for a in session.get('account_ids',[])))
        for row in self.data['read_only_observation'].get('accounts',[]):
            if row.get('account_id') in aliases:
                row['account_id'] = aliases[row['account_id']]
        self.data['audit'].append({'id':uuid.uuid4().hex,'time':self.clock(),'type':'account_deduplication','aliases':aliases})
        self._log('info',f'已按完整账户名称全局归并 {len(aliases)} 条重复记录，历史及原记录已保留')

    def _pending(self, aid):
        return [j for j in self.data['jobs'].values() if j['account_id'] == aid and j['status'] in self.UNRESOLVED]

    def _position(self, aid):
        return next((p for p in self.data['positions'].values() if p['account_id']==aid), None)

    def _strategy_outstanding(self, aid):
        position = self._position(aid)
        return bool((position and position.get('source')=='strategy') or any(
            j.get('source')=='strategy' or (self.data['positions'].get(j.get('local_position_id')) or {}).get('source')=='strategy'
            for j in self._pending(aid)))

    @staticmethod
    def _strategy_lock_defaults(account):
        for key, value in (('strategy_locked',False),('strategy_lock_reason',None),
                           ('strategy_lock_source',None),('strategy_locked_at',None)):
            account.setdefault(key,value)

    def _exclude_primary_account(self, account):
        """Persist the exclusion and cancel only actions that have not left us."""
        if not execution_excluded(account):
            return
        first = account.get('execution_exclusion_source') != 'primary_account'
        reason = '主账户永久排除所有执行，仅展示账户信息'
        account.update({'execution_excluded':True,'execution_exclusion_source':'primary_account',
                        'execution_exclusion_reason':reason,'enabled':False,
                        'strategy_locked':True,'strategy_lock_source':'execution_excluded',
                        'strategy_lock_reason':reason})
        account.setdefault('execution_excluded_at',self.clock())
        account['strategy_locked_at'] = account['execution_excluded_at']
        aid = account['id']
        strategy = self.data['strategy']
        for key in ('remaining_accounts','order_queue'):
            strategy[key] = [a for a in strategy.get(key,[]) if a != aid]
        if aid in strategy.get('upcoming_account_ids',[]):
            strategy['upcoming_account_ids'] = []
            strategy['preparation'] = {'status':'pending','accounts':{}}
        if strategy.get('phase')=='submitting' and not strategy['order_queue']:
            strategy['phase'] = 'monitoring'
            strategy['next_action_at'] = None
        for job in self.data['jobs'].values():
            if job['account_id']==aid and job['status']=='queued':
                job.update({'status':'cancelled','cancel_reason':reason})
                position = self.data['positions'].get(job.get('local_position_id'))
                if position and position.get('status')=='closing':
                    position['status'] = 'open'
        if aid in self.data['bridge_sessions']:
            self.data['bridge_sessions'][aid]['ready'] = False
        if first:
            self._log('info',f"{account['name']} 已永久排除所有开仓、平仓及自动策略；仅保留只读信息")

    @staticmethod
    def _assert_execution_allowed(account):
        if execution_excluded(account):
            raise ValidationError('主账户永久排除所有执行，仅允许读取账户信息',403)
        if account.get('retired_failed'):
            raise ValidationError('该账户已确认评估失败并永久停用，不能恢复执行',403)
        if account.get('archived'):
            raise ValidationError('该账户已手动归档，不参与执行',403)

    @staticmethod
    def _enforce_archived_account(account):
        if account.get('archived'):
            account['enabled']=False

    @staticmethod
    def _enforce_retired_failure(account):
        if account.get('retired_failed'):
            closed=account.get('terminal_status')=='closed'
            account.update({'enabled':False,'status':'locked','category':'closed' if closed else 'failed',
                            'lock_reason':('平台账户已关闭，账户已永久停用' if closed else
                                          '页面已确认评估失败，账户已永久停用' if account.get('failure_confirmation_source')=='platform'
                                          else '用户确认评估失败，账户已永久停用')})

    def _roll_strategy_locks(self, now=None):
        """Expire local exclusions at the next NY close, preserving account risk."""
        now = self.clock() if now is None else now
        day_start = entry_day_window(now)['start']
        changed = False
        for account in self.data['accounts'].values():
            if account.get('merged_into') or execution_excluded(account):
                continue
            locked_at = number(account.get('strategy_locked_at'))
            if account.get('strategy_locked') and account.get('strategy_lock_source')!='live_default' and locked_at is not None and locked_at < day_start:
                account.update({'strategy_locked':False,'strategy_lock_reason':None,
                                'strategy_lock_source':None,'strategy_locked_at':None})
                self._log('info',f"{account['name']} 新交易日已自动解除策略锁定；账户风控及平台状态保持不变")
                changed = True
        for account in self.data['accounts'].values():
            changed = self._apply_live_strategy_default(account) or changed
        return changed

    @staticmethod
    def _is_live_account(account):
        return account.get('phase')=='live' or account.get('category')=='live'

    def _apply_live_strategy_default(self, account):
        if (not self._is_live_account(account) or account.get('merged_into') or execution_excluded(account)
            or account.get('retired_failed') or account.get('archived')):
            return False
        enabled=self.data.get('preferences',{}).get('strategy_config',{}).get('live_default_locked',True)
        if not enabled and account.get('strategy_lock_source')=='live_default':
            account.update(strategy_locked=False,strategy_lock_reason=None,strategy_lock_source=None,strategy_locked_at=None)
            self._log('info',f"{account['name']} 已取消 Live 默认锁定；不会自动加入当前批次")
            return True
        if enabled and not account.get('live_default_lock_exempt') and not account.get('strategy_locked'):
            self._lock_strategy(account,'Live 账户默认锁定，可逐个手动解锁','live_default')
            return True
        return False

    def _lock_strategy(self, account, reason, source, *, new_event=False):
        """Exclude future strategy entries without stopping position protection."""
        if execution_excluded(account):
            self._exclude_primary_account(account)
            return
        self._strategy_lock_defaults(account)
        if source=='evaluation_passed':
            # A repeated Passed heartbeat is the same event. It must not
            # recreate yesterday's exclusion immediately after the reset.
            if number(account.get('evaluation_passed_at')) is None:
                prior = number(account.get('strategy_locked_at')) if account.get('strategy_lock_source')=='evaluation_passed' else None
                account['evaluation_passed_at'] = self.clock() if prior is None else prior
            if not new_event and account['evaluation_passed_at'] < entry_day_window(self.clock())['start']:
                return
        priority = {'manual':10,'external_close':1,'daily_completed':1,'take_profit':2,'stop_loss':2,'target_reached':2,'evaluation_passed':3}
        existing = account.get('strategy_lock_source')
        if account['strategy_locked'] and priority.get(existing,0) >= priority.get(source,0) and not (existing=='live_default' and source=='manual'):
            return
        account.update({'strategy_locked':True,'strategy_lock_reason':reason,
                        'strategy_lock_source':source,'strategy_locked_at':self.clock()})
        self._exclude_queued_entries(account['id'], reason, all_opens=source=='evaluation_passed')
        self._log('info',f"{account['name']} 已锁定自动策略：{reason}；已有持仓继续保护")

    def _exclude_queued_entries(self, aid, reason, *, all_opens):
        strategy = self.data['strategy']
        strategy['remaining_accounts'] = [a for a in strategy.get('remaining_accounts',[]) if a != aid]
        strategy['order_queue'] = [a for a in strategy.get('order_queue',[]) if a != aid]
        if aid in strategy.get('upcoming_account_ids',[]):
            strategy['upcoming_account_ids'] = []
            strategy['preparation'] = {'status':'pending','accounts':{}}
        if strategy.get('phase')=='submitting' and not strategy['order_queue']:
            strategy['phase'] = 'monitoring'
            strategy['next_action_at'] = None
        for job in self.data['jobs'].values():
            if job['account_id'] == aid and job['type']=='open' and job['status']=='queued' and (all_opens or job.get('source')=='strategy'):
                job['status'] = 'cancelled'
                job['cancel_reason'] = reason+'；已取消尚未派发的开仓'

    def _health_locked(self, account):
        return (account.get('health_unlock_at') or 0) > self.clock()

    def _lock_low_health(self, account, reading):
        if (not reading or reading.get('update_pending') or reading.get('percent') is None
            or not 0 <= reading['percent'] < self.MIN_ACCOUNT_HEALTH
            or abs(self.clock()-reading['observed_at']) > 5
            or reading.get('phase') != account.get('phase')
            or reading.get('category') != account.get('category')
            or account.get('status') != 'active' or not account.get('enabled')
            or execution_excluded(account) or account.get('archived') or account.get('retired_failed')
            or account.get('ambiguous') or self._health_locked(account)
            or self._position(account['id'])):
            return
        day = entry_day_window(reading['observed_at'])
        if not day['start'] <= self.clock() < day['end']:
            return
        account['health_unlock_at'] = day['end']
        reason = f"账户健康度 {reading['percent']:g}% 低于 {self.MIN_ACCOUNT_HEALTH}%，当日锁定"
        self._exclude_queued_entries(account['id'], reason, all_opens=True)
        self._log('warning', f"{account['name']} {reason}")

    def _observe_snapshot_health(self, account, snapshot):
        percent = snapshot.get('account_health')
        if percent is None or snapshot.get('position_open') is not False or snapshot.get('phase') != account.get('phase'):
            return
        metrics = account.setdefault('capital_metrics', {})
        previous = metrics.get('health') or {}
        reading = dict(percent=percent, text=f'{percent:g}%', current=None, limit=None,
                       observed_at=snapshot['observed_at'], category=account.get('category',''),
                       phase=account.get('phase'), source='vest-page', update_pending=False)
        if snapshot['observed_at'] >= previous.get('observed_at', 0):
            metrics['health'] = reading
            self._lock_low_health(account, reading)

    def strategy_lock_account(self, aid, locked):
        with self.lock:
            if not isinstance(locked,bool):
                raise ValidationError('locked 必须是布尔值')
            self._roll_strategy_locks()
            account = self._account(aid)
            self._assert_execution_allowed(account)
            self._strategy_lock_defaults(account)
            if locked:
                if self._is_live_account(account):account['live_default_lock_exempt']=False
                self._lock_strategy(account,'手动排除自动策略','manual')
            else:
                if account.get('status')=='passed' or account.get('category')=='passed':
                    raise ValidationError('账户已通过评估，不能重新加入自动策略')
                account.update({'strategy_locked':False,'strategy_lock_reason':None,
                                'strategy_lock_source':None,'strategy_locked_at':None})
                if self._is_live_account(account):account['live_default_lock_exempt']=True
                self._log('info',f"{account['name']} 已解除策略锁定；不会自动加入正在运行的批次")
            self._save()
            return copy.deepcopy(account)

    def _lock_strategy_exit(self, position, reason, pnl):
        self._roll_strategy_locks()
        account = self._account(position['account_id'])
        if account.get('status')=='passed' or account.get('category')=='passed':
            self._lock_strategy(account,'评估阶段已通过','evaluation_passed',new_event=True)
        elif reason == 'target':
            self._lock_strategy(account,'账户目标平仓已结束','target_reached')
        elif reason == 'take_profit' or (position.get('take_profit') is not None and pnl >= position['take_profit']):
            self._lock_strategy(account,'交易达到止盈，退出自动策略','take_profit')
        elif reason in ('stop_loss','max_loss') or (position.get('stop_loss') is not None and pnl <= -position['stop_loss']):
            self._lock_strategy(account,'交易达到止损，退出自动策略','stop_loss')
        elif reason == 'external':
            self._lock_strategy(account,'策略持仓已在网页结束，平仓原因未确认','external_close')
        else:
            self._lock_strategy(account,'当日交易已结束，退出自动策略','daily_completed')

    def _daily_entry_used(self, aid, *, exclude_job_id=None, window=None, phase=None):
        period = window or entry_day_window(self.clock())
        account=self._account(aid)
        scope=phase or account.get('phase')
        def same_phase(record):
            return not scope or self._quota_phase(account,record)==scope
        def today(value):
            timestamp = observed_time(value)
            return max(period['start'],self._claim_reentry_cutoff(account) if scope=='live' else 0) <= timestamp < period['end'] and timestamp <= self.clock()
        return (any(t['account_id']==aid and same_phase(t) and today(t.get('opened_at')) for t in self.data['history'])
                or any(p['account_id']==aid and same_phase(p) and today(p.get('opened_at')) for p in self.data['positions'].values())
                or any(j['account_id']==aid and j['type']=='open' and j['id']!=exclude_job_id
                       and same_phase(j) and j.get('skip_reason')!='account_health'
                       and j.get('delivered_at') is not None and today(j['delivered_at']) for j in self.data['jobs'].values()))

    @staticmethod
    def _position_exit_reason(account, position):
        pnl = position['unrealized_pnl']
        if account.get('max_loss') is not None and account.get('initial_balance') is not None and account['equity']-account['initial_balance'] <= -account['max_loss']:
            return 'max_loss'
        if (account.get('status')=='passed' or account.get('category')=='passed'
            or (account.get('profit_target') is not None and account.get('initial_balance') is not None
                and account['equity']-account['initial_balance'] >= account['profit_target'])):
            return 'target'
        if position.get('take_profit') is not None and pnl >= position['take_profit']:
            return 'take_profit'
        if position.get('stop_loss') is not None and pnl <= -position['stop_loss']:
            return 'stop_loss'
        return None

    def _fresh(self, aid):
        session = self.data['bridge_sessions'].get(aid, {})
        snapshot = session.get('snapshot', {})
        account=self._account(aid)
        return ((not account.get('phase') or snapshot.get('phase')==account['phase'])
                and self.clock()-session.get('last_seen', 0) <= 5
                and abs(self.clock()-observed_time(snapshot.get('observed_at'))) <= 5
                and all(number(snapshot.get(k)) is not None for k in ('balance','unrealized_pnl','price'))
                and number(snapshot.get('price')) > 0
                and isinstance(snapshot.get('position_open'), bool))

    def _ready(self, aid, *, opening=False):
        session = self.data['bridge_sessions'].get(aid, {})
        account = self._account(aid)
        granted = not account.get('auto_discovered') or self._execution_capable(aid)
        return (not execution_excluded(account) and granted and session.get('ready') is True and self._fresh(aid)
                and (not opening or session.get('snapshot', {}).get('position_open') is False))

    def _execution_capable(self, aid):
        account = self._account(aid)
        execution = self.data['execution_sessions'].get(account.get('session_id'), {})
        return bool(not execution_excluded(account) and not account.get('retired_failed') and not account.get('archived') and account.get('auto_discovered') and not account.get('ambiguous')
                    and (not account.get('phase') or (execution.get('account_phases',{}).get(aid) is not None and
                         execution['account_phases'][aid] in (account['phase'],account.get('phase_pending',{}).get('phase'))))
                    and account.get('available',False) and execution.get('mode') == 'execute'
                    and 0 <= self.clock()-execution.get('last_seen',0) <= 30
                    and aid in execution.get('account_ids',[])
                    and account.get('initial_balance') is not None)

    def _eligible(self, aid, *, live_ready=True, for_strategy=False, live_default_locked=None):
        if self._roll_strategy_locks():
            self._save()
        account = self._account(aid)
        self._assert_execution_allowed(account)
        if self._health_locked(account):
            raise ValidationError(f"{account['name']} 账户健康度锁定，下一交易日可重新核验")
        if account.get('phase_pending'):
            raise ValidationError('阶段切换同步中，等待明确空仓读数')
        locked=account.get('strategy_locked')
        if live_default_locked is False and account.get('strategy_lock_source')=='live_default':locked=False
        if live_default_locked is True and self._is_live_account(account) and not account.get('live_default_lock_exempt'):locked=True
        if for_strategy and locked:
            raise ValidationError(f"{account['name']} 已锁定，不参与自动策略")
        if account['mode'] != self.data['mode']:
            raise ValidationError('模拟账户与浏览器账户不可混用')
        if not account['enabled'] or account['status'] != 'active':
            raise ValidationError(f"{account['name']} 已停用、通过或被锁定")
        if self._daily_entry_used(aid):
            raise ValidationError(f"{account['name']} 本交易日已开仓，每账户每日只允许一单（美东 20:00 日切）")
        if self._position(aid) or self._pending(aid):
            raise ValidationError(f"{account['name']} 已有持仓或待确认操作")
        if account['mode']=='browser' and live_ready:
            if account.get('auto_discovered'):
                if not self._execution_capable(aid) or account.get('external_position'):
                    raise ValidationError(f"{account['name']} 插件未启用执行、账户身份不明确或存在外部持仓")
            elif not self._ready(aid, opening=True):
                raise ValidationError(f"{account['name']} 浏览器未就绪、数据过期或未确认空仓")
        return account

    def _ids(self, value):
        if not isinstance(value, list) or not 1 <= len(value) <= 100:
            raise ValidationError('请至少选择一个账户，最多 100 个')
        if any(not isinstance(a, str) for a in value) or len(set(value)) != len(value):
            raise ValidationError('账户列表格式错误或含重复账户')
        for aid in value:
            self._account(aid)
        return value

    def _assert_settled_browser_actions(self):
        if self.data['mode']!='browser':
            return
        if any(c['status'] in self.CLAIM_UNCERTAIN for c in self._pending_profit_claims()):
            raise ValidationError('利润领取结果尚未核验，请在只读模式核对该 Live 号余额',409)
        for job in self.data['jobs'].values():
            if job['status'] in ('delivered','unknown'):
                account=self.data['accounts'].get(job['account_id'],{})
                if account.get('mode')=='browser':
                    raise ValidationError(f"{account.get('name','账户')} 有结果待核验的开平仓操作，请先在 Vest 核对持仓，再在账户表点击「人工核对」；未创建新订单",409)

    def _order_config(self, body, *, strategy=False):
        if body.get('symbol', 'NQ-PERP') != 'NQ-PERP':
            raise ValidationError('当前版本仅支持 NQ-PERP')
        side = body.get('side', 'random' if strategy else 'buy')
        if side not in (('buy','sell','random') if strategy else ('buy','sell')):
            raise ValidationError('交易方向无效')
        sizing = {'quantity':positive(body.get('quantity'),'数量',100000)} if not strategy or 'size_percent' not in body else None
        if sizing is None:
            pct = body['size_percent']
            if isinstance(pct,bool) or not isinstance(pct,(int,float)) or pct not in (25,50,75,100):
                raise ValidationError('仓位大小只能选择 25%、50%、75% 或 100%')
            if body.get('quantity') is not None:
                raise ValidationError('按仓位大小下单时不能同时填写固定数量')
            sizing = {'size_percent':int(pct)}
        policy = {}
        if strategy:
            try:require_strategy(body.get('strategy_id','random_entry'))
            except ValueError as exc:raise ValidationError(str(exc)) from None
            enabled = body.get('skip_stop_loss_on_low_drawdown',False)
            if not isinstance(enabled,bool):
                raise ValidationError('回撤不足时不设止损开关必须是布尔值')
            if enabled and 'size_percent' not in sizing:
                raise ValidationError('回撤止损规则仅支持按仓位比例执行的策略')
            policy = dict(strategy_id=body.get('strategy_id','random_entry'),skip_stop_loss_on_low_drawdown=enabled,
                drawdown_stop_loss_threshold=positive(body.get('drawdown_stop_loss_threshold',200),'剩余最大回撤阈值'))
            for key in ('live_default_locked','live_skip_stop_loss_on_low_drawdown'):
                value=body.get(key,STRATEGY_FORM_DEFAULTS[key])
                if not isinstance(value,bool):raise ValidationError('Live 策略开关必须是布尔值')
                policy[key]=value
            live_side=body.get('live_side','random')
            if live_side not in ('random','buy','sell'):raise ValidationError('Live 下单方向无效')
            pct=body.get('live_size_percent',25)
            if isinstance(pct,bool) or pct not in (25,50,75,100):raise ValidationError('Live 仓位大小只能选择 25%、50%、75% 或 100%')
            if policy['live_skip_stop_loss_on_low_drawdown'] and 'size_percent' not in sizing:
                raise ValidationError('Live 回撤止损规则仅支持按仓位比例执行的策略')
            policy.update(live_side=live_side,live_size_percent=int(pct),
                live_drawdown_stop_loss_threshold=positive(body.get('live_drawdown_stop_loss_threshold',200),'Live 剩余最大回撤阈值'))
            for key,label,default in (('live_take_profit','Live 止盈金额',None),('live_stop_loss','Live 止损金额',130)):
                value=body.get(key,default)
                policy[key]=None if value is None else positive(value,label)
            if policy['live_skip_stop_loss_on_low_drawdown'] and policy['live_stop_loss'] is None:
                raise ValidationError('Live 低回撤止损规则需要先填写止损金额')
            recipient=body.get('live_payout_recipient','main')
            if recipient!='main':raise ValidationError('Live 利润收款人必须为主账户')
            payout_mode=body.get('live_payout_mode','all')
            if payout_mode not in ('all','amount','percent'):raise ValidationError('Live 利润领取方式无效')
            payout_amount=positive(body.get('live_payout_amount',1),'Live 利润领取金额')
            if round(payout_amount,6)!=payout_amount:raise ValidationError('Live 利润领取金额最多支持 6 位小数')
            policy.update(live_payout_recipient=recipient,live_payout_mode=payout_mode,live_payout_amount=payout_amount,
                live_payout_percent=positive(body.get('live_payout_percent',100),'Live 利润领取比例',100))
        return {'symbol': 'NQ-PERP', 'side': side, **sizing, **policy,
                'take_profit': positive(body['take_profit'], '止盈金额') if body.get('take_profit') is not None else None,
                'stop_loss': positive(body.get('stop_loss',130), '止损金额')}

    def _strategy_form_config(self, body):
        values = {**STRATEGY_FORM_DEFAULTS, **{k:v for k,v in body.items() if k in STRATEGY_FORM_DEFAULTS}}
        values['take_profit'] = None
        cfg = self._order_config(values,strategy=True)
        n = values['batch_size']
        if isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or int(n)!=n or not 1<=n<=100:
            raise ValidationError('每批账户数量必须为 1–100 的整数')
        cfg['batch_size'] = int(n)
        for field in ('order_delay_min','order_delay_max','batch_delay_min','batch_delay_max'):
            cfg[field] = nonnegative(values[field],field)
        if cfg['order_delay_min']>cfg['order_delay_max'] or cfg['batch_delay_min']>cfg['batch_delay_max']:
            raise ValidationError('随机延迟最小值不能大于最大值')
        return cfg

    def save_strategy_config(self, body):
        with self.lock:
            cfg = self._strategy_form_config({**self.data['preferences']['strategy_config'],**body})
            self.data['preferences']['strategy_config'] = cfg
            self._roll_strategy_locks()
            self._save()
            return copy.deepcopy(cfg)

    def _strategy_stop_decision(self, config, account, metric):
        configured = config.get('configured_stop_loss',config['stop_loss'])
        if not config.get('skip_stop_loss_on_low_drawdown'):
            return configured, None
        if not isinstance(metric,dict) or metric.get('update_pending') or (account.get('phase') and metric.get('phase')!=account['phase']):
            raise ValidationError('剩余最大回撤缺少当前阶段的新鲜金额读数，等待读取后再开仓',409)
        values = [metric.get(k) for k in ('current','limit','observed_at')]
        if (any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in values)
            or metric['limit']<=0 or metric.get('percent') is not None
            or not 0<=self.clock()-metric['observed_at']<=5):
            raise ValidationError('剩余最大回撤读数无效或已过期，等待读取后再开仓',409)
        remaining = round(metric['limit']-metric['current'],8)
        return (None if remaining<config['drawdown_stop_loss_threshold'] else configured), remaining

    def add_account(self, body):
        with self.lock:
            name = body.get('name')
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
                raise ValidationError('账户名称需为 1–64 个字符')
            if len(self.data['accounts']) >= 100:
                raise ValidationError('最多支持 100 个账户')
            mode = body.get('mode', self.data['mode'])
            if mode not in ('paper','browser'):
                raise ValidationError('账户模式无效')
            initial = positive(body.get('initial_balance'), '初始资金')
            target = positive(body.get('profit_target'), '通过目标')
            loss = positive(body.get('max_loss'), '最大亏损')
            if loss >= initial:
                raise ValidationError('最大亏损必须小于初始资金')
            aid = uuid.uuid4().hex[:12]
            account = {'id': aid, 'name': name.strip(), 'balance': initial,
                       'initial_balance': initial, 'profit_target': target, 'max_loss': loss,
                       'realized_pnl': 0, 'unrealized_pnl': 0, 'equity': initial,
                       'status': 'active', 'enabled': True, 'mode': mode,
                       'created_at': self.clock(), 'lock_reason': None}
            self._strategy_lock_defaults(account)
            self.data['accounts'][aid] = account
            self._exclude_primary_account(account)
            self._log('info', f'已创建{ "模拟" if mode=="paper" else "浏览器" }账户 {name.strip()}')
            self._save()
            return copy.deepcopy(account)

    def toggle_account(self, aid, enabled):
        with self.lock:
            account = self._account(aid)
            if not isinstance(enabled, bool):
                raise ValidationError('enabled 必须是布尔值')
            if enabled:
                self._assert_execution_allowed(account)
            account['enabled'] = enabled
            self._save()
            return copy.deepcopy(account)

    def delete_account(self, aid):
        with self.lock:
            account = self._account(aid)
            if account.get('retired_failed') or account.get('archived'):
                raise ValidationError('已归档或评估失败账户需保留原始记录，不能删除后重新启用',403)
            if self._position(aid) or self._pending(aid):
                raise ValidationError('存在持仓或待确认操作，不能删除账户')
            s = self.data['strategy']
            if s['status'] in self.ACTIVE and aid in s['config'].get('account_ids', []):
                raise ValidationError('请先停止使用此账户的策略')
            del self.data['accounts'][aid]
            self._save()
            return {'ok': True}

    def settings(self, body):
        with self.lock:
            mode = body.get('mode',self.data['mode'])
            if mode not in ('paper', 'browser'):
                raise ValidationError('模式无效')
            automatic=body.get('auto_archive_failed',self.data['preferences']['auto_archive_failed'])
            if not isinstance(automatic,bool):
                raise ValidationError('auto_archive_failed 必须是布尔值')
            if 'mode' in body and (self.data['strategy']['status'] in self.ACTIVE or any(j['status'] in self.UNRESOLVED for j in self.data['jobs'].values())):
                raise ValidationError('策略运行或存在待确认操作，不能切换模式')
            if mode != self.data['mode'] and self.data['positions']:
                raise ValidationError('仍有持仓需要保护，请先平仓后再切换模式')
            if 'mode' in body:
                self.data['mode'] = mode
                self._log('info', f'切换至{ "模拟交易" if mode=="paper" else "浏览器桥接" }模式')
            self.data['preferences']['auto_archive_failed']=automatic
            if automatic:
                for account in self.data['accounts'].values():
                    if not account.get('merged_into') and account.get('retired_failed'):
                        self._archive_failed_account(account,'automatic')
            self._save()
            return {'mode':mode,'preferences':copy.deepcopy(self.data['preferences'])}

    def _new_job(self, kind, aid, config=None, position=None, reason='manual'):
        self._assert_execution_allowed(self._account(aid))
        if kind=='close' and position and reason=='target':
            self._lock_strategy(self._account(aid),'已触发账户目标平仓','target_reached')
        if kind=='close' and position and position.get('source')=='strategy' and reason in ('take_profit','stop_loss','max_loss'):
            source = 'take_profit' if reason=='take_profit' else 'stop_loss'
            self._lock_strategy(self._account(aid),'自动策略达到'+('止盈' if source=='take_profit' else '止损'),source)
        if position is not None and config is None:
            config = {k:position[k] for k in ('symbol','side','quantity','take_profit','stop_loss')}
        job = {'id': uuid.uuid4().hex, 'type': kind, 'account_id': aid,
               'status': 'queued', 'created_at': self.clock(), 'delivered_at': None,
               'local_position_id': position['id'] if position else None,
               'position_id': position.get('browser_position_id') if position else None,
               'browser_position_id': position.get('browser_position_id') if position else None, 'reason': reason,
               **(config or {})}
        if position:
            job['source'] = position.get('source','manual')
        account = self._account(aid)
        job['phase']=position.get('phase') if position else account.get('phase')
        if account.get('auto_discovered'):
            execution = self.data['execution_sessions'].get(account['session_id'],{})
            job.update({'session_id':account['session_id'],'identity':account['identity'],
                        'mode_epoch':execution.get('mode_epoch',0)})
        self.data['jobs'][job['id']] = job
        return job

    def _open_one(self, aid, cfg, source):
        if self.data['mode']=='browser' and self._pending_profit_claims():
            raise ValidationError('Live 利润领取尚未核验，禁止新开仓',409)
        account = self._eligible(aid,for_strategy=source=='strategy')
        if source=='strategy':
            self._assert_strategy_batch_side(aid,cfg)
            s=self.data['strategy']
            cfg={**cfg,'strategy_run_id':s['run_id'],'strategy_batch_number':s['batch_number']}
        if account['mode'] == 'browser':
            if cfg.get('skip_stop_loss_on_low_drawdown'):
                cfg = {**cfg,'configured_stop_loss':cfg['stop_loss']}
            job = self._new_job('open', aid, {**cfg, 'source': source})
            self._log('info', f"{account['name']} 开仓请求已排队，等待浏览器确认")
            return {'job_id': job['id'], 'account_id': aid, 'status': 'pending'}
        price = self.data['market']['price']
        if cfg.get('skip_stop_loss_on_low_drawdown'):
            # Paper accounts use their explicit simulated loss limit and equity.
            if account.get('max_loss') is None or account.get('initial_balance') is None:
                raise ValidationError('模拟账户最大亏损或资金基准缺失，不能计算剩余回撤')
            metric = dict(current=account['initial_balance']-account['equity'],limit=account['max_loss'],observed_at=self.clock(),phase=account.get('phase'))
            stop, remaining = self._strategy_stop_decision(cfg,account,metric)
            cfg = {**cfg,'configured_stop_loss':cfg['stop_loss'],'stop_loss':stop,
                'stop_loss_omitted':stop is None,'remaining_drawdown_at_entry':remaining}
        if 'size_percent' in cfg:
            # Paper-only 1x cash simulation. Real sizing comes exclusively from
            # Vest's percentage button and is never inferred from cash here.
            cfg = {**cfg,'quantity':positive(round(account['equity']*cfg['size_percent']/100/price,8),'模拟仓位数量',100000)}
        position = {'id': uuid.uuid4().hex, 'account_id': aid, 'account_name': account['name'],
                    **cfg, 'entry_price': price, 'mark_price': price, 'unrealized_pnl': 0,
                    'opened_at': self.clock(), 'source': source, 'mode': 'paper', 'status': 'open'}
        self.data['positions'][position['id']] = position
        self._log('info', f"[模拟] {account['name']} {'买入' if cfg['side']=='buy' else '卖出'} {cfg['quantity']:g} NQ-PERP")
        return copy.deepcopy(position)

    def place_orders(self, body):
        with self.lock:
            cfg = self._order_config(body)
            ids = self._ids(body.get('account_ids'))
            key = body.get('idempotency_key')
            if not isinstance(key, str) or not 8 <= len(key) <= 128:
                raise ValidationError('下单必须提供 8–128 位幂等键')
            fingerprint = json.dumps({'ids': ids, 'cfg': cfg}, sort_keys=True)
            if key in self.data['idempotency']:
                prior = self.data['idempotency'][key]
                if prior['fingerprint'] != fingerprint:
                    raise ValidationError('同一幂等键不能用于不同订单', 409)
                return copy.deepcopy(prior['response'])
            self._assert_settled_browser_actions()
            for aid in ids:
                self._eligible(aid)
                s = self.data['strategy']
                if s['status'] in self.ACTIVE and aid in s['config'].get('account_ids', []) and not self._account(aid).get('strategy_locked'):
                    raise ValidationError('账户由运行中的策略管理，请先停止策略')
            response = {'ok': True, 'orders': [self._open_one(aid, cfg, 'manual') for aid in ids]}
            self.data['idempotency'][key] = {'fingerprint': fingerprint, 'response': response}
            self._save()
            return copy.deepcopy(response)

    def _account_status(self, account):
        self._enforce_retired_failure(account)
        if account.get('lock_reason'):
            return
        if account.get('initial_balance') is None or account.get('equity') is None or account.get('balance') is None:
            return
        if account.get('max_loss') is not None and account['equity']-account['initial_balance'] <= -account['max_loss']:
            account['status'] = 'locked'
            account['lock_reason'] = '已达到配置的账户最大亏损'
        elif account.get('profit_target') is not None and account['balance']-account['initial_balance'] >= account['profit_target']:
            if account.get('status')!='passed':
                account['evaluation_passed_at'] = self.clock()
            account['status'] = 'passed'
        if account['status']=='passed':
            self._lock_strategy(account,'评估阶段已通过','evaluation_passed')

    def _close_paper(self, position, reason):
        account = self._account(position['account_id'])
        self._assert_execution_allowed(account)
        pnl = round(position['unrealized_pnl'], 2)
        account['balance'] = round(account['balance']+pnl, 2)
        account['realized_pnl'] = round(account['balance']-account['initial_balance'], 2)
        account['unrealized_pnl'] = 0
        account['equity'] = account['balance']
        trade = {k: position[k] for k in ('id','account_id','account_name','symbol','side','quantity','entry_price','opened_at','source','mode')}
        trade['phase']=position.get('phase')
        trade.update({'exit_price': position['mark_price'], 'pnl': pnl, 'closed_at': self.clock(), 'reason': reason})
        self.data['history'].append(trade)
        del self.data['positions'][position['id']]
        self._account_status(account)
        self._lock_strategy_exit(position,reason,pnl)
        self._log('success' if pnl >= 0 else 'warning', f"[模拟] {account['name']} 已平仓 {pnl:+.2f} USDC · {reason}")

    def close_position(self, pid, reason='manual'):
        with self.lock:
            position = self.data['positions'].get(pid)
            if not position:
                raise ValidationError('持仓不存在或已平仓', 404)
            self._assert_execution_allowed(self._account(position['account_id']))
            if position['mode'] == 'paper':
                self._close_paper(position, reason)
                result = {'ok': True, 'status': 'closed'}
            else:
                if self._pending(position['account_id']):
                    raise ValidationError('已有待确认操作；请勿重复提交')
                account = self._account(position['account_id'])
                if account.get('auto_discovered'):
                    if not self._execution_capable(account['id']):
                        raise ValidationError('插件处于只读模式或连接已过期，不能执行平仓')
                else:
                    if not self._ready(position['account_id']):
                        raise ValidationError('浏览器未就绪或持仓数据已过期')
                    if self.data['bridge_sessions'][position['account_id']]['snapshot'].get('position_open') is not True:
                        raise ValidationError('页面未明确显示持仓，请到 Vest 人工核对')
                    current = self.data['bridge_sessions'][position['account_id']]['snapshot']
                    if not self._matches_position(position, current):
                        raise ValidationError('页面持仓标识、方向或数量已变化，请到 Vest 人工核对')
                job = self._new_job('close', position['account_id'], position=position, reason=reason)
                position['status'] = 'closing'
                result = {'ok': True, 'status': 'pending', 'job_id': job['id']}
            self._save()
            return result

    def close_all(self):
        with self.lock:
            # Stop first so that flattening cannot trigger the next batch.
            self._stop_strategy()
            results = []
            for pid, position in list(self.data['positions'].items()):
                if position['mode'] != self.data['mode']:
                    continue
                if execution_excluded(self._account(position['account_id'])):
                    results.append({'position_id':pid,'status':'excluded','reason':'主账户永久排除所有执行'})
                    continue
                try:
                    results.append({'position_id': pid, **self.close_position(pid, 'manual')})
                except ValidationError as exc:
                    results.append({'position_id': pid, 'error': str(exc)})
            self._save()
            return {'ok': not any('error' in x for x in results), 'results': results}

    def start_strategy(self, body):
        with self.lock:
            if self._pending_profit_claims():raise ValidationError('请先完成或核对原轮次的 Live 利润领取，不能新建轮次',409)
            if self.data['strategy']['status'] in self.ACTIVE:
                raise ValidationError('策略已运行', 409)
            self._assert_settled_browser_actions()
            live_saved={k:v for k,v in self.data['preferences']['strategy_config'].items() if k.startswith('live_')}
            cfg = self._order_config({**live_saved,**body}, strategy=True)
            ids = self._ids(body.get('account_ids'))
            n = body.get('batch_size')
            if isinstance(n, bool) or not isinstance(n, (int,float)) or not math.isfinite(n) or int(n)!=n or not 1 <= n <= len(ids):
                raise ValidationError('每批账户数量必须为正整数且不超过所选账户数')
            for aid in ids:
                self._eligible(aid,for_strategy=True,live_default_locked=cfg['live_default_locked'])
            cfg.update({'account_ids': list(ids), 'batch_size': int(n)})
            for field in ('order_delay_min','order_delay_max','batch_delay_min','batch_delay_max'):
                cfg[field] = nonnegative(body.get(field), field)
            if cfg['order_delay_min'] > cfg['order_delay_max'] or cfg['batch_delay_min'] > cfg['batch_delay_max']:
                raise ValidationError('随机延迟最小值不能大于最大值')
            self.data['strategy'] = {'status': 'running', 'batch_number': 0, 'run_id':uuid.uuid4().hex,
                'batch_account_ids': [], 'completed_accounts': [], 'remaining_accounts': list(ids),
                'next_action_at': self.clock(), 'config': cfg, 'phase': 'next_batch', 'order_queue': [],
                'upcoming_account_ids':[], 'preparation':{'status':'disabled','accounts':{}}}
            self.data['preferences']['strategy_config'] = self._strategy_form_config(cfg)
            self._roll_strategy_locks()
            self._log('success', f"随机策略启动：每批 {n} 个账户；账户池使用一次后结束")
            self._save()
            return copy.deepcopy(self.data['strategy'])

    def _stop_strategy(self):
        s = self.data['strategy']
        s['status'] = 'stopped'
        s['order_queue'] = []
        s['next_action_at'] = None
        s['upcoming_account_ids'] = []
        s['preparation'] = {'status':'inactive','accounts':{}}
        # An action not yet delivered can safely be cancelled. Delivered jobs
        # cannot be recalled and still require reconciliation.
        for job in self.data['jobs'].values():
            if job['type']=='open' and job['status']=='queued' and job.get('source')=='strategy':
                job['status'] = 'cancelled'

    def stop_strategy(self):
        with self.lock:
            self._stop_strategy()
            self._log('warning', '策略已停止；不再开新仓，已开持仓的止盈止损继续有效')
            self._save()
            return copy.deepcopy(self.data['strategy'])

    def _strategy_candidates(self):
        eligible = []
        for aid in self.data['strategy']['remaining_accounts']:
            try:
                self._eligible(aid,for_strategy=True)
                eligible.append(aid)
            except ValidationError:
                pass
        return eligible

    def bridge_preparation(self, body):
        """Compatibility response for retired preparation windows; no side effects."""
        return {'active':False,'accounts':[],'disabled':True}

    def _batch_side_preferences(self, members):
        cfg=self.data['strategy']['config']
        return [cfg.get('live_side','random') if self._is_live_account(self._account(aid)) else cfg['side'] for aid in members]

    def _strategy_batch_side(self, symbol):
        """Persist once; recover legacy batches from observed executions, never reroll."""
        s=self.data['strategy'];members=set(s.get('batch_account_ids',[]))
        if not s.get('run_id'):s['run_id']=uuid.uuid4().hex
        saved=s.get('batch_directions',{}).get(symbol);known={saved} if saved in ('buy','sell') else set()
        for p in self.data['positions'].values():
            if p['account_id'] in members and p.get('source')=='strategy' and p.get('symbol')==symbol:
                known.add(p['side'])
        day_start=entry_day_window(self.clock())['start']
        for aid in members:
            records=[]
            for j in self.data['jobs'].values():
                if j['account_id']!=aid or j.get('source')!='strategy' or j.get('type')!='open' or j.get('symbol')!=symbol:continue
                if j.get('strategy_run_id')==s.get('run_id') and j.get('strategy_batch_number')==s['batch_number']:
                    if j.get('delivered_at') is not None:known.add(j['side'])
                elif saved is None and not j.get('strategy_run_id') and (aid not in s.get('order_queue',[]) or j['status']=='queued'):
                    if j.get('created_at',0)>=day_start and (j.get('delivered_at') is not None or j['status']=='queued'):records.append(j)
            if saved is None and aid not in s.get('order_queue',[]):
                records += [t for t in self.data['history'] if t['account_id']==aid and t.get('source')=='strategy' and
                            t.get('symbol')==symbol and isinstance(t.get('opened_at'),(int,float)) and t['opened_at']>=day_start]
            if records:known.add(max(records,key=lambda x:x.get('opened_at',x.get('created_at',0)))['side'])
        if len(known)>1:
            raise ValidationError('同批同品种已存在方向冲突，禁止继续开仓，请核对原批次')
        if saved is not None and saved not in ('buy','sell'):
            raise ValidationError('已保存的批次方向无效，禁止继续开仓')
        if saved is None:
            try:
                preferences=self._batch_side_preferences(members)+list(known)
                saved=choose_batch_side(s['config'].get('strategy_id','random_entry'),self.rng,preferences)
            except ValueError as exc:raise ValidationError(str(exc)) from None
            s.setdefault('batch_directions',{})[symbol]=saved
        return saved

    def _assert_strategy_batch_side(self, aid, order):
        s=self.data['strategy'];symbol=order.get('symbol')
        if (s['status'] not in self.ACTIVE or aid not in s.get('batch_account_ids',[]) or symbol!=s['config'].get('symbol') or
            (order.get('strategy_run_id') is not None and order['strategy_run_id']!=s.get('run_id')) or
            (order.get('strategy_batch_number') is not None and order['strategy_batch_number']!=s.get('batch_number'))):
            raise ValidationError('策略开仓不属于当前批次，禁止派发')
        if order.get('side')!=self._strategy_batch_side(symbol):
            raise ValidationError('同批同品种必须同向，已拦截反向开仓')

    def _strategy_tick(self, now):
        s = self.data['strategy']
        if s['status'] not in self.ACTIVE:
            return
        if self._pending_profit_claims():return
        for claim in self.data['profit_claims'].values():
            if claim['status']=='verified':self._schedule_claim_reentry(claim)
        cfg = s['config']
        if s['phase'] == 'monitoring':
            if any(self._strategy_outstanding(a) for a in s['batch_account_ids']):
                return
            s['completed_accounts'].extend(s['batch_account_ids'])
            s['phase'] = 'cooldown'
            s['status'] = 'waiting'
            s['next_action_at'] = now+self.rng.uniform(cfg['batch_delay_min'], cfg['batch_delay_max'])
            self._log('info', f"第 {s['batch_number']} 批全部结束，进入随机批间等待")
            return
        if s['next_action_at'] is not None and now < s['next_action_at']:
            return
        if s['phase'] == 'cooldown':
            s['phase'] = 'next_batch'
        if s['phase'] == 'next_batch':
            eligible = self._strategy_candidates()
            if len(eligible) < cfg['batch_size']:
                if any(self._account(a).get('phase_pending') or
                       (self._account(a).get('phase') and self._account(a).get('initial_balance') is None)
                       for a in s['remaining_accounts']):
                    s.update({'status':'waiting','next_action_at':now+1})
                    return
                s['status'] = 'completed'
                s['next_action_at'] = None
                s['upcoming_account_ids'] = []
                s['preparation'] = {'status':'inactive','accounts':{}}
                self._log('success', '策略完成：剩余可用账户不足一批，不重复使用账户')
                return
            batch = choose_live_first(cfg.get('strategy_id','random_entry'),self.rng,eligible,cfg['batch_size'],
                lambda aid:self._account(aid).get('phase')=='live' or self._account(aid).get('category')=='live')
            try:
                side=choose_batch_side(cfg.get('strategy_id','random_entry'),self.rng,self._batch_side_preferences(batch))
            except ValueError as exc:
                s.update(status='paused',next_action_at=None)
                self._log('error',f'策略暂停，整批未开仓：{exc}')
                return
            s['batch_directions']={cfg['symbol']:side}
            s['remaining_accounts'] = [a for a in s['remaining_accounts'] if a not in batch]
            s['batch_account_ids'] = list(batch)
            s['order_queue'] = list(batch)
            s['batch_number'] += 1
            s['phase'] = 'submitting'
            s['status'] = 'running'
            s['upcoming_account_ids'] = []
            self._log('info', f"第 {s['batch_number']} 批：随机选定 {len(batch)} 个账户；{cfg['symbol']} 统一{'做多' if side=='buy' else '做空'}")
        if s['phase'] == 'submitting':
            # Accounts can be locked while the per-account jitter is running.
            s['order_queue'] = [a for a in s['order_queue'] if not self._account(a).get('strategy_locked')
                                and not self._health_locked(self._account(a))
                                and self._account(a)['enabled'] and self._account(a)['status']=='active']
            if not s['order_queue']:
                s['phase'] = 'monitoring'
                s['next_action_at'] = None
                return
            # Browser failures/unknowns freeze globally before another order.
            s['order_queue'].sort(key=lambda aid:0 if self._account(aid).get('phase')=='live' or self._account(aid).get('category')=='live' else 1)
            aid = s['order_queue'][0]
            if self._account(aid).get('phase_pending'):
                return
            order_cfg = {k: cfg[k] for k in ('symbol','quantity','size_percent','take_profit','stop_loss','skip_stop_loss_on_low_drawdown','drawdown_stop_loss_threshold') if k in cfg}
            if self._is_live_account(self._account(aid)):
                for key in ('take_profit','stop_loss','skip_stop_loss_on_low_drawdown','drawdown_stop_loss_threshold'):
                    order_cfg[key]=cfg.get('live_'+key,STRATEGY_FORM_DEFAULTS['live_'+key])
                if 'size_percent' in order_cfg:order_cfg['size_percent']=cfg.get('live_size_percent',25)
            try:
                order_cfg['side']=self._strategy_batch_side(cfg['symbol'])
                self._open_one(aid, order_cfg, 'strategy')
            except ValidationError as exc:
                s['status'] = 'paused'
                s['next_action_at'] = None
                self._log('error', f'策略暂停：{exc}')
                return
            s['order_queue'].pop(0)
            if s['order_queue']:
                s['next_action_at'] = now+self.rng.uniform(cfg['order_delay_min'], cfg['order_delay_max'])
            else:
                s['phase'] = 'monitoring'
                s['next_action_at'] = None

    def tick(self, price=None):
        with self.lock:
            now = self.clock()
            self._roll_strategy_locks(now)
            self._expire_execution_sessions()
            market = self.data['market']
            # Paper market is independent from the currently displayed mode.
            if price is not None:
                new_price = positive(price, '行情价格')
            else:
                new_price = max(1, round(market['price'] + self.rng.gauss(0, 2.7), 2))
            market['price'] = new_price
            market['change'] = round(new_price-self.data['candles'][0]['open'], 2)
            market['change_pct'] = round(market['change']/self.data['candles'][0]['open']*100, 2)
            market['high'] = max(market['high'], new_price)
            market['low'] = min(market['low'], new_price)
            market['updated_at'] = now
            candle_time = int(now//60)*60
            candles = self.data['candles']
            if candles[-1]['time'] < candle_time:
                candles.append({'time': candle_time, 'open': candles[-1]['close'], 'high': new_price,
                                'low': new_price, 'close': new_price, 'volume': 0})
                candles[-1]['high'] = max(candles[-1]['open'], new_price)
                candles[-1]['low'] = min(candles[-1]['open'], new_price)
            else:
                candles[-1]['high'] = max(candles[-1]['high'], new_price)
                candles[-1]['low'] = min(candles[-1]['low'], new_price)
                candles[-1]['close'] = new_price
            candles[-1]['volume'] += self.rng.randint(1, 20)
            self.data['candles'] = candles[-500:]
            for position in list(self.data['positions'].values()):
                account = self._account(position['account_id'])
                if execution_excluded(account):
                    continue
                if position['mode']=='paper':
                    pnl = round((new_price-position['entry_price'])*position['quantity']*(1 if position['side']=='buy' else -1), 2)
                    position['mark_price'] = new_price
                    position['unrealized_pnl'] = pnl
                    account['unrealized_pnl'] = pnl
                    account['equity'] = round(account['balance']+pnl, 2)
                    reason = self._position_exit_reason(account,position)
                    if reason:
                        self._close_paper(position, reason)
                elif not (self._execution_capable(position['account_id']) if account.get('auto_discovered') else self._ready(position['account_id'])):
                    if account.get('phase_pending'):
                        continue
                    if self.data['strategy']['status'] in self.ACTIVE:
                        self.data['strategy']['status'] = 'paused'
                        self.data['strategy']['next_action_at'] = None
                        self._log('error', f"{account['name']} 桥接断开或读数失效，策略暂停；请在 Vest 核对持仓")
            for job in self.data['jobs'].values():
                if job['status']=='delivered' and now-job['delivered_at'] > 30:
                    job['status'] = 'unknown'
                    self._freeze(job['account_id'], '浏览器操作超过 30 秒仍未确认，请在 Vest 核对，不会自动重试。')
                elif job['status']=='queued' and now-job['created_at'] > (300 if job.get('session_id') else 30):
                    job['status'] = 'cancelled'
                    self._freeze(job['account_id'], '浏览器请求未送达且已过期，策略暂停，请检查连接。')
            self._strategy_tick(now)
            self.data['history'] = self.data['history'][-5000:]
            self._save()

    def _snapshot(self, raw):
        if not isinstance(raw, dict):
            return {}
        health = number(raw.get('account_health'))
        return {'balance': number(raw.get('balance')), 'equity': number(raw.get('equity')),
                **({'account_health': health} if health is not None and 0 <= health <= 100 else {}),
                **({'phase':raw['phase']} if raw.get('phase') in PHASES else {}),
                'entry_price': number(raw.get('entry_price')), 'quantity': number(raw.get('quantity')),
                'side': raw.get('side') if raw.get('side') in ('buy','sell') else None,
                'symbol': raw.get('symbol') if isinstance(raw.get('symbol'),str) else None,
                'position_id': str(raw.get('position_id'))[:160] if raw.get('position_id') else None,
                **{key:raw[key] for key in ('native_sl','native_tp','native_sl_absent') if isinstance(raw.get(key),bool)},
                **{key:number(raw[key]) for key in ('native_sl_price','native_tp_price') if key in raw},
                'unrealized_pnl': number(raw.get('unrealized_pnl')),
                'price': number(raw.get('price')), 'position_open': raw.get('position_open') if isinstance(raw.get('position_open'), bool) else None,
                'observed_at': observed_time(raw.get('observed_at'))}

    @staticmethod
    def _matches_position(position, snapshot):
        return (snapshot.get('position_id') == position.get('browser_position_id')
                and snapshot.get('symbol') == position['symbol']
                and snapshot.get('side') == position['side']
                and snapshot.get('quantity') is not None
                and math.isclose(snapshot['quantity'],position['quantity'],rel_tol=1e-9,abs_tol=1e-9))

    def _observe_browser_price(self, snapshot):
        last = self.data.get('browser_market')
        observed = snapshot['observed_at']
        if last and observed < last['observed_at']:
            return
        price = snapshot['price']
        minute = int(observed//60)*60
        candles = self.data['browser_candles']
        if not candles or candles[-1]['time'] < minute:
            candles.append({'time':minute,'open':price,'high':price,'low':price,'close':price,'volume':0})
        elif candles[-1]['time']==minute:
            candles[-1]['high'] = max(candles[-1]['high'],price)
            candles[-1]['low'] = min(candles[-1]['low'],price)
            candles[-1]['close'] = price
        self.data['browser_candles'] = candles[-720:]
        self.data['browser_market'] = {'price':price,'observed_at':observed}

    @staticmethod
    def _observation_money(raw, name):
        value = raw.get(name)
        if value is None:
            return None
        if isinstance(value,bool) or not isinstance(value,(int,float)):
            raise ValidationError(f'只读观察 {name} 必须是有限数字或 null')
        if abs(value)>1e15 or not math.isfinite(value):
            raise ValidationError(f'只读观察 {name} 数值超出范围')
        return float(value)

    def _capital_metrics(self, raw, previous=None, *, category='', phase=None):
        """Independent DOM readings; never substitute local trading limits."""
        if raw is None:
            raw = {}
        if not isinstance(raw,dict) or set(raw)-set(self.CAPITAL_METRICS):
            raise ValidationError('账户指标只接受 daily_loss / drawdown / goal / health 对象')
        result = {}
        previous = previous or {}
        for key in self.CAPITAL_METRICS:
            prior = previous.get(key)
            incoming = raw.get(key)
            if phase and isinstance(prior,dict) and prior.get('phase')!=phase:
                prior=None
            if phase and isinstance(incoming,dict) and incoming.get('phase') not in (None,phase):
                incoming=None
            if incoming is None:
                result[key] = {**copy.deepcopy(prior),'update_pending':True} if isinstance(prior,dict) else None
                continue
            if not isinstance(incoming,dict):
                raise ValidationError('账户指标必须为对象或 null')
            text = incoming.get('text')
            stamp = self._observation_money(incoming,'observed_at')
            values = {name:self._observation_money(incoming,name) for name in ('current','limit','percent')}
            pair = values['current'] is not None and values['limit'] is not None and values['limit']>=0 and values['percent'] is None
            percentage = values['current'] is None and values['limit'] is None and values['percent'] is not None and values['percent']>=0
            valid_reading = (percentage and values['percent']<=100) if key=='health' else (pair or percentage)
            metric_category = incoming.get('category',category)
            if (not isinstance(text,str) or not 1<=len(text)<=160 or stamp is None or not 0<stamp<=self.clock()+5
                or not valid_reading or not isinstance(metric_category,str) or len(metric_category)>120
                or ('update_pending' in incoming and not isinstance(incoming['update_pending'],bool))):
                raise ValidationError('账户指标文本、金额、比例或观察时间无效')
            if isinstance(prior,dict) and prior['observed_at']>stamp:
                result[key] = copy.deepcopy(prior)
                continue
            pending = incoming.get('update_pending',False)
            if isinstance(prior,dict) and prior['observed_at']==stamp:
                pending = pending or prior.get('update_pending',False)
            result[key] = {**values,'text':' '.join(text.replace(self.bridge_token,'[redacted]').split()),
                           'observed_at':stamp,'category':metric_category,'source':'vest-page','update_pending':pending,
                           **({'phase':phase} if phase else {})}
        return result

    def observe_accounts(self, body):
        """Store DOM observations without touching the trading account graph.

        A display key is not a broker account ID and cannot be used by any
        order or account mutation. Reading somebody's open position here does
        not create a managed position, a job, a lock or a strategy transition.
        """
        with self.lock:
            if not isinstance(body,dict):
                raise ValidationError('只读观察必须是 JSON 对象')
            tab_url = body.get('tab_url')
            if not isinstance(tab_url,str) or len(tab_url)>2048:
                raise ValidationError('只读观察页面地址无效')
            try:
                parsed = urlparse(tab_url)
                valid_url = (parsed.scheme=='https' and parsed.hostname=='next.vestmarkets.com'
                             and parsed.port in (None,443) and not parsed.username and not parsed.password
                             and parsed.path.rstrip('/') in NQ_TRADE_PATHS)
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValidationError('只读观察仅接受 https://next.vestmarkets.com/trade/NQ-PERP 页面')
            raw_accounts = body.get('accounts')
            if not isinstance(raw_accounts,list) or len(raw_accounts)>100:
                raise ValidationError('只读观察 accounts 必须为最多 100 项的数组')
            error = body.get('error','')
            if not isinstance(error,str) or len(error)>1000:
                raise ValidationError('只读观察 error 必须为不超过 1000 个字符的文本')
            error = error.replace(self.bridge_token,'[redacted]')
            now = self.clock()
            rows = []
            duplicate_counts = {}
            for raw in raw_accounts:
                if not isinstance(raw,dict):
                    raise ValidationError('只读账户记录必须是 JSON 对象')
                name, category = raw.get('name'), raw.get('category','')
                phase=raw.get('phase')
                if phase is not None and phase not in PHASES:
                    raise ValidationError('阶段只支持 1/2、2/2、live 或 null')
                if not isinstance(name,str) or not name.strip() or len(name)>160:
                    raise ValidationError('只读账户名称需为 1–160 个字符')
                if not isinstance(category,str) or len(category)>120:
                    raise ValidationError('只读账户类别需为不超过 120 个字符的文本')
                if not isinstance(raw.get('current'),bool):
                    raise ValidationError('只读账户 current 必须是布尔值')
                observed = raw.get('observed_at')
                if observed is not None:
                    if isinstance(observed,bool) or not isinstance(observed,(int,float)) or abs(observed)>1e15 or not math.isfinite(observed):
                        raise ValidationError('只读账户 observed_at 必须是有效时间戳或 null')
                    observed = observed/1000 if observed>1e12 else float(observed)
                    if observed<=0 or observed>now+5:
                        raise ValidationError('只读账户观察时间戳无效或来自未来')
                status_observed=raw.get('status_observed_at',observed)
                if status_observed is not None:
                    if (isinstance(status_observed,bool) or not isinstance(status_observed,(int,float))
                        or not math.isfinite(status_observed) or not 0<status_observed<=now+5):
                        raise ValidationError('账户状态观察时间戳无效或来自未来')
                phase_stamp=raw.get('phase_observed_at',status_observed) if phase else None
                if phase and (isinstance(phase_stamp,bool) or not isinstance(phase_stamp,(int,float))
                              or number(phase_stamp) is None or not 0<phase_stamp<=now+5):
                    raise ValidationError('阶段观察时间戳无效')
                phase_snapshot=self._snapshot(raw.get('phase_snapshot')) if raw.get('phase_snapshot') is not None else None
                if raw.get('phase_snapshot') is not None and not isinstance(raw['phase_snapshot'],dict):
                    raise ValidationError('阶段空仓观察必须为对象')
                identity = raw.get('identity')
                if identity is not None and (not isinstance(identity,str) or len(identity)>256):
                    raise ValidationError('只读账户 identity 格式无效')
                read_error = raw.get('read_error','')
                if not isinstance(read_error,str) or len(read_error)>500:
                    raise ValidationError('只读账户 read_error 格式无效')
                read_error = read_error.replace(self.bridge_token,'[redacted]')
                equity = self._observation_money(raw,'equity')
                upnl = self._observation_money(raw,'unrealized_pnl')
                reported_balance = self._observation_money(raw,'balance')
                balance = None
                problems = [read_error] if read_error else []
                if equity is not None and upnl is not None:
                    derived = round(equity-upnl,8)
                    if reported_balance is None or math.isclose(reported_balance,derived,rel_tol=1e-12,abs_tol=.01000001):
                        balance = derived
                    else:
                        problems.append('余额与净值减浮盈亏不一致，未采纳余额')
                elif reported_balance is not None:
                    problems.append('未读取完整净值与浮盈亏，余额无法核验')
                if equity is None and upnl is None:
                    problems.append('该账户金额尚未读取')
                name, category = name.strip(), category.strip()
                prior_metrics = {}
                cached = [a for a in self.data['accounts'].values() if a.get('mode')=='browser' and not a.get('merged_into')]
                cached += self.data['read_only_observation'].get('accounts',[])
                for cached_account in cached:
                    if cached_account.get('name')!=name:
                        continue
                    if phase and cached_account.get('phase')!=phase:
                        previous=cached_account.get('phase_books',{}).get(phase,{})
                        metrics=previous.get('capital_metrics',{})
                    elif phase is None and cached_account.get('phase'):
                        continue
                    else:
                        metrics=cached_account.get('capital_metrics',{})
                    for key,metric in metrics.items():
                        if isinstance(metric,dict) and (key not in prior_metrics or metric['observed_at']>=prior_metrics[key]['observed_at']):
                            prior_metrics[key] = metric
                capital_metrics = self._capital_metrics(raw.get('capital_metrics'),prior_metrics,category=category,phase=phase)
                duplicate_group = (name,category)
                ordinal = duplicate_counts.get(duplicate_group,0)+1
                duplicate_counts[duplicate_group] = ordinal
                digest = hashlib.sha256(json.dumps([name,category,ordinal],ensure_ascii=False).encode('utf-8')).hexdigest()[:20]
                rows.append({'key':'observed-'+digest,'name':name,'category':category,'capital_metrics':capital_metrics,
                             'phase':phase,'phase_observed_at':phase_stamp,'phase_snapshot':phase_snapshot,
                             'equity':equity,'balance':balance,'unrealized_pnl':upnl,
                             'current':raw['current'],'observed_at':observed,'status_observed_at':status_observed,'source':'vest-page',
                             'read_error':'；'.join(problems),'ambiguous':False})
            # Duplicate labels are separate observations, never guessed IDs.
            for row in rows:
                if duplicate_counts[(row['name'],row['category'])]>1:
                    row['ambiguous'] = True
                    row['read_error'] = '；'.join(filter(None,[row['read_error'],'账户同名且类别相同，仅按扫描序号展示，不能作为交易身份']))
            progress = body.get('progress')
            if progress is not None:
                if not isinstance(progress,dict):
                    raise ValidationError('只读扫描 progress 必须为对象或 null')
                completed, total = progress.get('completed',0), progress.get('total',len(rows))
                if any(isinstance(v,bool) or not isinstance(v,int) or not 0<=v<=100 for v in (completed,total)) or completed>total:
                    raise ValidationError('只读扫描进度计数无效')
                progress_name = progress.get('name','')
                if not isinstance(progress_name,str) or len(progress_name)>160:
                    raise ValidationError('只读扫描进度名称无效')
                progress = {'completed':completed,'total':total,'name':progress_name.replace(self.bridge_token,'[redacted]')}
                for flag in ('done','restored'):
                    if flag in body['progress']:
                        if not isinstance(body['progress'][flag],bool):
                            raise ValidationError('只读扫描进度标记必须为布尔值')
                        progress[flag] = body['progress'][flag]
            self.data['read_only_observation'] = {'last_seen':now,'error':error,'accounts':rows,
                                                  'tab_url':parsed.geturl(),'progress':progress}
            self._save()
            return {'ok':True,'accounts_received':len(rows),'server_time':now}

    def account_observation(self):
        """Keep last observed amounts visible; freshness never grants execution."""
        with self.lock:
            observation = self.data['read_only_observation']
            now = self.clock()
            last_seen = observation.get('last_seen',0)
            connected = bool(last_seen and 0<=now-last_seen<=30)
            rows = copy.deepcopy(observation.get('accounts',[]))
            for row in rows:
                observed = row.get('observed_at')
                row['execution_excluded'] = execution_excluded(row) or any(a['name']==row['name'] and execution_excluded(a)
                    for a in self.data['accounts'].values() if not a.get('merged_into'))
                stale = not connected or observed is None or not 0<=now-observed<=30
                row['stale'] = stale
                row['update_pending'] = stale
                for field in ('equity','balance','unrealized_pnl'):
                    row['observed_'+field] = row[field]
                    if observed is None:
                        row[field] = None
            return {'connected':connected,'last_seen':last_seen,'error':observation.get('error',''),
                    'accounts':rows,'progress':copy.deepcopy(observation.get('progress'))}

    def _revoke_execution(self, session, reason):
        """Revoke future clicks, never pretend a delivered action was recalled."""
        session['mode'] = 'readonly'
        for claim in self._pending_profit_claims():
            if claim['session_id']!=session['session_id']:continue
            if claim['status']=='preparing':claim.update(status='queued',mode_epoch=None)
            elif claim['status']=='submitted':claim.update(status='unknown',error='执行许可停止时领取结果未核验，禁止重复提交')
        affected = {a for a in session.get('account_ids',[]) if self.data['accounts'].get(a,{}).get('session_id')==session['session_id']}
        for job in self.data['jobs'].values():
            if job['account_id'] in affected and job['status']=='queued':
                job['status'] = 'cancelled'
                job['cancel_reason'] = reason
                position = self.data['positions'].get(job.get('local_position_id'))
                if position and position.get('status')=='closing':
                    position['status'] = 'open'
        for aid in affected:
            if aid in self.data['bridge_sessions']:
                self.data['bridge_sessions'][aid]['ready'] = False
        strategy = self.data['strategy']
        if strategy['status'] in self.ACTIVE and affected.intersection(strategy['config'].get('account_ids',[])):
            strategy['status'] = 'paused'
            strategy['order_queue'] = []
            strategy['next_action_at'] = None
            self._log('warning', reason+'；相关策略暂停，已送达的操作仍需核对')

    def _expire_execution_sessions(self):
        now = self.clock()
        changed = False
        for session in self.data['execution_sessions'].values():
            if session.get('mode')=='execute' and not 0<=now-session.get('last_seen',0)<=30:
                self._revoke_execution(session,'浏览器执行连接已过期，自动切回只读')
                changed = True
        return changed

    @staticmethod
    def _mode_epoch(value):
        if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=9007199254740991:
            raise ValidationError('mode_epoch 必须是非负整数')
        return value

    def _execution_identity(self, body, account, *, result=False, job=None):
        if not account.get('auto_discovered'):
            return
        if result:
            if job.get('session_id') != body.get('session_id'):
                raise ValidationError('回执与原执行会话不匹配',409)
            return
        if body.get('session_id') != account['session_id']:
            raise ValidationError('浏览器会话与账户不匹配',409)
        session = self.data['execution_sessions'].get(account['session_id'])
        if not session:
            raise ValidationError('浏览器会话已失效，请在插件中重新连接',409)
        epoch = self._mode_epoch(body.get('mode_epoch'))
        if epoch != session['mode_epoch']:
            raise ValidationError('插件模式已更新，请使用最新模式版本',409)
        if body.get('identity') != account['identity'] or account['id'] not in session.get('account_ids',[]):
            raise ValidationError('账户身份与已读取列表不匹配',409)
        if account.get('phase') and body.get('phase')!=account['phase']:
            raise ValidationError('账户阶段已变化，请先同步当前阶段',409)
        if account.get('ambiguous') or not account.get('available'):
            raise ValidationError('账户身份不唯一或已不在当前列表中',409)

    def bridge_session(self, body):
        """Automatically discover accounts; only an explicit mode switch grants execution."""
        with self.lock:
            sid = body.get('session_id')
            if not isinstance(sid,str) or not 8<=len(sid)<=200 or any(ord(c)<33 for c in sid):
                raise ValidationError('session_id 需为 8–200 个非空白字符')
            requested = body.get('mode')
            if requested not in ('readonly','execute'):
                raise ValidationError('插件模式必须为 readonly 或 execute')
            epoch = self._mode_epoch(body.get('mode_epoch',0))
            authorize = body.get('authorize',False)
            if not isinstance(authorize,bool):
                raise ValidationError('authorize 必须是布尔值')
            heartbeat_only = body.get('heartbeat_only',False)
            if not isinstance(heartbeat_only,bool):
                raise ValidationError('heartbeat_only 必须是布尔值')
            execution_error = body.get('execution_error','')
            if not isinstance(execution_error,str) or len(execution_error)>500:
                raise ValidationError('execution_error 必须是不超过 500 字符的文本')
            execution_error = execution_error.replace(self.bridge_token,'[redacted]').strip()
            self._expire_execution_sessions()
            scope = self._account_scope(sid)
            if requested=='execute' and authorize:
                requested_names = {r['name'].strip() for r in body.get('accounts',[]) if isinstance(r,dict) and isinstance(r.get('name'),str)} if isinstance(body.get('accounts'),list) else set()
                other_active = any(a.get('auto_discovered') and not a.get('merged_into') and not execution_excluded(a) and a['name'] in requested_names
                    and a.get('session_id')!=sid and self.data['execution_sessions'].get(a.get('session_id'),{}).get('mode')=='execute'
                    for a in self.data['accounts'].values())
                protected = any(a.get('auto_discovered') and not a.get('merged_into') and a['name'] in requested_names
                    and a.get('session_id')!=sid and self._pending(a['id']) for a in self.data['accounts'].values())
                if other_active or protected:
                    raise ValidationError('同名账户正在另一窗口执行或有待确认操作，请先在原窗口切回只读并完成核对',409)
            prior = self.data['execution_sessions'].get(sid)
            prior_epoch = prior.get('mode_epoch',0) if prior else -1
            if epoch<prior_epoch:
                raise ValidationError('插件模式版本已过期，忽略迟到的模式控制消息',409)
            # Reuse the already strict URL/money/null/duplicate validation.
            observation_body = {'tab_url':body.get('tab_url'),'accounts':body.get('accounts',[]),
                                'error':body.get('error',''),'progress':body.get('progress')}
            self.observe_accounts(observation_body)
            rows = self.data['read_only_observation']['accounts']
            scanning = self.data['read_only_observation']['progress'] is not None
            names = {}
            for row in rows:
                names[row['name']] = names.get(row['name'],0)+1
            now = self.clock()
            session = prior or {'session_id':sid,'mode':'readonly','mode_epoch':epoch,
                                'account_ids':[],'cursor':0,'last_seen':0}
            old_ids = set(session['account_ids'])
            ids = []
            account_phases={}
            for raw, row in zip(body.get('accounts',[]), rows):
                matches = [a for a in self.data['accounts'].values() if a.get('auto_discovered') and not a.get('merged_into')
                    and a['name']==row['name']]
                if len(matches)>1:
                    for conflict in matches:
                        conflict.update({'dedup_conflict':True,'ambiguous':True})
                        if conflict['id'] not in ids:
                            ids.append(conflict['id'])
                    row.update({'ambiguous':True,'account_id':None,'account_scope':scope,'auto_discovered':True})
                    continue
                aid = matches[0]['id'] if matches else 'vest-'+hashlib.sha256(row['name'].encode('utf-8')).hexdigest()[:24]
                if aid in ids:
                    continue
                ids.append(aid)
                identity = row['category']+':'+row['name']
                account = self.data['accounts'].get(aid)
                if account is None:
                    account = {'id':aid,'name':row['name'],'mode':'browser','auto_discovered':True,
                        'session_id':sid,'account_scope':scope,'identity':identity,'category':row['category'],
                        'balance':None,'initial_balance':None,'profit_target':None,'max_loss':None,
                        'baseline_source':'first_observed','realized_pnl':None,'unrealized_pnl':None,
                        'equity':None,'status':'active','enabled':True,'lock_reason':None,
                        'created_at':now,'observed_at':None,'external_position':False}
                    self._strategy_lock_defaults(account)
                    self.data['accounts'][aid] = account
                account_phases[aid]=row.get('phase')
                phase_ok=self._apply_phase_observation(account,row) if names[row['name']]==1 else False
                if not phase_ok and (account.get('phase') or row.get('phase')):
                    account.update({'available':True,'ambiguous':names[row['name']]>1})
                    if row['category'].lower() in ('failed','closed') and row.get('phase') in (None,account.get('phase')):
                        stamp=row.get('status_observed_at')
                        if stamp is not None and abs(now-stamp)<=5 and not account['ambiguous'] and not execution_excluded(account):
                            self._finalize_failed_account(account,'platform',{'name':row['name'],'category':row['category'].lower(),'observed_at':stamp})
                    row.update({'account_id':aid,'account_scope':scope,'auto_discovered':True,'phase_stale':True})
                    continue
                if account['identity'] != identity and row['category'].lower() not in ('passed','failed','closed') and (self._position(aid) or self._pending(aid)):
                    self._freeze(aid,f"{account['name']} 平台账户状态或身份变化，请核对现有仓位和操作")
                owner = self.data['execution_sessions'].get(account.get('session_id'),{})
                owner_fresh = 0<=now-owner.get('last_seen',0)<=30
                if account.get('session_id')!=sid and not self._pending(aid) and ((requested=='execute' and authorize and epoch>prior_epoch)
                    or (not owner_fresh and not self._position(aid))):
                    account['session_id']=sid
                    account['account_scope']=scope
                    self.data['bridge_sessions'].pop(aid,None)
                if row['category'].lower()=='passed' and account.get('category','').lower()!='passed':
                    account['evaluation_passed_at'] = now
                account.update({'identity':identity,'category':row['category'],
                    'ambiguous':names[row['name']]>1,'available':True})
                self._exclude_primary_account(account)
                self._enforce_retired_failure(account)
                if not account['ambiguous']:
                    account['capital_metrics'] = self._capital_metrics(row.get('capital_metrics'),account.get('capital_metrics'),category=row['category'],phase=account.get('phase'))
                if names[row['name']]>1:
                    row['ambiguous'] = True
                if not account['ambiguous'] and row['observed_at'] is not None and row['observed_at'] >= (account.get('observed_at') or 0):
                    account['observed_at'] = row['observed_at']
                    for field in ('balance','equity','unrealized_pnl'):
                        account[field] = row[field]
                    if row['balance'] is not None:
                        if account['initial_balance'] is None:
                            account['initial_balance'] = row['balance']
                        account['realized_pnl'] = round(row['balance']-account['initial_balance'],2)
                category = row['category'].lower()
                status_stamp=row.get('status_observed_at')
                if (category in ('failed','closed') and not account['ambiguous'] and not execution_excluded(account)
                    and status_stamp is not None and abs(now-status_stamp)<=5
                    and status_stamp >= (account.get('status_observed_at') or 0)):
                    self._finalize_failed_account(account,'platform',{'name':row['name'],'category':category,'observed_at':status_stamp})
                if status_stamp is not None and status_stamp >= (account.get('status_observed_at') or 0):
                    account['status_observed_at']=status_stamp
                if category=='passed':
                    if not account.get('lock_reason'):
                        account['status'] = 'passed'
                    self._lock_strategy(account,'评估阶段已通过','evaluation_passed')
                elif category in ('failed','closed','disabled'):
                    account['status'] = 'locked'
                    account['lock_reason'] = '平台账户状态为 '+row['category']
                self._enforce_retired_failure(account)
                self._enforce_archived_account(account)
                proof = row.get('phase_snapshot') or {}
                if (scanning and raw.get('scan_status')=='success' and account.get('session_id')==sid
                    and abs(now-proof.get('observed_at',0))<=5
                    and not any(j['status'] in ('delivered','unknown') for j in self._pending(aid))):
                    self._observe_snapshot_health(account, proof)
                row['account_id'] = aid if not account['ambiguous'] else None
                row['account_scope'] = scope
                row['auto_discovered'] = True
            for aid in old_ids-set(ids):
                account = self.data['accounts'][aid]
                if account.get('session_id')!=sid:
                    continue
                account['available'] = False
                if self._position(aid) or self._pending(aid):
                    self._freeze(aid,f"{account['name']} 已从页面账户列表消失，请核对现有仓位")
            # Same-epoch heartbeats can maintain, but never elevate permission.
            continuing = session.get('mode')=='execute' and epoch==prior_epoch
            fresh_authorization = authorize and epoch>prior_epoch
            mode = 'execute' if requested=='execute' and (continuing or fresh_authorization) else 'readonly'
            if session.get('mode')=='execute' and mode=='readonly':
                source=body.get('revocation_source')
                reason=('插件执行检查失败：'+execution_error if execution_error else
                        '页面刷新或脚本重新连接，执行已切回只读' if source=='page-actor-restore' else
                        '执行授权轮次不一致，已切回只读' if requested=='execute' else '插件已切换只读模式')
                self.data['audit'].append({'event':'execution_permission_revoked','time':now,
                    'session_id':sid,'previous_epoch':prior_epoch,'requested_epoch':epoch,
                    'requested_mode':requested,'source':source if source in ('page-actor-restore','local-service-unavailable','execution-check-failed') else 'mode-request',
                    'error':execution_error})
                self.data['audit']=self.data['audit'][-1000:]
                self._revoke_execution(session,reason)
            if execution_error and mode=='readonly':
                session['error'] = execution_error
            elif fresh_authorization and mode=='execute':
                session['error'] = ''
            session.update({'mode':mode,'mode_epoch':epoch,'last_seen':now,'account_ids':ids,
                            'account_phases':account_phases,
                            'tab_url':body['tab_url']})
            self.data['execution_sessions'][sid] = session
            next_account = None
            phase_reader=next((a for a in ids if self._account(a).get('session_id')==sid and self._account(a).get('phase_pending') and self._execution_capable(a)),None)
            uncertain = any(j.get('session_id')==sid and j['status'] in ('delivered','unknown') for j in self.data['jobs'].values())
            if mode=='execute' and not heartbeat_only and (not uncertain or phase_reader):
                jobs = sorted((j for j in self.data['jobs'].values() if j.get('session_id')==sid and j['status']=='queued'),
                              key=lambda j:(j['type']!='close',not self._is_live_account(self._account(j['account_id'])),j['created_at']))
                candidates = [j['account_id'] for j in jobs]
                if not candidates:
                    positions = [a for a in ids if self._position(a)]
                    pool = positions
                    if pool:
                        cursor = session.get('cursor',0)%len(pool)
                        candidates = pool[cursor:]+pool[:cursor]
                        session['cursor'] = (cursor+1)%len(pool)
                aid = phase_reader or next((a for a in candidates if self._account(a).get('session_id')==sid and self._execution_capable(a)),None)
                if aid:
                    account = self.data['accounts'][aid]
                    next_account = {k:account[k] for k in ('id','name','category','identity')}
                    next_account['phase']=account.get('phase')
                    next_account['operation'] = 'monitor' if phase_reader==aid else next((j['type'] for j in jobs if j['account_id']==aid),'monitor')
            self.data['read_only_observation']['session_id'] = sid
            profit_claim=self._claim_route(sid) if mode=='execute' and not heartbeat_only and not uncertain else None
            if profit_claim and not any(j['type']=='close' for j in jobs):
                a=self._account(profit_claim['account_id'])
                next_account={k:a.get(k) for k in ('id','name','category','identity','phase')}
                next_account['operation']='profit-claim'
            else:profit_claim=None
            self._save()
            accounts = [a for a in self.state()['accounts'] if a['id'] in ids]
            return {'ok':True,'mode':mode,'mode_epoch':epoch,'session_id':sid,
                    'accounts':accounts,'next_account':next_account,'profit_claim':profit_claim,'preparation':{'active':False,'accounts':[],'disabled':True}}

    def bridge_inspect(self, body):
        """Store a current DOM snapshot for explicit human reconciliation only."""
        with self.lock:
            account=self._account(body.get('account_id'))
            if account.get('mode')!='browser' or not account.get('auto_discovered'):
                raise ValidationError('只读核对只接受已发现的浏览器账户')
            sid=body.get('session_id')
            caller=self.data['execution_sessions'].get(sid,{}) if isinstance(sid,str) else {}
            owner=self.data['execution_sessions'].get(account.get('session_id'),{})
            foreign_active=(sid!=account.get('session_id') and owner.get('mode')=='execute' and 0<=self.clock()-owner.get('last_seen',0)<=30)
            if body.get('name')!=account['name'] or foreign_active or (sid!=account.get('session_id') and account['id'] not in caller.get('account_ids',[])):
                raise ValidationError('只读核对账户或会话不匹配',409)
            parsed=urlparse(str(body.get('tab_url','')))
            if (parsed.scheme!='https' or parsed.hostname!='next.vestmarkets.com' or parsed.port not in (None,443)
                or parsed.username or parsed.password or parsed.path.rstrip('/') not in NQ_TRADE_PATHS):
                raise ValidationError('只读核对需要 Vest NQ 页面')
            snapshot=self._snapshot(body.get('snapshot'))
            if account.get('phase') and snapshot.get('phase')!=account['phase']:
                raise ValidationError('只读快照与当前账户阶段不一致',409)
            if (abs(self.clock()-snapshot['observed_at'])>5 or snapshot.get('symbol')!='NQ-PERP'
                or not isinstance(snapshot.get('position_open'),bool)
                or any(number(snapshot.get(k)) is None for k in ('balance','equity','unrealized_pnl','price'))
                or snapshot['price']<=0 or abs(snapshot['equity']-snapshot['balance']-snapshot['unrealized_pnl'])>.03):
                raise ValidationError('只读核对需要新鲜、完整且一致的账户及持仓读数')
            if snapshot['position_open'] and (not snapshot.get('position_id') or snapshot.get('side') not in ('buy','sell')
                or not snapshot.get('quantity') or snapshot['quantity']<=0 or not snapshot.get('entry_price') or snapshot['entry_price']<=0):
                raise ValidationError('只读核对持仓标识、方向或数量不完整')
            # An active execution owner must not lose a live protection snapshot
            # to an unrelated inspection. This route is for frozen/read-only work.
            if self._execution_capable(account['id']) and account.get('status')!='locked':
                raise ValidationError('账户仍在正常执行中，请使用执行窗口的实时检查',409)
            self.data['bridge_sessions'][account['id']]={'snapshot':snapshot,'last_seen':self.clock(),
                'ready':False,'tab_url':parsed.geturl(),'session_id':account['session_id'],'source':'readonly-inspection'}
            self._save()
            return {'ok':True,'read_only':True,'account_id':account['id'],'position_open':snapshot['position_open']}

    def bridge_poll(self, body, *, dispatch=True):
        with self.lock:
            aid = body.get('account_id')
            account = self._account(aid)
            if account['mode'] != 'browser':
                raise ValidationError('桥接只接受浏览器账户')
            self._exclude_primary_account(account)
            self._expire_execution_sessions()
            self._execution_identity(body,account)
            reply = {'job':None,'mode':self.data['mode']}
            if not dispatch:
                # This route can save observations and queue protection. It
                # cannot reserve or return a job, even if the response is lost.
                if not account.get('auto_discovered'):
                    raise ValidationError('持仓监控需要有效的插件会话',409)
                lease = self.data['execution_sessions'][account['session_id']]
                reply.update(mode=lease['mode'],mode_epoch=lease['mode_epoch'])
                if lease['mode']!='execute':
                    return reply
            if account.get('phase_pending'):
                return {**reply,'phase_sync_pending':True}
            parsed = urlparse(str(body.get('tab_url', '')))
            if parsed.scheme != 'https' or parsed.hostname != 'next.vestmarkets.com':
                raise ValidationError('只允许连接 https://next.vestmarkets.com 页面')
            snapshot = self._snapshot(body.get('snapshot'))
            if account.get('phase') and snapshot.get('phase')!=account['phase']:
                raise ValidationError('页面快照与账户阶段不一致，请重新同步',409)
            session = {'last_seen': self.clock(), 'snapshot': snapshot, 'ready': body.get('ready') is True,
                       'tab_url': parsed.geturl(), 'error': str(body.get('error',''))[:200]}
            if account.get('auto_discovered'):
                session.update({'session_id':account['session_id'],'identity':account['identity'],'mode_epoch':body['mode_epoch']})
            self.data['bridge_sessions'][aid] = session
            if self._fresh(aid):
                if snapshot.get('symbol') == 'NQ-PERP':
                    self._observe_browser_price(snapshot)
                account['balance'] = snapshot['balance']
                if account['initial_balance'] is None:
                    account['initial_balance'] = snapshot['balance']
                account['realized_pnl'] = round(account['balance']-account['initial_balance'], 2)
                account['unrealized_pnl'] = snapshot['unrealized_pnl']
                account['equity'] = round(account['balance']+account['unrealized_pnl'], 2)
                position = self._position(aid)
                if position:
                    if snapshot['position_open'] is True:
                        if not self._matches_position(position, snapshot):
                            self._freeze(aid, f"{account['name']} 持仓标识、方向或数量变化，请到 Vest 人工核对")
                            self._save()
                            return {**reply,'server_time':self.clock()}
                        position['unrealized_pnl'] = snapshot['unrealized_pnl']
                        position['mark_price'] = snapshot['price']
                        if position['status']=='open' and not self._pending(aid) and self._ready(aid):
                            pnl = position['unrealized_pnl']
                            reason = self._position_exit_reason(account,position)
                            if reason:
                                self._new_job('close', aid, position=position, reason=reason)
                                position['status'] = 'closing'
                    elif snapshot['position_open'] is False:
                        pending = self._pending(aid)
                        if not pending or all(j['type']=='close' and j['status']=='queued' for j in pending):
                            # Native target/stop may close before our queued
                            # protection is dispatched. Do not send another close.
                            reason = 'target' if account.get('status')=='passed' or account.get('category')=='passed' or any(j.get('reason')=='target' for j in pending) else 'external'
                            for closing in pending:
                                closing['status']='cancelled'
                                closing['cancel_reason']='页面已确认空仓，取消尚未派发的平仓'
                            self._finish_browser_close(position,snapshot,reason)
                elif snapshot['position_open'] is True and not self._pending(aid):
                    if account.get('auto_discovered'):
                        account['external_position'] = True
                    else:
                        self._freeze(aid, f"{account['name']} 页面存在工具未管理的持仓，请先人工处理")
                elif snapshot['position_open'] is False:
                    account['external_position'] = False
                self._account_status(account)
            elif self._position(aid) and self.data['strategy']['status'] in self.ACTIVE:
                self.data['strategy']['status'] = 'paused'
                self.data['strategy']['next_action_at'] = None
                self._log('error', f"{account['name']} 读数不完整或未启用桥接，策略暂停")
            actor_busy = dispatch and account.get('auto_discovered') and any(
                j.get('session_id')==account['session_id'] and j['status'] in ('delivered','unknown')
                for j in self.data['jobs'].values())
            if dispatch and self.data['mode']=='browser' and self._ready(aid) and not actor_busy:
                for candidate in self.data['jobs'].values():
                    if candidate['account_id'] != aid or candidate['status']!='queued':
                        continue
                    if candidate['type']=='open' and self._pending_profit_claims():continue
                    if candidate['type']=='open' and self._health_locked(account):
                        candidate.update(status='cancelled', cancel_reason='账户健康度当日锁定')
                        continue
                    if candidate['type']=='open' and candidate.get('source')=='strategy':
                        try:self._assert_strategy_batch_side(aid,candidate)
                        except ValidationError as exc:
                            candidate.update(status='cancelled',cancel_reason=str(exc))
                            self._freeze(aid,f'策略开仓派发已拦截：{exc}')
                            break
                    if candidate['type']=='open' and self._daily_entry_used(aid,exclude_job_id=candidate['id']):
                        candidate['status']='cancelled'
                        candidate['cancel_reason']='本交易日已使用一次开仓额度'
                        continue
                    if candidate['type']=='open' and (account.get('status')!='active' or (candidate.get('source')=='strategy' and account.get('strategy_locked'))):
                        candidate['status'] = 'cancelled'
                        candidate['cancel_reason'] = account.get('strategy_lock_reason') or '账户不可开仓'
                        continue
                    if candidate['type']=='open' and not self._ready(aid, opening=True):
                        candidate['status'] = 'cancelled'
                        self._freeze(aid, '页面未确认空仓，已取消开仓请求')
                        break
                    if candidate['type']=='close' and snapshot['position_open'] is not True:
                        candidate['status'] = 'unknown'
                        self._freeze(aid, '待平仓时页面未显示明确持仓，请人工核对')
                        break
                    if candidate['type']=='close':
                        position = self.data['positions'].get(candidate.get('local_position_id'))
                        if not position or not self._matches_position(position,snapshot):
                            candidate['status'] = 'unknown'
                            self._freeze(aid, '待平仓标识、方向或数量不一致，操作未发送')
                            break
                    if candidate['type']=='open' and candidate.get('skip_stop_loss_on_low_drawdown'):
                        try:
                            self._strategy_stop_decision(candidate,account,(account.get('capital_metrics') or {}).get('drawdown'))
                        except ValidationError:
                            # No action is delivered while the metric is missing,
                            # stale, or belongs to another phase. Retry reads only.
                            continue
                    if self.clock()-candidate['created_at'] > (300 if candidate.get('session_id') else 30):
                        candidate['status'] = 'cancelled'
                        self._freeze(aid, '开平仓请求已过期，未发送浏览器')
                        break
                    candidate['status'] = 'delivered'
                    candidate['delivered_at'] = self.clock()
                    candidate['balance_before'] = snapshot['balance']
                    reply['job'] = copy.deepcopy(candidate)
                    break
            self._save()
            return {**reply,'server_time':self.clock()}

    def _finish_browser_close(self, position, snapshot, reason):
        account = self._account(position['account_id'])
        # Reconcile observed cash balance. No inferred/simulated execution price.
        pnl = round(snapshot['balance']-position['balance_before'], 2)
        trade = {k: position[k] for k in ('id','account_id','account_name','symbol','side','quantity','entry_price','opened_at','source','mode')}
        trade['phase']=position.get('phase')
        trade.update({'exit_price': snapshot['price'], 'pnl': pnl, 'closed_at': self.clock(),
                      'reason': reason, 'price_source': 'observed_mark', 'pnl_source': 'observed_balance_change'})
        self.data['history'].append(trade)
        account['balance'] = snapshot['balance']
        account['unrealized_pnl'] = snapshot['unrealized_pnl']
        account['equity'] = round(account['balance']+account['unrealized_pnl'], 2)
        account['realized_pnl'] = round(account['balance']-account['initial_balance'], 2)
        del self.data['positions'][position['id']]
        self._account_status(account)
        self._observe_snapshot_health(account, snapshot)
        self._lock_strategy_exit(position,reason,pnl)
        self._queue_profit_claim(position,trade,snapshot)
        self._log('info', f"{account['name']} 平仓已观察确认，余额变化 {pnl:+.2f} USDC")

    def bridge_size(self, body):
        """Pin the page-calculated quantity before a percentage entry click."""
        with self.lock:
            job = self.data['jobs'].get(body.get('job_id')) if isinstance(body.get('job_id'),str) else None
            if not job or job['type']!='open' or job['status']!='delivered' or job.get('size_percent') not in (25,50,75,100) or self.clock()-job['delivered_at']>30:
                raise ValidationError('不存在可确认仓位大小的开仓任务',409)
            account = self._account(job['account_id'])
            self._assert_execution_allowed(account)
            if body.get('account_id')!=account['id']:
                raise ValidationError('仓位大小与任务账户不匹配',409)
            self._execution_identity(body,account)
            if account['status']!='active' or account.get('strategy_locked') or account.get('phase_pending') or not account['enabled']:
                raise ValidationError('账户已锁定、通过或停用，禁止开仓',409)
            if self._daily_entry_used(account['id'],exclude_job_id=job['id']):
                raise ValidationError('本交易日已有其他开仓，禁止重复开仓',409)
            if not (self._execution_capable(account['id']) if account.get('auto_discovered') else self._ready(account['id'],opening=True)):
                raise ValidationError('执行权限已失效，禁止确认仓位大小',409)
            parsed = urlparse(str(body.get('tab_url','')))
            if parsed.scheme!='https' or parsed.hostname!='next.vestmarkets.com' or parsed.path.rstrip('/') not in NQ_TRADE_PATHS:
                raise ValidationError('只能从 Vest NQ-PERP 页面确认仓位大小')
            if isinstance(body.get('size_percent'),bool) or body.get('size_percent')!=job['size_percent']:
                raise ValidationError('页面仓位比例与策略不一致',409)
            snapshot = self._snapshot(body.get('snapshot'))
            if account.get('phase') and snapshot.get('phase')!=account['phase']:
                raise ValidationError('页面快照与账户阶段不一致，禁止开仓',409)
            if (snapshot.get('position_open') is not False or self._position(account['id'])
                or abs(self.clock()-snapshot['observed_at'])>5
                or any(snapshot.get(k) is None for k in ('balance','unrealized_pnl','price')) or snapshot['price']<=0):
                raise ValidationError('确认仓位大小需要新鲜且明确空仓的页面读数',409)
            if self._health_locked(account):
                self._save()
                return {'ok':True, 'job_id':job['id'], 'skip_reason':'account_health'}
            quantity = positive(body.get('quantity'),'页面计算数量',100000)
            if job.get('quantity') is not None and not math.isclose(job['quantity'],quantity,rel_tol=1e-9,abs_tol=1e-9):
                raise ValidationError('本任务已固定数量，禁止变更或重新计算',409)
            policy = {}
            if job.get('skip_stop_loss_on_low_drawdown'):
                stop, remaining = self._strategy_stop_decision(job,account,body.get('drawdown'))
                if 'stop_loss' not in body or body['stop_loss']!=stop or isinstance(body['stop_loss'],bool):
                    raise ValidationError('页面止损设置与剩余最大回撤规则不一致，未提交订单',409)
                if job.get('sized_at') is not None and job['stop_loss']!=stop:
                    raise ValidationError('本任务止损决策已固定，回撤条件变化后禁止重算或提交',409)
                policy = dict(stop_loss=stop,stop_loss_omitted=stop is None,
                    remaining_drawdown_at_entry=remaining,drawdown_at_entry=copy.deepcopy(body['drawdown']))
            job.update({'quantity':quantity,'sized_at':self.clock(),'sizing_source':'vest_percentage_button'})
            job.update(policy)
            self._save()
            return {'ok':True,'job_id':job['id'],'quantity':quantity,'size_percent':job['size_percent'],**policy}

    def bridge_stop_protection(self, body):
        """Reserve one native amendment for a delivered, actually filled entry."""
        with self.lock:
            job=self.data['jobs'].get(body.get('job_id')) if isinstance(body.get('job_id'),str) else None
            if not job or job['type']!='open' or job['status']!='delivered' or not 0<=self.clock()-job['delivered_at']<=90:
                raise ValidationError('不存在可校正止损的已派发开仓任务',409)
            account=self._account(job['account_id'])
            if body.get('account_id')!=account['id']:
                raise ValidationError('止损校正与任务账户不匹配',409)
            self._assert_execution_allowed(account)
            self._execution_identity(body,account)
            if not self._execution_capable(account['id']) or job.get('session_id')!=body.get('session_id') or job.get('mode_epoch')!=body.get('mode_epoch'):
                raise ValidationError('止损校正执行授权已失效',409)
            parsed=urlparse(str(body.get('tab_url','')))
            if parsed.scheme!='https' or parsed.hostname!='next.vestmarkets.com' or parsed.path.rstrip('/') not in NQ_TRADE_PATHS:
                raise ValidationError('止损校正必须来自 Vest NQ 交易页面',403)
            snap=self._snapshot(body.get('snapshot'))
            if (snap.get('position_open') is not True or not snap.get('position_id') or
                snap.get('symbol')!=job['symbol'] or snap.get('side')!=job['side'] or
                number(job.get('quantity')) is None or number(snap.get('quantity')) is None or
                not math.isclose(snap['quantity'],job['quantity'],rel_tol=1e-9,abs_tol=1e-9) or
                abs(self.clock()-snap['observed_at'])>5 or snap['observed_at']<job['delivered_at'] or
                job.get('phase')!=snap.get('phase')):
                raise ValidationError('止损校正需要同账户、阶段和真实成交仓位的新鲜快照',409)
            if job.get('stop_loss') is None:
                raise ValidationError('本单配置不设止损，禁止添加止损',409)
            try:plan=stop_plan(snap,job['stop_loss'])
            except ValueError as exc:raise ValidationError(str(exc),409) from None
            if not (snap.get('price') and (snap['price']>plan['target_price'] if snap['side']=='buy' else snap['price']<plan['target_price'])):
                raise ValidationError('行情已越过目标止损价，禁止自动修改',409)
            if number(body.get('target_price')) is None or not math.isclose(number(body['target_price']),plan['target_price'],abs_tol=1e-8,rel_tol=0):
                raise ValidationError('止损触发价与真实成交风险计算不符',409)
            if plan['status']=='verified':
                return {'ok':True,'authorize_click':False,**plan}
            # Existing intent is never another authorization, including after a
            # lost reply or a service restart. Keep the fill receipt independent.
            if job.get('stop_amendment'):
                return {'ok':True,'authorize_click':False,'target_price':plan['target_price']}
            job['stop_amendment']={**plan,'position_id':snap['position_id'],'entry_price':snap['entry_price'],
                'quantity':snap['quantity'],'submitted_at':self.clock(),'status':'intent'}
            self._save()
            return {'ok':True,'authorize_click':True,'target_price':plan['target_price']}

    def bridge_result(self, body):
        with self.lock:
            jid = body.get('job_id')
            job = self.data['jobs'].get(jid) if isinstance(jid, str) else None
            if not job:
                raise ValidationError('操作不存在', 404)
            reported_account = body.get('account_id')
            seen = set()
            while isinstance(reported_account,str) and reported_account not in seen and self.data['accounts'].get(reported_account,{}).get('merged_into'):
                seen.add(reported_account)
                reported_account = self.data['accounts'][reported_account]['merged_into']
            if reported_account and reported_account != job['account_id']:
                raise ValidationError('操作与账户不匹配', 409)
            self._execution_identity(body,self._account(job['account_id']),result=True,job=job)
            if job['status'] in ('archived_failed','archived_phase'):
                if 'late_result' not in job:
                    job['late_result']={'received_at':self.clock(),'success':body.get('success') is True,
                        'confirmed':body.get('confirmed') is True,'snapshot':self._snapshot(body.get('snapshot')),
                        'error':str(body.get('error','')).replace(self.bridge_token,'[redacted]')[:250]}
                    self._save()
                return {'ok':True,'duplicate':True,'status':job['status']}
            if job['status'] == 'confirmed':
                return {'ok': True, 'duplicate': True}
            if job['status']=='cancelled' and job.get('skip_reason')=='account_health':
                return {'ok':True, 'duplicate':True, 'status':'cancelled'}
            if job['status'] != 'delivered':
                raise ValidationError('操作结果已不再接受；请人工核对账户', 409)
            snapshot = self._snapshot(body.get('snapshot'))
            unconfirmed_without_phase = (snapshot.get('phase') is None and
                (body.get('success') is not True or body.get('confirmed') is not True))
            if job.get('phase') and snapshot.get('phase')!=job['phase'] and not unconfirmed_without_phase:
                raise ValidationError('回执阶段与原订单不同，请先同步当前阶段',409)
            complete = (all(number(snapshot.get(k)) is not None for k in ('balance','unrealized_pnl','price'))
                        and snapshot['price'] > 0 and abs(self.clock()-snapshot['observed_at']) <= 5)
            expected = job['type']=='open'
            account = self._account(job['account_id'])
            if body.get('skip_reason')=='account_health':
                health = snapshot.get('account_health', 100)
                if (not expected or not complete or snapshot.get('position_open') is not False
                    or snapshot.get('phase') != job.get('phase') or snapshot.get('phase') != account.get('phase')
                    or snapshot['observed_at'] < job['delivered_at'] or job.get('stop_amendment')
                    or body.get('success') is not False or body.get('confirmed') is not False
                    or (health >= self.MIN_ACCOUNT_HEALTH and not self._health_locked(account))):
                    raise ValidationError('健康度跳过回执缺少原账户的新鲜空仓证明',409)
                job.update(status='cancelled', skip_reason='account_health', cancel_reason='插件因账户健康度跳过开仓')
                self._save()
                return {'ok':True, 'status':'cancelled'}
            if expected:
                complete = (complete and snapshot.get('entry_price') is not None and snapshot['entry_price'] > 0
                            and bool(snapshot.get('position_id')) and snapshot.get('symbol') == job['symbol']
                            and snapshot.get('side') == job['side'] and snapshot.get('quantity') is not None
                            and number(job.get('quantity')) is not None
                            and math.isclose(snapshot['quantity'],job['quantity'],rel_tol=1e-9,abs_tol=1e-9))
            if body.get('success') is not True or body.get('confirmed') is not True or not complete or snapshot['position_open'] is not expected:
                job['status'] = 'unknown'
                job['error'] = str(body.get('error', '未观察到明确的成交/平仓结果'))[:250]
                self._freeze(job['account_id'], f"浏览器操作结果未确认：{job['error']}；不自动重试，请到 Vest 核对。")
                self._save()
                return {'ok': False, 'status': 'unknown', 'error': job['error']}
            session = self.data['bridge_sessions'].get(account['id'], {})
            previous = session.get('snapshot', {})
            if job['type']=='open':
                cfg = {k: job[k] for k in ('symbol','side','quantity','take_profit','stop_loss')}
                if 'size_percent' in job:
                    cfg['size_percent'] = job['size_percent']
                for key in ('configured_stop_loss','skip_stop_loss_on_low_drawdown','drawdown_stop_loss_threshold','stop_loss_omitted','remaining_drawdown_at_entry','strategy_run_id','strategy_batch_number'):
                    if key in job:
                        cfg[key] = job[key]
                position = {'id': uuid.uuid4().hex, 'account_id': account['id'], 'account_name': account['name'],
                            'phase':job.get('phase'),
                            **cfg, 'entry_price': snapshot['entry_price'], 'mark_price': snapshot['price'],
                            'unrealized_pnl': snapshot['unrealized_pnl'], 'opened_at': self.clock(),
                            'source': job['source'], 'mode': 'browser', 'status': 'open',
                            'balance_before': job.get('balance_before', previous.get('balance', account['balance'])),
                            'price_source': 'observed_entry', 'browser_position_id': snapshot['position_id']}
                self.data['positions'][position['id']] = position
                self._log('success', f"{account['name']} 浏览器已观察到开仓，已记录页面入场价")
                raw=body.get('snapshot') or {}
                if 'native_sl_price' in raw or 'stop_check' in raw:
                    try:protection=stop_plan(snapshot,job.get('stop_loss'))
                    except ValueError as exc:protection={'status':'failed','error':str(exc)}
                    reported=raw.get('stop_check')
                    if isinstance(reported,dict) and reported.get('status')=='failed':
                        protection.update(status='failed',error=str(reported.get('error','止损校正未确认'))[:250])
                    if job.get('stop_amendment'):
                        if snapshot.get('position_id')!=job['stop_amendment']['position_id'] or not math.isclose(snapshot['entry_price'],job['stop_amendment']['entry_price'],rel_tol=0,abs_tol=1e-8):
                            protection.update(status='failed',error='止损校正回执的持仓或成交均价变化')
                        job['stop_amendment']['status']='verified' if protection['status']=='verified' else 'unknown'
                    position['stop_protection']={**protection,'observed_at':snapshot['observed_at'],
                        'trigger_price':snapshot.get('native_sl_price'),
                        'corrected':bool(job.get('stop_amendment')) and protection['status']=='verified'}
                    job['stop_check']=copy.deepcopy(position['stop_protection'])
                    if protection['status'] not in ('verified','omitted'):
                        self._freeze(account['id'],f"{account['name']} 开仓已确认，但止损金额校验失败：{protection.get('error','实际止损与配置不一致')}；保留持仓并暂停后续开仓，请核对")
                    else:
                        self._log('info',f"{account['name']} 成交后止损{'已校正并' if job.get('stop_amendment') else ''}核验：实际风险 {protection.get('actual_risk')} USDC，配置 {job.get('stop_loss')} USDC")
            else:
                position = self.data['positions'].get(job['local_position_id'])
                if not position:
                    job['status'] = 'unknown'
                    self._freeze(account['id'], '平仓结果与本地持仓不一致，请人工核对')
                    self._save()
                    return {'ok': False, 'status': 'unknown'}
                self._finish_browser_close(position, snapshot, job['reason'])
            job['status'] = 'confirmed'
            job['confirmed_at'] = self.clock()
            session.update({'last_seen': self.clock(), 'snapshot': snapshot})
            self.data['bridge_sessions'][account['id']] = session
            account['balance'] = snapshot['balance']
            account['unrealized_pnl'] = snapshot['unrealized_pnl']
            account['equity'] = round(account['balance']+account['unrealized_pnl'], 2)
            account['realized_pnl'] = round(account['balance']-account['initial_balance'], 2)
            self._save()
            return {'ok': True, 'status': 'confirmed'}

    def _archive_failed_account(self, account, source):
        if account.get('archived') or not account.get('retired_failed') or execution_excluded(account):
            return False
        account.update({'archived':True,'archived_at':self.clock(),'archive_source':source,'archive_kind':'terminal'})
        self._log('info',f"{account['name']} 已{'自动' if source=='automatic' else '手动'}归档，原始信息和历史记录保留")
        return True

    def _finalize_failed_account(self, account, source, evidence=None):
        """A proven terminal account cannot keep blocking unrelated batches.

        Preserve uncertain receipts and positions as evidence, never fabricate
        a fill, flat snapshot or PnL. Keep the active strategy's phase and lease.
        """
        if account.get('retired_failed'):
            return 0
        if execution_excluded(account):
            raise ValidationError('主账户永久排除执行，不能作为失败账户归档',403)
        aid=account['id']; now=self.clock()
        terminal='closed' if (evidence or {}).get('category')=='closed' else 'failed'
        description='已关闭' if terminal=='closed' else '评估失败'
        jobs=self._pending(aid)
        original_jobs=copy.deepcopy(jobs)
        original_account=copy.deepcopy(account)
        already_archived=bool(account.get('archived'))
        positions=[copy.deepcopy(p) for p in self.data['positions'].values() if p['account_id']==aid]
        for job in jobs:
            if job['status']=='queued':
                job.update({'status':'cancelled','cancel_reason':f'账户已确认{description}，取消未派发请求'})
            else:
                job.update({'original_status':job['status'],'status':'archived_failed','archived_at':now,
                            'archive_reason':f'账户已确认{description}；归档异常记录，不作为成交或平仓确认'})
        for position in positions:
            del self.data['positions'][position['id']]
        account.update({'retired_failed':True,'failure_confirmed_at':now,'failure_confirmation_source':source,
                        'failure_evidence':copy.deepcopy(evidence),'terminal_status':terminal,'archived':already_archived})
        self._enforce_retired_failure(account)
        if aid in self.data['bridge_sessions']:
            self.data['bridge_sessions'][aid]['ready']=False
        strategy=self.data['strategy']
        for key in ('remaining_accounts','order_queue'):
            strategy[key]=[a for a in strategy.get(key,[]) if a!=aid]
        if strategy.get('phase')=='submitting' and not strategy['order_queue']:
            strategy.update({'phase':'monitoring','next_action_at':None})
        if source=='user' or self.data['preferences']['auto_archive_failed']:
            self._archive_failed_account(account,'user' if source=='user' else 'automatic')
        self.data['audit'].append({'event':'user_confirmed_evaluation_failure' if source=='user' else 'platform_evaluation_failure',
            'time':now,'account_id':aid,'account_name':account['name'],'account_before':original_account,
            'original_jobs':original_jobs,'positions_archived_without_execution':positions,'source':source,
            'evidence':copy.deepcopy(evidence),'acknowledgement':
            '用户明确确认该账户评估失败；仅归档异常并永久停用，不确认成交或补造损益' if source=='user' else
            f'页面明确显示账户{description}；保留未知成交及持仓记录，不生成推测损益，其他账户按原批次继续'})
        self.data['audit']=self.data['audit'][-1000:]
        self._log('warning',f"{account['name']} 已确认{description}并永久停用；旧任务和持仓已留存审计，不再阻塞其他账户")
        return len(original_jobs)

    def archive_account(self, aid, body):
        with self.lock:
            if body.get('acknowledge') is not True:
                raise ValidationError('请明确确认归档该账户')
            account=self._account(aid)
            if execution_excluded(account):
                raise ValidationError('主账户保持只读展示，不能归档',403)
            if account.get('archived'):
                return {'ok':True,'archived':True,'duplicate':True}
            if account.get('retired_failed'):
                changed=self._archive_failed_account(account,'user')
                if changed:
                    self.data['audit'].append({'event':'manual_account_archive','time':self.clock(),'account_id':aid})
                    self._save()
                return {'ok':True,'archived':True,'duplicate':not changed}
            if body.get('terminal_status') is not None:
                if body['terminal_status']!='failed':
                    raise ValidationError('仅支持显式确认 failed 终止状态')
                result=self._retire_failed_account(account)
                return {**result,'archived':True}
            if account.get('mode')!='browser' or not account.get('auto_discovered'):
                raise ValidationError('仅支持归档已读取的 Vest 账户')
            jobs=self._pending(aid)
            if any(j['status']!='queued' or j['type']!='open' or j.get('delivered_at') is not None for j in jobs):
                raise ValidationError('仍有未确认交易，请先核对；已确认评估失败时可使用「确认失败并归档」',409)
            snapshot=self.data['bridge_sessions'].get(aid,{}).get('snapshot',{})
            fresh_flat=self._fresh(aid) and snapshot.get('position_open') is False
            if (self._position(aid) or snapshot.get('position_open') is True
                or (account.get('external_position') and not fresh_flat)):
                raise ValidationError('账户仍有持仓记录，请先结束持仓后再归档',409)
            original=copy.deepcopy(account)
            original_jobs=copy.deepcopy(jobs)
            for job in jobs:
                job.update({'status':'cancelled','cancel_reason':'账户已手动归档，取消尚未派发的开仓'})
            account.update({'archived':True,'archived_at':self.clock(),'archive_source':'user',
                            'archive_kind':'manual','enabled':False})
            strategy=self.data['strategy']
            for key in ('remaining_accounts','order_queue'):
                strategy[key]=[a for a in strategy.get(key,[]) if a!=aid]
            if strategy.get('phase')=='submitting' and not strategy['order_queue']:
                strategy.update({'phase':'monitoring','next_action_at':None})
            if aid in self.data['bridge_sessions']:
                self.data['bridge_sessions'][aid]['ready']=False
            self.data['audit'].append({'event':'manual_account_archive','time':self.clock(),'account_id':aid,
                'account_name':account['name'],'account_before':original,'cancelled_queued_jobs':original_jobs,
                'acknowledgement':'用户确认手动归档；保留平台状态和历史，不声明评估失败，不生成成交或损益'})
            self.data['audit']=self.data['audit'][-1000:]
            self._log('info',f"{account['name']} 已手动归档并退出执行，平台状态及历史保留，其他账号继续原策略")
            self._save()
            return {'ok':True,'archived':True,'archive_kind':'manual','cancelled_jobs':len(jobs)}

    def _retire_failed_account(self, account):
        """Administrative disposition of an inaccessible, user-confirmed failed account.

        This is not an execution/flat confirmation. Preserve original receipts
        and daily usage, disable the account, and record the user's statement.
        """
        if account.get('retired_failed'):
            return {'ok':True,'terminal_status':'failed','duplicate':True,'archived_jobs':0}
        if execution_excluded(account) or not account.get('auto_discovered'):
            raise ValidationError('只能归档已发现的考核账户，不能修改主账户',403)
        if self.data['strategy']['status'] in self.ACTIVE:
            raise ValidationError('请先停止策略，再确认账户终止状态',409)
        if self._position(account['id']):
            raise ValidationError('仍有本地持仓记录，需要新鲜空仓读数完成普通人工核对',409)
        unknown_locked=account.get('category')=='unknown' and account.get('status')=='locked'
        if account.get('available') and account.get('category') not in ('failed','closed','disabled') and not unknown_locked:
            raise ValidationError('账户仍在页面列表中，请通过实时页面读数核对失败状态',409)
        jobs=self._pending(account['id'])
        now=self.clock()
        if any(j.get('delivered_at') is not None and now-j['delivered_at']<=30 for j in jobs):
            raise ValidationError('已送达操作尚在等待回执，不能归档',409)
        count=self._finalize_failed_account(account,'user')
        self._save()
        return {'ok':True,'terminal_status':'failed','archived_jobs':count}

    def _adopt_verified_position(self, account, body):
        """Explicitly adopt observed exposure; never replay or confirm an unknown fill."""
        aid=account['id'];self._assert_execution_allowed(account)
        if self.data['strategy']['status'] in self.ACTIVE:
            raise ValidationError('请先暂停策略再核对接管现有持仓',409)
        job=self.data['jobs'].get(body.get('job_id'))
        if not job or job.get('account_id')!=aid or job.get('type')!='open':
            raise ValidationError('需要该账户原有的开仓请求',409)
        position=self._position(aid)
        if (job.get('resolution')=='adopted_observed_position' and position and
            position.get('browser_position_id')==body.get('position_id')):
            return {'ok':True,'adopted':True,'duplicate':True,'position_id':position['id']}
        if position or job.get('status')!='unknown' or job.get('delivered_at') is None:
            raise ValidationError('仅支持接管唯一已送达但结果未知的开仓请求',409)
        if account.get('phase_pending') or (job.get('phase') and job['phase']!=account.get('phase')):
            raise ValidationError('原请求与当前账户阶段不符，不能接管',409)
        pending=self._pending(aid)
        if len(pending)!=1 or pending[0]['id']!=job['id']:
            raise ValidationError('该账户还有其他未确认操作，不能接管',409)
        session=self.data['bridge_sessions'].get(aid,{})
        owner=self.data['execution_sessions'].get(account.get('session_id'),{})
        if (session.get('source')!='readonly-inspection' or owner.get('mode')!='readonly' or
            session.get('session_id')!=job.get('session_id') or not self._fresh(aid)):
            raise ValidationError('需要同一执行窗口在只读模式下提供5秒内的完整持仓核对读数',409)
        snapshot=session['snapshot']
        if (snapshot.get('position_open') is not True or not body.get('position_id') or
            snapshot.get('position_id')!=body['position_id'] or snapshot.get('symbol')!=job['symbol'] or
            snapshot.get('side')!=job['side'] or number(job.get('quantity')) is None or
            number(snapshot.get('quantity')) is None or not math.isclose(snapshot['quantity'],job['quantity'],rel_tol=1e-9,abs_tol=1e-9) or
            not snapshot.get('entry_price') or snapshot['entry_price']<=0 or snapshot.get('equity') is None or
            abs(snapshot['equity']-snapshot['balance']-snapshot['unrealized_pnl'])>.03):
            raise ValidationError('实际持仓ID、品种、方向、数量、阶段或资金与待核对请求不符',409)
        stop_ok=snapshot.get('native_sl_absent') is True if job.get('stop_loss') is None else snapshot.get('native_sl') is True
        tp_ok=(snapshot.get('native_tp') is False and snapshot.get('native_tp_price') is None) if job.get('take_profit') is None else snapshot.get('native_tp') is True
        if not stop_ok or not tp_ok:
            raise ValidationError('无法核对原策略要求的原生止盈止损状态，不能接管',409)
        now=self.clock();original=copy.deepcopy(job)
        keys=('symbol','side','quantity','take_profit','stop_loss','size_percent','configured_stop_loss',
              'skip_stop_loss_on_low_drawdown','drawdown_stop_loss_threshold','stop_loss_omitted','remaining_drawdown_at_entry')
        position={'id':uuid.uuid4().hex,'account_id':aid,'account_name':account['name'],'phase':job.get('phase'),
            **{key:job[key] for key in keys if key in job},'entry_price':snapshot['entry_price'],
            'mark_price':snapshot['price'],'unrealized_pnl':snapshot['unrealized_pnl'],
            'opened_at':now,'opened_at_source':'verified_existing_position_observation',
            'order_delivered_at':job['delivered_at'],'source':job['source'],'mode':'browser','status':'open',
            'balance_before':job.get('balance_before',snapshot['balance']),
            'price_source':'observed_entry','browser_position_id':snapshot['position_id'],'reconciled_position':True}
        self.data['positions'][position['id']]=position
        job.update(status='reconciled',original_status='unknown',reconciled_at=now,
                   resolution='adopted_observed_position',local_position_id=position['id'])
        account.update(status='active',lock_reason=None,external_position=False)
        self.data['audit'].append({'event':'manual_position_adoption','time':now,'account_id':aid,
            'account_name':account['name'],'original_job':original,'observed_snapshot':copy.deepcopy(snapshot),
            'position_id':position['id'],'acknowledgement':'明确核对现有持仓并接管；原请求留为已核对而非确认成交，不重放订单或补造历史，开仓时间记为接管观察时间'})
        self.data['audit']=self.data['audit'][-1000:]
        self._log('warning',f"{account['name']} 已核对并接管现有持仓；原开仓请求未重发，策略保持暂停")
        self._save()
        return {'ok':True,'adopted':True,'position_id':position['id'],'status':account['status']}

    def resume_strategy(self, body):
        """Explicitly resume the same monitoring batch or its cooldown."""
        with self.lock:
            s=self.data['strategy']
            if body.get('acknowledge') is not True or not body.get('run_id') or body['run_id']!=s.get('run_id'):
                raise ValidationError('需要明确确认继续当前这一轮策略',409)
            if s['status']!='paused' or s.get('phase') not in ('monitoring','cooldown') or s.get('order_queue'):
                raise ValidationError('当前状态不能继续原监控批次，请核对后重新启动策略',409)
            self._assert_settled_browser_actions()
            symbols={p['symbol'] for p in self.data['positions'].values() if p.get('source')=='strategy' and p['account_id'] in s.get('batch_account_ids',[])}
            for symbol in symbols:self._strategy_batch_side(symbol)
            for aid in s.get('remaining_accounts',[]):self._eligible(aid,for_strategy=True)
            for aid in s.get('batch_account_ids',[]):
                position=self._position(aid)
                if position and position['mode']=='browser':
                    if not self._execution_capable(aid) or not self._fresh(aid) or not self._matches_position(position,self.data['bridge_sessions'][aid]['snapshot']):
                        raise ValidationError('当前批次持仓尚未恢复执行或缺少新鲜核对读数',409)
            if s['phase']=='cooldown':
                if any(self._position(aid) for aid in s.get('batch_account_ids',[])):
                    raise ValidationError('批间等待状态仍有持仓，不能提前开始下一批',409)
                cfg=s['config'];s.update(status='waiting',next_action_at=self.clock()+self.rng.uniform(cfg['batch_delay_min'],cfg['batch_delay_max']))
            else:
                s.update(status='running',next_action_at=None)
            self.data['audit'].append({'event':'manual_strategy_resume','time':self.clock(),'run_id':s['run_id'],
                'phase':s['phase'],'acknowledgement':'明确继续同一轮策略；参数、当前批次、每日额度及剩余账户保持不变'})
            self.data['audit']=self.data['audit'][-1000:]
            self._log('success','原策略已继续：保留当前批次和持仓，按原参数处理剩余账户')
            self._save();return copy.deepcopy(s)

    def reconcile(self, body):
        """Explicit human verification of flatness or terminal evaluation failure.

        This resolves uncertainty; it does not invent executions or restart a
        strategy. The HTTP route is intentionally unavailable to extensions.
        """
        with self.lock:
            if body.get('acknowledge') is not True:
                raise ValidationError('请明确确认人工核对结果或账户评估失败状态')
            aid = body.get('account_id')
            account = self._account(aid)
            if account['mode'] != 'browser':
                raise ValidationError('仅浏览器账户可以人工核对')
            if body.get('adopt_position') is not None:
                if body['adopt_position'] is not True or body.get('terminal_status') is not None:
                    raise ValidationError('持仓接管选项无效')
                return self._adopt_verified_position(account,body)
            if body.get('terminal_status') is not None:
                if body['terminal_status']!='failed':
                    raise ValidationError('仅支持显式确认 failed 终止状态')
                return self._retire_failed_account(account)
            if not self._fresh(aid):
                raise ValidationError(f"需要 5 秒内有效读数；请在 Vest 显示 {account['name']} 的活动持仓面板，保持插件只读，等待读取空仓状态后再核对")
            snapshot = self.data['bridge_sessions'][aid]['snapshot']
            if snapshot['position_open'] is not False or abs(snapshot['unrealized_pnl']) > 1e-8:
                raise ValidationError('页面必须明确空仓且浮动盈亏为零，请先在 Vest 处理所有仓位')
            now = self.clock()
            jobs = [j for j in self.data['jobs'].values() if j['account_id']==aid]
            if any(j['status']=='delivered' and now-j['delivered_at'] <= 30 for j in jobs):
                raise ValidationError('仍有已送达浏览器的操作尚未超时，不能提前解除不确定状态',409)
            archived = [copy.deepcopy(p) for p in self.data['positions'].values() if p['account_id']==aid]
            resolved_jobs = []
            for job in jobs:
                if job['status'] in self.UNRESOLVED or job['status']=='cancelled':
                    job['status'] = 'reconciled'
                    job['reconciled_at'] = now
                    resolved_jobs.append(job['id'])
            for position in archived:
                del self.data['positions'][position['id']]
            self.data['audit'].append({'event':'manual_reconcile','time':now,'account_id':aid,
                'account_name':account['name'],'positions_archived_without_execution':archived,
                'job_ids':resolved_jobs,'observed_balance':snapshot['balance'],
                'acknowledgement':'用户确认已在 Vest 核对并处理全部实际持仓及挂单'})
            self.data['audit'] = self.data['audit'][-1000:]
            account.update({'balance':snapshot['balance'],'unrealized_pnl':snapshot['unrealized_pnl'],
                'equity':round(snapshot['balance']+snapshot['unrealized_pnl'],2),
                'realized_pnl':round(snapshot['balance']-account['initial_balance'],2),
                'status':'active','lock_reason':None})
            if account.get('category')=='passed':
                account['status']='passed'
            elif account.get('category') in ('failed','closed','disabled'):
                account.update({'status':'locked','lock_reason':'平台账户状态为 '+account['category']})
            self._account_status(account)
            strategy = self.data['strategy']
            if strategy['status'] in self.ACTIVE:
                strategy['status'] = 'paused'
            strategy['order_queue'] = []
            strategy['next_action_at'] = None
            self._log('warning', f"{account['name']} 已人工确认空仓并完成核对；归档 {len(archived)} 条本地持仓记录，未生成成交或损益历史；策略不会自动恢复")
            self._save()
            return {'ok':True,'status':account['status'],'archived_positions':len(archived),
                    'resolved_jobs':len(resolved_jobs)}

    def state(self):
        with self.lock:
            expired_locks = self._roll_strategy_locks()
            if self._expire_execution_sessions() or expired_locks:
                self._save()
            now = self.clock()
            d = self.data
            result = {k: copy.deepcopy(d[k]) for k in ('mode','strategy','candles','market','preferences')}
            result['strategies']=catalogue()
            result['capabilities']={'strategy_batch_same_side':True,'live_profit_claims':True}
            result['accounts'] = copy.deepcopy([a for a in d['accounts'].values() if not a.get('merged_into')])
            result['positions'] = copy.deepcopy(list(d['positions'].values()))
            result['history'] = copy.deepcopy(sorted(d['history'], key=lambda h: h['closed_at'], reverse=True))
            result['trading_day'] = trading_day_window(now)
            result['entry_day'] = entry_day_window(now)
            daily = {}
            for trade in d['history']:
                closed = observed_time(trade.get('closed_at'))
                pnl = number(trade.get('pnl'))
                if result['trading_day']['start'] <= closed < result['trading_day']['end'] and closed <= now and pnl is not None:
                    key = (trade['account_id'],trade.get('mode'),trade.get('phase'))
                    total,count = daily.get(key,(0,0))
                    daily[key] = (total+pnl,count+1)
            result['logs'] = copy.deepcopy(list(reversed(d['logs'])))
            connected = [aid for aid, session in d['bridge_sessions'].items() if now-session['last_seen']<=5]
            for execution in d['execution_sessions'].values():
                if 0<=now-execution.get('last_seen',0)<=30:
                    connected.extend(a for a in execution.get('account_ids',[]) if a not in connected)
            result['bridge'] = {'connected_accounts': connected, 'ready_accounts': [a for a in connected if self._ready(a)],
                                'pending_jobs': sum(j['status'] in self.UNRESOLVED for j in d['jobs'].values()),
                                'jobs': [{k: j.get(k) for k in ('id','type','account_id','phase','status','created_at','error')} for j in d['jobs'].values()][-100:]}
            result['bridge']['execution_sessions'] = [{k:s.get(k) for k in ('session_id','mode','mode_epoch','last_seen','account_ids','error')}
                                                       for s in d['execution_sessions'].values()]
            result['bridge']['profit_claims']=copy.deepcopy(list(d['profit_claims'].values())[-100:])
            result['connection'] = {'status': 'connected', 'server_time': now, 'paper': True,
                                    'market_source': 'simulated', 'browser_accounts': len(connected)}
            for account in result['accounts']:
                if account.get('phase'):
                    pnl,count=daily.get((account['id'],account['mode'],account['phase']),(0,0))
                else:
                    entries=[v for k,v in daily.items() if k[:2]==(account['id'],account['mode'])]
                    pnl,count=sum(v[0] for v in entries),sum(v[1] for v in entries)
                account.update({'daily_realized_pnl':round(pnl,2),'daily_trade_count':count})
                self._sync_phase_book(account)
                self._phase_summary(account,d['history'],result['trading_day'])
                account['daily_entry_used'] = self._daily_entry_used(account['id'],window=result['entry_day'])
                account['health_locked'] = self._health_locked(account)
                if not account['health_locked']:
                    account.pop('health_unlock_at', None)
                account['strategy_unlock_at'] = (entry_day_window(account['strategy_locked_at'])['end']
                    if not execution_excluded(account) and not account.get('retired_failed') and not account.get('archived') and account.get('strategy_locked') and account.get('strategy_lock_source')!='live_default' and number(account.get('strategy_locked_at')) is not None else None)
                if account['mode']=='browser':
                    if account.get('auto_discovered'):
                        execution = d['execution_sessions'].get(account['session_id'],{})
                        account['connected'] = account['id'] in connected and account.get('available',False)
                        account['execution_mode'] = execution.get('mode','readonly') if account['connected'] and not execution_excluded(account) and not account.get('retired_failed') and not account.get('archived') else 'readonly'
                        account['mode_epoch'] = execution.get('mode_epoch',0)
                        account['executable'] = (self._execution_capable(account['id']) and account['enabled']
                            and account['status']=='active' and not account.get('external_position'))
                        account['ready'] = account['executable']
                        account['snapshot_ready'] = self._ready(account['id'])
                        account['strategy_executable'] = (account['executable'] and not account.get('strategy_locked',False)
                            and not account['health_locked'] and not account['daily_entry_used'])
                        account['data_stale'] = (not account['connected'] or account.get('observed_at') is None
                            or not 0<=now-account['observed_at']<=30)
                        account['update_pending'] = account['data_stale']
                        continue
                    account['execution_mode'] = 'readonly'
                    account['executable'] = False
                    account['connected'] = account['id'] in connected
                    account['ready'] = self._ready(account['id'])
                    account['observed_at'] = d['bridge_sessions'].get(account['id'], {}).get('snapshot', {}).get('observed_at')
                    account['data_stale'] = not self._fresh(account['id'])
                    if account['data_stale']:
                        account['unrealized_pnl'] = None
                        account['equity'] = None
            if d['mode']=='browser':
                snapshots = [d['bridge_sessions'][a]['snapshot'] for a in connected if self._fresh(a) and d['bridge_sessions'][a]['snapshot'].get('symbol')=='NQ-PERP']
                snapshot = max(snapshots,key=lambda x:x['observed_at']) if snapshots else None
                result['market'] = {'symbol': 'NQ-PERP', 'price': snapshot['price'] if snapshot else None,
                                    'change': None, 'change_pct': None, 'high': None, 'low': None,
                                    'volume': None, 'source': 'browser' if snapshot else 'unavailable',
                                    'point_value': None, 'updated_at': snapshot['observed_at'] if snapshot else None,
                                    'observed_since': d['browser_candles'][0]['time'] if d['browser_candles'] else None}
                result['candles'] = copy.deepcopy(d['browser_candles'])
                result['connection']['market_source'] = result['market']['source']
                for position in result['positions']:
                    if position['mode']=='browser' and not self._fresh(position['account_id']):
                        position['unrealized_pnl'] = None
                        position['mark_price'] = None
                        position['data_stale'] = True
            return result

    def history_csv(self, account_id=None, days=None):
        with self.lock:
            output = io.StringIO(newline='')
            fields = ['id','account_id','account_name','mode','symbol','side','quantity','entry_price','exit_price','pnl','reason','opened_at','closed_at']
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            cutoff = self.clock()-positive(days, '天数', 36500)*86400 if days else 0
            for trade in sorted(self.data['history'], key=lambda h: h['closed_at'], reverse=True):
                if trade['mode'] != self.data['mode'] or (account_id and trade['account_id'] != account_id) or trade['closed_at'] < cutoff:
                    continue
                row = dict(trade)
                row['opened_at'] = datetime.fromtimestamp(row['opened_at'], timezone.utc).isoformat()
                row['closed_at'] = datetime.fromtimestamp(row['closed_at'], timezone.utc).isoformat()
                # Prevent spreadsheet formula injection in user-provided names.
                for k, value in row.items():
                    if isinstance(value, str) and value[:1] in ('=','+','-','@','\t','\r'):
                        row[k] = "'"+value
                writer.writerow(row)
            return '\ufeff'+output.getvalue()
