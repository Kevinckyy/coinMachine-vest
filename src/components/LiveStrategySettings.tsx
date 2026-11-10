export type LiveStrategyConfig = {
  live_default_locked: boolean;
  live_side: 'random' | 'buy' | 'sell';
  live_size_percent: number;
  live_take_profit: number | null;
  live_stop_loss: number | null;
  live_skip_stop_loss_on_low_drawdown: boolean;
  live_drawdown_stop_loss_threshold: number;
  live_payout_recipient: 'main';
  live_payout_mode: 'all' | 'amount' | 'percent';
  live_payout_amount: number;
  live_payout_percent: number;
};

export const defaultLiveStrategyConfig: LiveStrategyConfig = {
  live_default_locked: true, live_side: 'random', live_size_percent: 25,
  live_take_profit: null, live_stop_loss: 130,
  live_skip_stop_loss_on_low_drawdown: false, live_drawdown_stop_loss_threshold: 200,
  live_payout_recipient: 'main', live_payout_mode: 'all', live_payout_amount: 1, live_payout_percent: 100,
};

export default function LiveStrategySettings({value, onChange}: {
  value: LiveStrategyConfig; onChange: (patch: Partial<LiveStrategyConfig>) => void;
}) {
  return <section className="strategy-live-settings" aria-label="Live 自动策略参数">
    <div className="form-section-label"><span>04</span>Live 账户</div>
    <label className="live-default-lock-toggle"><input type="checkbox" aria-label="Live 号默认锁定"
      checked={value.live_default_locked} onChange={e => onChange({live_default_locked:e.target.checked})}/>
      <span>Live 号默认锁定</span></label>
    <p className="form-footnote">开启后，新识别及转入 Live 的账号自动锁定，可在账户池逐个解锁。手动解锁会保留；关闭开关仅解除默认锁定，不解除其他锁定。</p>
    <label className="field"><span className="field-label">Live 下单方向</span>
      <select aria-label="Live 下单方向" value={value.live_side} onChange={e => onChange({live_side:e.target.value as LiveStrategyConfig['live_side']})}>
        <option value="random">随机做多 / 做空</option><option value="buy">只做多</option><option value="sell">只做空</option>
      </select><small>与考核号同批时，遵守同品种统一方向规则。</small></label>
    <div className="field"><span className="field-label" id="live-size-label">Live 仓位大小</span>
      <div className="position-size-options" role="group" aria-labelledby="live-size-label">{[25,50,75,100].map(percent =>
        <button key={percent} type="button" aria-label={`Live 仓位大小 ${percent}%`} aria-pressed={value.live_size_percent===percent}
          className={value.live_size_percent===percent?'selected':''} onClick={() => onChange({live_size_percent:percent})}>{percent}%</button>)}</div></div>
    <div className="two-columns">
      <label className="field"><span className="field-label">Live 止盈金额 · USDC</span>
        <input aria-label="Live 止盈金额" type="number" min="0.01" step="any" placeholder="不设置" value={value.live_take_profit??''}
          onChange={e => onChange({live_take_profit:e.target.value===''?null:Number(e.target.value)})}/></label>
      <label className="field"><span className="field-label">Live 止损金额 · USDC</span>
        <input aria-label="Live 止损金额" type="number" min="0.01" step="any" placeholder="不设置" value={value.live_stop_loss??''}
          onChange={e => onChange({live_stop_loss:e.target.value===''?null:Number(e.target.value)})}/></label>
    </div>
    <div className="strategy-stop-rule">
      <label className="strategy-live-stop-toggle"><input type="checkbox" aria-label="Live 剩余最大回撤不足时不设置止损"
        checked={value.live_skip_stop_loss_on_low_drawdown} onChange={e => onChange({live_skip_stop_loss_on_low_drawdown:e.target.checked})}/>
        <span>Live 剩余最大回撤小于阈值时不设置止损</span></label>
      <label className="field"><span className="field-label">Live 剩余最大回撤阈值 · USDC</span>
        <input aria-label="Live 剩余最大回撤阈值" type="number" min="0.01" step="any" required
          disabled={!value.live_skip_stop_loss_on_low_drawdown} value={value.live_drawdown_stop_loss_threshold}
          onChange={e => onChange({live_drawdown_stop_loss_threshold:Number(e.target.value)})}/></label>
    </div>
    <p className="form-footnote">Live 参数独立于考核号，单号手动解锁后仍需选入策略。止盈或止损留空表示不设置；已有持仓沿用开仓时的参数。</p>
    <div className="strategy-stop-rule live-payout-settings" aria-label="Live 利润领取参数">
      <h3>利润领取</h3>
      <label className="field"><span className="field-label">收款人</span>
        <input aria-label="Live 利润收款人" value="主账户" readOnly/></label>
      <label className="field"><span className="field-label">领取方式</span>
        <select aria-label="Live 利润领取方式" value={value.live_payout_mode}
          onChange={e=>onChange({live_payout_mode:e.target.value as LiveStrategyConfig['live_payout_mode']})}>
          <option value="all">全部可领取利润</option><option value="amount">固定金额</option><option value="percent">按比例</option>
        </select></label>
      {value.live_payout_mode==='amount'&&<label className="field"><span className="field-label">申请金额 · USDC</span>
        <input aria-label="Live 利润领取金额" type="number" min="0.000001" step="0.000001" required value={value.live_payout_amount}
          onChange={e=>onChange({live_payout_amount:Number(e.target.value)})}/></label>}
      {value.live_payout_mode==='percent'&&<label className="field"><span className="field-label">领取比例 · %</span>
        <input aria-label="Live 利润领取比例" type="number" min="0.000001" max="100" step="any" required value={value.live_payout_percent}
          onChange={e=>onChange({live_payout_percent:Number(e.target.value)})}/></label>}
      <p className="form-footnote">默认领取全部可领取利润到主账户。金额和比例均按分润前的可领取利润计算，实际到账以 Vest 弹窗预览为准。</p>
      <p className="form-footnote">Live 策略每单结束并确认空仓后，有净利润则按此参数领取到主账户。返回交易页核验余额恢复账号初始规模后，同一 Live 号当天继续下一单；手动锁定和风控限制保留。</p>
      <p className="form-footnote">部分领取后余额未恢复初始规模时暂停新开仓；提交结果不明时先核对，不重复领取。修改参数只影响下次启动的策略。</p>
    </div>
  </section>;
}
