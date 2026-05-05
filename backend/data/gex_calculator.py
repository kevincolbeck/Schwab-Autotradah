"""
GEX (Gamma Exposure) computation from Schwab options chain.

Convention (standard dealer model):
  - Dealers assumed short calls, long puts
  - Call GEX per strike = +gamma × OI × 100 × spot   (dealer long delta when price rises → they sell)
  - Put GEX per strike  = -gamma × OI × 100 × spot   (dealer short delta when price falls → they buy)
  - Net GEX = sum of all strikes

Positive net GEX → dealers absorb moves (mean-reverting environment)
Negative net GEX → dealers amplify moves (trending/volatile environment)

Key levels:
  call_wall   = strike with highest call GEX → resistance
  put_wall    = strike with highest put GEX magnitude → support
  gamma_flip  = strike nearest to where cumulative GEX changes sign
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
import json

from backend.data.schwab_rest import get_options_chain

logger = logging.getLogger(__name__)


@dataclass
class GexLevels:
    ticker: str
    spot_price: float
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    gamma_flip: Optional[float] = None
    net_gex: float = 0.0
    net_gex_regime: str = "POSITIVE"   # POSITIVE or NEGATIVE
    per_strike: dict = field(default_factory=dict)   # strike → net GEX

    def proximity_to_nearest_wall(self) -> tuple[float, str]:
        """Returns (pct_distance, wall_side) for the nearest key level."""
        candidates = []
        if self.call_wall:
            candidates.append((abs(self.spot_price - self.call_wall) / self.spot_price, "CALL"))
        if self.put_wall:
            candidates.append((abs(self.spot_price - self.put_wall) / self.spot_price, "PUT"))
        if self.gamma_flip:
            candidates.append((abs(self.spot_price - self.gamma_flip) / self.spot_price, "FLIP"))
        if not candidates:
            return (1.0, "NONE")
        return min(candidates, key=lambda x: x[0])

    def approaching_wall(self, threshold_pct: float = 0.003) -> tuple[bool, str]:
        """Returns (True, wall_side) if within threshold of any key level."""
        pct, side = self.proximity_to_nearest_wall()
        return (pct <= threshold_pct, side)

    def to_json(self) -> str:
        return json.dumps({
            "call_wall": self.call_wall,
            "put_wall": self.put_wall,
            "gamma_flip": self.gamma_flip,
            "net_gex": self.net_gex,
            "per_strike": {str(k): v for k, v in list(self.per_strike.items())[:50]},
        })


def _parse_chain(chain_data: dict, spot_price: float) -> dict:
    """Extract (strike → {call_gex, put_gex}) from raw Schwab chain response."""
    result = {}

    for date_key, strikes in chain_data.get("callExpDateMap", {}).items():
        for strike_str, contracts in strikes.items():
            strike = float(strike_str.split(":")[0])
            for contract in contracts:
                gamma = contract.get("gamma", 0) or 0
                oi    = contract.get("openInterest", 0) or 0
                gex   = gamma * oi * 100 * spot_price
                if strike not in result:
                    result[strike] = {"call_gex": 0.0, "put_gex": 0.0}
                result[strike]["call_gex"] += gex

    for date_key, strikes in chain_data.get("putExpDateMap", {}).items():
        for strike_str, contracts in strikes.items():
            strike = float(strike_str.split(":")[0])
            for contract in contracts:
                gamma = contract.get("gamma", 0) or 0
                oi    = contract.get("openInterest", 0) or 0
                gex   = -gamma * oi * 100 * spot_price
                if strike not in result:
                    result[strike] = {"call_gex": 0.0, "put_gex": 0.0}
                result[strike]["put_gex"] += gex

    return result


def _find_gamma_flip(per_strike: dict, spot_price: float) -> Optional[float]:
    """
    Find the strike nearest to where cumulative GEX (scanning from ATM outward)
    changes sign. This is the gamma flip level.
    """
    strikes = sorted(per_strike.keys())
    net_by_strike = {
        s: per_strike[s]["call_gex"] + per_strike[s]["put_gex"]
        for s in strikes
    }
    cumulative = 0.0
    prev_cumulative = 0.0
    prev_strike = None

    for strike in sorted(strikes, key=lambda s: abs(s - spot_price)):
        prev_cumulative = cumulative
        cumulative += net_by_strike[strike]
        if prev_strike is not None and prev_cumulative * cumulative < 0:
            # Sign changed between prev_strike and strike
            return (prev_strike + strike) / 2
        prev_strike = strike

    return None


async def compute_gex(ticker: str) -> Optional[GexLevels]:
    """Fetch options chain and compute GEX levels. Returns None on error."""
    try:
        chain = await get_options_chain(ticker, contract_type="ALL", strike_count=60)
        underlying = chain.get("underlyingPrice", 0)
        if not underlying:
            logger.warning(f"GEX: no underlying price for {ticker}")
            return None

        spot_price = float(underlying)
        per_strike = _parse_chain(chain, spot_price)

        if not per_strike:
            logger.warning(f"GEX: empty chain for {ticker}")
            return None

        net_by_strike = {
            s: per_strike[s]["call_gex"] + per_strike[s]["put_gex"]
            for s in per_strike
        }
        net_gex = sum(net_by_strike.values())

        # Call wall = highest call GEX above spot
        call_strikes = {s: per_strike[s]["call_gex"] for s in per_strike if s > spot_price}
        call_wall = max(call_strikes, key=call_strikes.get) if call_strikes else None

        # Put wall = largest put GEX magnitude below spot
        put_strikes = {s: abs(per_strike[s]["put_gex"]) for s in per_strike if s < spot_price}
        put_wall = max(put_strikes, key=put_strikes.get) if put_strikes else None

        gamma_flip = _find_gamma_flip(per_strike, spot_price)

        levels = GexLevels(
            ticker=ticker,
            spot_price=spot_price,
            call_wall=call_wall,
            put_wall=put_wall,
            gamma_flip=gamma_flip,
            net_gex=net_gex,
            net_gex_regime="POSITIVE" if net_gex >= 0 else "NEGATIVE",
            per_strike=net_by_strike,
        )
        flip_str = f"{gamma_flip:.2f}" if gamma_flip is not None else "None"
        logger.debug(f"GEX {ticker}: call_wall={call_wall}, put_wall={put_wall}, "
                     f"flip={flip_str}, net={net_gex:.0f}")
        return levels

    except Exception as e:
        logger.error(f"GEX compute failed for {ticker}: {e}")
        return None
