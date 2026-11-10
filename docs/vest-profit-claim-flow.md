# Vest Live 利润领取流程（提交前取证）

已通过登录后的公开网页 DOM 和原生界面检查。原始账户截图、金额和组件快照保存在本机 `runtime/page-evidence/20261110-account41-payout/`，不提交到 Git。没有调用 Vest 私有接口，没有提交领取申请。

## 实测步骤与组件

| 步骤 | 定位与校验 | 已确认行为 |
| --- | --- | --- |
| 核对来源账户 | `[data-testid="account-selector-trigger"]` | 唯一匹配目标账号名称，且明确显示“实盘 / Live Funded Account”；不要仅按账户编号推断阶段。 |
| 顶部“持仓” | `a[href="/portfolio"]`，可见文字“持仓” | 打开 Portfolio。与下方持仓表的同名 Tab 区分。 |
| 打开领取窗口 | `button[data-testid="withdraw-open"]` | `type="button"`，本次检查点击逻辑仅将 `isWithdrawModalOpen` 设为 true；不发送领取。 |
| 限定窗口 | `[role="dialog"][data-slot="dialog-content"]`，标题 `[data-slot="dialog-title"]` 精确为“领取利润” | 只接受一个可见窗口；Radix 的运行时 `id` 不作为固定选择器。 |
| 收款人 | 窗口中包含“收款人”的区域；其 `input[data-slot="input"]`、同区“PA / 主账户”标记 | 初始收款输入值为空，旁边显示主账户标记。没有观察到收款人下拉菜单；不要编造下拉选项、填入外部地址或把空值直接当成未知错误。未来实现必须同时核验主账户标记、输入框及来源账户身份。 |
| 申请金额 | 窗口内 `input[name="amount"][data-slot="form-control"]` | 实际是 text 输入框，可填写 USDC 小数，平台本次使用 6 位精度。`id` 与 `aria-describedby` 动态生成。 |
| 可领取利润 | `[data-testid="claim-profit-available"]` | 以此实时金额计算全部或百分比，不能用净值、余额或未实现盈亏替代。 |
| 比例输入 | `input[data-testid="claim-profit-amount-slider-percentage"]`，`aria-label="金额滑块百分比"` | 输入 100 会联动申请全部可领取利润；输入 50 会联动一半。实际 input type 为 text。 |
| 比例滑块 | `[data-testid="claim-profit-amount-slider"] [role="slider"]` | 可用比例输入替代坐标拖动；填写后核对联动金额和比例。 |
| 到账预览 | 同一窗口的“您将收到”标签及关联金额 | 本次界面按申请金额的 80% 展示，例如申请 10 USDC 时预览到账 8 USDC；将来仍以实际预览为准，不能硬编码分润比例。 |
| 最终提交边界 | `button[data-testid="claim-profit-submit"]` | `type="submit"`；初始金额空时 disabled，填写有效金额后 enabled。本次从未点击，也没有在表单内按 Enter。 |
| 取消并退出 | `[data-slot="dialog-close"][type="button"]`，可访问名称 `Close` | 只关闭预览窗口，保留原账户。 |

## 策略参数与口径

- `live_payout_recipient = "main"`：固定为主账户，后端拒绝其它收款人。
- `live_payout_mode = "all" | "amount" | "percent"`：默认 `all`。
- `live_payout_amount`：固定申请金额，正数、最多 6 位小数。为分润前金额，不是目标到账额。
- `live_payout_percent`：`0 < n <= 100`，按实时可领取利润计算，为分润前比例。

参数与原有 Live 参数一同持久保存；运行策略记录参数快照，修改表单不改写当前轮次。当前仅提供配置保存，没有创建利润领取任务或资金提交执行器。前端明确说明“不会自动提交利润领取申请”。

## 后续自动化边界

准备预览和最终提交应分开。预览前后都要核验来源账户名称、Live 阶段、主账户收款状态、可领取金额和到账预览；禁止使用另一个账户或旧页面的金额。金额超过可领取值、余额变化、未知弹窗、收款状态不明时停止，不自动改收款人或截断申请金额。

自动填金额使用输入事件触发联动后再读取实际值，不能仅修改 DOM 属性；不要通过表单内 Enter 提交。若未来允许真实提交，应持久记录账户、金额和领取状态，按已送达操作核对；响应丢失不能重新点击或推断失败。

本次未真实触发提交后的成功、排队、失败或待到账窗口，因此尚无这些组件的实测选择器，也没有验证到账时间。它们需要后续单独取证后才能实现自动核对。
