"""Durable, single-submit Live profit claims through the public browser UI."""
import copy
import math
import uuid
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlparse


class ProfitClaims:
    CLAIM_PENDING = {'queued','preparing','submitted','unknown','balance_mismatch'}
    CLAIM_UNCERTAIN = {'submitted','unknown','balance_mismatch'}
    CLAIM_AUTO_LOCKS = {'take_profit','external_close','daily_completed'}

    def _claim_audit(self, event, claim):
        self.data['audit'].append(dict(event='live_profit_claim_'+event,time=self.clock(),
            **{k:claim.get(k) for k in ('id','trade_id','account_id','run_id','status','mode_epoch','amount','target_balance','balance_after')}))
        self.data['audit']=self.data['audit'][-1000:]

    def _init_profit_claims(self):
        self.data.setdefault('profit_claims',{})
        for claim in self.data['profit_claims'].values():
            if claim['status']=='preparing':claim.update(status='queued',mode_epoch=None)
            elif claim['status']=='submitted':claim.update(status='unknown',error='服务重启时领取结果未核验，禁止重复提交')
            if claim['status'] in self.CLAIM_UNCERTAIN:
                self._freeze(claim['account_id'],'利润领取结果待核验；请在只读模式核对该 Live 号实际余额')

    def _pending_profit_claims(self):
        return [c for c in self.data.get('profit_claims',{}).values() if c['status'] in self.CLAIM_PENDING]

    def _queue_profit_claim(self, position, trade, snapshot):
        account=self._account(position['account_id']);s=self.data['strategy']
        target=account.get('initial_balance')
        if (position.get('mode')!='browser' or position.get('source')!='strategy' or not account.get('auto_discovered') or account.get('phase')!='live'
            or not isinstance(target,(int,float)) or target<=0 or snapshot['balance']-target<=.005
            or s['status'] not in self.ACTIVE or position.get('strategy_run_id')!=s.get('run_id')
            or account['id'] not in s.get('batch_account_ids',[])):
            return
        if any(c['trade_id']==trade['id'] for c in self.data['profit_claims'].values()):return
        cfg=s['config'];cid=uuid.uuid4().hex
        self.data['profit_claims'][cid]=dict(id=cid,trade_id=trade['id'],account_id=account['id'],name=account['name'],
            phase='live',identity=account['identity'],session_id=account['session_id'],run_id=s['run_id'],
            batch_number=s['batch_number'],target_balance=target,status='queued',created_at=self.clock(),
            policy={k:cfg.get(k,self.data['preferences']['strategy_config'][k]) for k in
                    ('live_payout_recipient','live_payout_mode','live_payout_amount','live_payout_percent')})
        self._claim_audit('queued',self.data['profit_claims'][cid])
        self._log('info',f"{account['name']} Live 已空仓且存在净利润，等待领取到主账户并核验初始余额")

    def _claim_reentry_cutoff(self, account):
        cid=account.get('live_reentry_claim_id');claim=self.data.get('profit_claims',{}).get(cid,{})
        return claim.get('verified_at',0) if (account.get('phase')=='live' and claim.get('status')=='verified'
            and claim.get('account_id')==account['id']) else 0

    def _schedule_claim_reentry(self, claim):
        s=self.data['strategy'];a=self._account(claim['account_id'])
        if (claim.get('reentry_scheduled') or s['status'] not in self.ACTIVE or s.get('run_id')!=claim['run_id']
            or a['id'] not in s.get('batch_account_ids',[]) or a.get('strategy_locked') or self._health_locked(a)
            or a.get('status')!='active'):
            return
        if a['id'] not in s['order_queue']:s['order_queue'].append(a['id'])
        s.update(phase='submitting',next_action_at=self.clock()+self.rng.uniform(s['config']['order_delay_min'],s['config']['order_delay_max']))
        claim['reentry_scheduled']=True
        self._log('success',f"{a['name']} 余额已恢复账号规模，保留本批方向，按原延迟继续下一单")

    def _claim_route(self, sid):
        if self.data['strategy']['status'] not in self.ACTIVE:return None
        for c in self._pending_profit_claims():
            a=self._account(c['account_id'])
            if (c['status'] in ('queued','preparing') and c['run_id']==self.data['strategy'].get('run_id')
                and a.get('session_id')==sid and a['name']==c['name'] and a.get('phase')=='live'
                and self._execution_capable(a['id'])):
                # Only a provably unsubmitted claim may follow a freshly
                # authorized replacement page actor. Submitted claims never do.
                if c['status']=='queued':c.update(session_id=sid,identity=a['identity'])
                if c['session_id']==sid:return copy.deepcopy(c)
        return None

    def _claim_authority(self, body, action):
        from .engine import ValidationError
        claim=self.data['profit_claims'].get(body.get('claim_id'))
        if not claim:raise ValidationError('利润领取任务不存在',404)
        a=self._account(claim['account_id']);self._assert_execution_allowed(a)
        self._expire_execution_sessions();self._execution_identity(body,a)
        parsed=urlparse(str(body.get('tab_url','')))
        paths={'/trade/NQ-PERP','/trade/NDX-USD-PERP'} if action in ('reserve','verify') else {'/portfolio','/trade','/trade/NQ-PERP','/trade/NDX-USD-PERP'}
        if (parsed.scheme!='https' or parsed.hostname!='next.vestmarkets.com' or parsed.port not in (None,443)
            or parsed.username or parsed.password or parsed.path.rstrip('/') not in paths):
            raise ValidationError('利润领取页面来源无效',403)
        if (body.get('account_id')!=a['id'] or body.get('name')!=a['name'] or a['name']!=claim['name']
            or a.get('phase')!='live' or a.get('phase_pending') or a.get('identity')!=claim['identity']
            or a.get('session_id')!=claim['session_id'] or not self._execution_capable(a['id'])):
            raise ValidationError('利润领取账户、阶段或执行许可已变化',409)
        s=self.data['strategy']
        if action not in ('heartbeat','verify','fail') and (s['status'] not in self.ACTIVE or s.get('run_id')!=claim['run_id']):
            raise ValidationError('原策略已停止，禁止提交利润领取',409)
        if action in ('reserve','intent') and a.get('strategy_locked') and a.get('strategy_lock_source') not in self.CLAIM_AUTO_LOCKS:
            raise ValidationError('Live 账户已手动锁定或触及风控，禁止领取提交',409)
        return claim,a

    def _claim_flat(self, a, raw, expected=None):
        from .engine import ValidationError
        snap=self._snapshot(raw)
        fields=('balance','equity','unrealized_pnl','price')
        if (any(snap.get(k) is None for k in fields) or snap.get('phase')!='live' or snap['position_open'] is not False
            or snap['price']<=0 or abs(snap['unrealized_pnl'])>1e-8 or abs(snap['equity']-snap['balance'])>.005
            or not 0<=self.clock()-snap['observed_at']<=5 or self._position(a['id']) or self._pending(a['id'])):
            raise ValidationError('领取利润需要同名 Live 号的新鲜明确空仓读数',409)
        if expected is not None and abs(snap['balance']-expected)>.005:
            raise ValidationError('领取后余额未恢复账号初始规模，停止后续下单',409)
        return snap

    def _verify_profit_claim(self, claim, a, raw, *, manual=False):
        from .engine import ValidationError
        if claim['status']=='verified':return {'claim':copy.deepcopy(claim),'duplicate':True}
        if claim['status'] not in self.CLAIM_UNCERTAIN:raise ValidationError('尚无已送达的领取需要核验',409)
        if (a.get('phase')!='live' or a.get('phase_pending') or a.get('archived') or a.get('retired_failed')
            or a['name']!=claim['name'] or a.get('initial_balance')!=claim['target_balance']):
            raise ValidationError('领取核验账户或账号规模已变化',409)
        snap=self._claim_flat(a,raw)
        if snap['observed_at']<claim['submitted_at']:raise ValidationError('领取核验读数早于提交时间，请重新读取',409)
        expected=claim['balance_before']-claim['amount']
        if (not manual and abs(snap['balance']-expected)>.01) or abs(snap['balance']-claim['target_balance'])>.005:
            claim.update(status='balance_mismatch',observed_balance=snap['balance'],error='领取后余额未恢复账号初始规模，禁止新开仓')
            self._freeze(a['id'],claim['error']);self._save()
            raise ValidationError(claim['error'],409)
        claim.update(status='verified',verified_at=self.clock(),balance_after=snap['balance'],error='',credited_at=None)
        if manual:claim['manual_balance_reconciled']=True
        self._claim_audit('balance_verified',claim)
        a.update(balance=snap['balance'],equity=snap['equity'],unrealized_pnl=0,external_position=False,
                 realized_pnl=round(snap['balance']-a['initial_balance'],2),live_reentry_claim_id=claim['id'])
        if a.get('strategy_lock_source') in self.CLAIM_AUTO_LOCKS:
            a.update(strategy_locked=False,strategy_lock_reason=None,strategy_lock_source=None,strategy_locked_at=None)
        if (a.get('lock_reason') or '').startswith(('领取','利润领取')):
            a.update(status='active',lock_reason=None)
        old=self.data['bridge_sessions'].get(a['id'],{})
        self.data['bridge_sessions'][a['id']]={**old,'snapshot':snap,'last_seen':self.clock(),'ready':False}
        self._schedule_claim_reentry(claim)
        self._log('success',f"{a['name']} 利润领取后余额核验通过：{snap['balance']:g} USDC；到账状态仍以平台为准")
        self._save();return {'claim':copy.deepcopy(claim),'verified':True}

    def bridge_profit_claim(self, body):
        from .engine import ValidationError
        with self.lock:
            action=body.get('action')
            if action not in ('reserve','heartbeat','intent','verify','fail'):raise ValidationError('领取动作无效')
            claim,a=self._claim_authority(body,action)
            owner=self.data['execution_sessions'][a['session_id']]
            if action=='heartbeat':
                if claim['status'] not in ('preparing','submitted') or not 0<=self.clock()-claim.get('reserved_at',0)<=90:
                    raise ValidationError('领取流程授权窗口已结束，请返回交易页核对',409)
                if claim.get('mode_epoch')!=body['mode_epoch']:raise ValidationError('领取授权轮次已变化',409)
                owner.update(last_seen=self.clock(),tab_url=body['tab_url']);self._save()
                return {'mode':'execute','mode_epoch':owner['mode_epoch'],'claim_id':claim['id']}
            if action=='reserve':
                if claim['status']!='queued':return {'claim':copy.deepcopy(claim),'duplicate':True}
                self._assert_settled_browser_actions()
                snap=self._claim_flat(a,body.get('snapshot'))
                if snap['balance']-claim['target_balance']<=.005:raise ValidationError('Live 账号已无可领取净利润',409)
                claim.update(status='preparing',reserved_at=self.clock(),mode_epoch=body['mode_epoch'],balance_before=snap['balance'])
            elif action=='intent':
                if claim['status']!='preparing':return {'claim':copy.deepcopy(claim),'authorize_click':False,'duplicate':True}
                if claim.get('mode_epoch')!=body['mode_epoch'] or not 0<=self.clock()-claim['reserved_at']<=90:
                    raise ValidationError('领取准备已过期，禁止提交',409)
                self._assert_settled_browser_actions()
                q=body.get('preview') or {}
                flat=q.get('flat') or {}
                stamp=flat.get('observed_at')
                if (any(isinstance(flat.get(k),bool) or flat.get(k)!=0 for k in ('leverage','position_margin','order_margin'))
                    or isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp)
                    or not 0<=self.clock()-stamp<=5):
                    raise ValidationError('持仓页尚未明确无持仓、无挂单，禁止领取',409)
                def money(key):
                    v=q.get(key)
                    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:raise ValidationError('领取预览金额无效')
                    return Decimal(str(v))
                available=money('available');amount=money('amount');received=money('received')
                cfg=claim['policy'];mode=cfg['live_payout_mode']
                expected=available if mode=='all' else Decimal(str(cfg['live_payout_amount'])) if mode=='amount' else (available*Decimal(str(cfg['live_payout_percent']))/100).quantize(Decimal('.000001'),rounding=ROUND_DOWN)
                if (q.get('recipient')!='main' or cfg['live_payout_recipient']!='main' or amount!=expected or amount>available
                    or received>amount or abs(Decimal(str(claim['balance_before']))-available-Decimal(str(claim['target_balance'])))>Decimal('.005')):
                    raise ValidationError('主账户、可领取利润或申请金额与原策略不符，禁止提交',409)
                if self._position(a['id']) or self._pending(a['id']):raise ValidationError('账户出现持仓或未决订单，禁止领取',409)
                claim.update(status='submitted',submitted_at=self.clock(),amount=float(amount),available=float(available),received_preview=float(received))
                self._claim_audit('submit_intent',claim)
                self._save()
                return {'claim':copy.deepcopy(claim),'authorize_click':True}
            elif action=='verify':return self._verify_profit_claim(claim,a,body.get('snapshot'))
            else:
                if claim['status']!='verified':
                    claim.update(status='unknown' if claim['status'] in self.CLAIM_UNCERTAIN else 'queued',
                        error=('利润领取结果需核验：'+str(body.get('error','执行中断'))).replace(self.bridge_token,'[redacted]')[:300])
                    self._freeze(a['id'],claim['error'])
                    self._revoke_execution(owner,claim['error'])
                    self._claim_audit('interrupted',claim)
            self._save();return {'claim':copy.deepcopy(claim)}

    def reconcile_profit_claim(self, cid, body):
        from .engine import ValidationError
        with self.lock:
            if body.get('acknowledge') is not True:raise ValidationError('需要明确确认核对领取后的余额')
            claim=self.data['profit_claims'].get(cid)
            if not claim:raise ValidationError('领取记录不存在',404)
            a=self._account(claim['account_id']);session=self.data['bridge_sessions'].get(a['id'],{})
            if session.get('source')!='readonly-inspection' or self.clock()-session.get('last_seen',0)>5:
                raise ValidationError('请在只读模式切到该 Live 号，并保持 NQ 活动持仓面板可见',409)
            return self._verify_profit_claim(claim,a,session.get('snapshot'),manual=True)

    def cancel_profit_claim(self, cid, body):
        from .engine import ValidationError
        with self.lock:
            claim=self.data['profit_claims'].get(cid)
            if not claim:raise ValidationError('领取记录不存在',404)
            owner=self.data['execution_sessions'].get(claim['session_id'],{})
            if (body.get('acknowledge') is not True or claim['status'] not in ('queued','preparing')
                or self.data['strategy']['status'] in self.ACTIVE or owner.get('mode')=='execute'):
                raise ValidationError('仅可在策略暂停且插件只读时取消尚未提交的领取',409)
            claim.update(status='cancelled',cancelled_at=self.clock())
            self._claim_audit('cancelled',claim)
            a=self._account(claim['account_id'])
            if (a.get('lock_reason') or '').startswith('利润领取') and not self._pending(a['id']):
                a.update(status='active',lock_reason=None)
            self._save();return {'claim':copy.deepcopy(claim)}
