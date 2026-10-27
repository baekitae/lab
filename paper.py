"""Deterministic counterfactual fills, shared by AI outcome evaluation."""
from __future__ import annotations
import math

INITIAL_CASH = 10000
SLIPPAGE = .001
FEE = 1.0


def simulate(rows, proposed_qty):
    if not rows or type(proposed_qty) is not int or proposed_qty < 0:
        raise ValueError('후속 가격과 음수가 아닌 정수 수량이 필요합니다.')
    entry = rows[0]['open'] * (1 + SLIPPAGE)
    exit_price = rows[-1]['close'] * (1 - SLIPPAGE)
    filled = proposed_qty if proposed_qty > 0 and proposed_qty * entry + FEE <= INITIAL_CASH else 0
    cash = INITIAL_CASH - filled * entry - (FEE if filled else 0)
    pnl = filled * (exit_price - entry) - (2 * FEE if filled else 0)
    equity = [INITIAL_CASH] + [cash + filled * row['close'] for row in rows]
    equity[-1] = INITIAL_CASH + pnl
    peak, mdd = INITIAL_CASH, 0
    for value in equity:
        peak = max(peak, value)
        mdd = max(mdd, (peak - value) / peak)
    return {'proposed_qty': proposed_qty, 'filled_qty': filled,
            'fill_status': 'filled' if filled else 'cash' if proposed_qty == 0 else 'rejected_insufficient_cash',
            'entry_price': entry if filled else None, 'exit_price': exit_price if filled else None,
            'fees': 2 * FEE if filled else 0, 'net_pnl': pnl, 'net_return': pnl / INITIAL_CASH,
            'end_equity': INITIAL_CASH + pnl, 'close_equity_mdd': mdd, 'daily_close_equity': equity}


def buy_hold(rows):
    # Same capital, entry/exit timing, fees and slippage as the team experiment.
    quantity = max(0, math.floor((INITIAL_CASH - FEE) / (rows[0]['open'] * (1 + SLIPPAGE))))
    return simulate(rows, quantity)
