"""Native NQ stop risk uses canonical position quantity, without a futures multiplier."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math


def stop_plan(position, target):
    if target is None:
        return {'status': 'omitted' if position.get('native_sl_absent') is True else 'mismatch',
                'target_risk': None, 'actual_risk': None, 'target_price': None}
    entry, quantity = position.get('entry_price'), position.get('quantity')
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
               for v in (entry, quantity, target)) or position.get('side') not in ('buy', 'sell'):
        raise ValueError('止损校验缺少真实成交均价、数量或方向')
    sign = 1 if position['side'] == 'sell' else -1
    # The public position table and editor show NQ prices to cents. If the
    # requested risk cannot be represented, round toward entry, never away.
    raw = Decimal(str(entry)) + sign * Decimal(str(target)) / Decimal(str(quantity))
    price = float(raw.quantize(Decimal('.01'), rounding=ROUND_FLOOR if sign == 1 else ROUND_CEILING))
    if price <= 0 or sign * (price - entry) <= 0:
        raise ValueError('止损金额小于当前数量可表示的最小价格间距')
    stop = position.get('native_sl_price')
    actual = sign * (stop - entry) * quantity if isinstance(stop, (int, float)) and math.isfinite(stop) and stop > 0 else None
    matched = actual is not None and actual > 0 and abs(stop - price) < 0.000001
    return {'status': 'verified' if matched else 'mismatch', 'target_risk': target,
            'actual_risk': round(actual, 8) if actual is not None else None, 'target_price': price}
