"""
mtf_profile_context_builder.py
Constructs Multi-Timeframe (MTF) Context according to Garvit Garg's rules:
- Weekly Context: Monday/Tuesday uses Prior Week VAH/VAL; Wed/Thu/Fri uses Current Week Developing.
- Monthly Context: First week uses Prior Month VAH/VAL/POC.
- Context Question Framework:
  1. Where is price coming from?
  2. Where is price right now (Inside Value vs Outside Balance)?
  3. Where is price trying to go?
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class ProfileLevels:
    vah: float
    val: float
    poc: float
    single_prints: List[float]


class MTFProfileContextBuilder:
    def __init__(
        self,
        prev_month: ProfileLevels,
        prev_week: ProfileLevels,
        current_week: ProfileLevels,
        daily_profile: ProfileLevels,
        current_month: Optional[ProfileLevels] = None,
    ):
        self.prev_month = prev_month
        # current_month defaults to prev_month for callers that only have
        # one month of levels; when supplied, the day-of-month rule below
        # actually selects between the two (the old code returned
        # prev_month from both branches).
        self.current_month = current_month if current_month is not None else prev_month
        self.prev_week = prev_week
        self.current_week = current_week
        self.daily_profile = daily_profile

    def get_active_location_zones(self, current_dt: Optional[datetime] = None) -> Dict[str, any]:
        """
        Applies Day-of-Week Calendar Rule to select effective profile references.
        """
        dt = current_dt or datetime.now()
        weekday = dt.weekday()  # Monday = 0, Tuesday = 1, Wednesday = 2 ...

        if weekday in (0, 1):
            active_weekly = self.prev_week
            weekly_label = "PRIOR_WEEK"
        else:
            active_weekly = self.current_week
            weekly_label = "CURRENT_WEEK_DEVELOPING"

        use_prior_month = dt.day <= 7
        # Fixed: the old code returned self.prev_month from BOTH branches,
        # so the current month was never used.
        active_monthly = self.prev_month if use_prior_month else self.current_month
        monthly_label = "PRIOR_MONTH" if use_prior_month else "CURRENT_MONTH"

        return {
            "weekly_label": weekly_label,
            "weekly_levels": active_weekly,
            "monthly_label": monthly_label,
            "monthly_levels": active_monthly,
            "daily_levels": self.daily_profile
        }

    def evaluate_auction_context(self, current_price: float, current_dt: Optional[datetime] = None) -> Dict[str, any]:
        """
        Answers the 3 Evergreen Auction Questions and applies the POC Chop Filter.
        """
        zones = self.get_active_location_zones(current_dt)
        w_levels: ProfileLevels = zones["weekly_levels"]

        inside_value = w_levels.val <= current_price <= w_levels.vah
        is_above_value = current_price > w_levels.vah
        is_below_value = current_price < w_levels.val

        # Chop Filter: Avoid initiating breakout trades right at the POC (within 0.1%)
        distance_to_poc = abs(current_price - w_levels.poc)
        is_chop_risk = distance_to_poc < (0.001 * current_price)

        if is_above_value:
            bias = "IMBALANCE_BULLISH"
            destination = "NEW_VALUE_DISCOVERY_HIGH"
        elif is_below_value:
            bias = "IMBALANCE_BEARISH"
            destination = "NEW_VALUE_DISCOVERY_LOW"
        else:
            bias = "BALANCED_ROTATIONAL"
            destination = w_levels.poc

        return {
            "bias": bias,
            "inside_value": inside_value,
            "target_destination": destination,
            "is_chop_risk": is_chop_risk,
            "active_reference": zones["weekly_label"],
            "vah": w_levels.vah,
            "val": w_levels.val,
            "poc": w_levels.poc
        }
