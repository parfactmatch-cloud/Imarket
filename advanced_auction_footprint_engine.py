"""
advanced_auction_footprint_engine.py
Implements Garvit Garg's Auction Market & Footprint Mechanics:
1. Price-Level Bid/Ask Grid (Footprint)
2. Diagonal Imbalance (3:1 threshold)
3. Stacked Imbalances (>= 3 consecutive levels)
4. Absorption Detection ("Effort without Reward")
5. Structural Imperfections (Poor High/Low & Single Prints)
6. Single Time-Framing Guardrail
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class PriceLevelVolume:
    bid_vol: int = 0
    ask_vol: int = 0

    @property
    def total_vol(self) -> int:
        return self.bid_vol + self.ask_vol

    @property
    def delta(self) -> int:
        return self.ask_vol - self.bid_vol


@dataclass
class FootprintBar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    grid: Dict[float, PriceLevelVolume] = field(default_factory=dict)
    bar_delta: int = 0
    cumulative_delta: int = 0
    poc_price: float = 0.0
    stacked_buy_imbalances: List[float] = field(default_factory=list)
    stacked_sell_imbalances: List[float] = field(default_factory=list)
    is_buying_absorption: bool = False
    is_selling_absorption: bool = False
    is_poor_high: bool = False
    is_poor_low: bool = False


class AdvancedAuctionFootprintEngine:
    def __init__(self, tick_size: float = 0.05, imbalance_ratio: float = 3.0, stacked_count: int = 3):
        self.tick_size = tick_size
        self.imbalance_ratio = imbalance_ratio
        self.stacked_count = stacked_count

        self.current_grid: Dict[float, PriceLevelVolume] = {}
        self.current_ticks: List[Tuple[float, int, str]] = []
        self.running_cvd: int = 0
        self.historical_bars: List[FootprintBar] = []

    def round_tick(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 2)

    def process_tick(self, ltp: float, qty: int, best_bid: float, best_ask: float):
        price = self.round_tick(ltp)
        if price not in self.current_grid:
            self.current_grid[price] = PriceLevelVolume()

        if ltp >= best_ask:
            self.current_grid[price].ask_vol += qty
            self.running_cvd += qty
            self.current_ticks.append((price, qty, "BUY"))
        elif ltp <= best_bid:
            self.current_grid[price].bid_vol += qty
            self.running_cvd -= qty
            self.current_ticks.append((price, qty, "SELL"))
        else:
            if self.current_ticks and ltp >= self.current_ticks[-1][0]:
                self.current_grid[price].ask_vol += qty
                self.running_cvd += qty
                self.current_ticks.append((price, qty, "BUY"))
            else:
                self.current_grid[price].bid_vol += qty
                self.running_cvd -= qty
                self.current_ticks.append((price, qty, "SELL"))

    def evaluate_bar(self, timestamp: str, open_p: float, high_p: float, low_p: float, close_p: float, volume: int) -> FootprintBar:
        sorted_prices = sorted(self.current_grid.keys())
        if not sorted_prices:
            return FootprintBar(timestamp, open_p, high_p, low_p, close_p, volume)

        total_ask = sum(level.ask_vol for level in self.current_grid.values())
        total_bid = sum(level.bid_vol for level in self.current_grid.values())
        bar_delta = total_ask - total_bid
        poc_price = max(self.current_grid.keys(), key=lambda p: self.current_grid[p].total_vol)

        buy_imbalance_levels = []
        sell_imbalance_levels = []

        for p in sorted_prices:
            lower_p = self.round_tick(p - self.tick_size)
            if lower_p in self.current_grid:
                lower_bid = self.current_grid[lower_p].bid_vol
                curr_ask = self.current_grid[p].ask_vol
                if (lower_bid == 0 and curr_ask >= 10) or (lower_bid > 0 and (curr_ask / lower_bid) >= self.imbalance_ratio):
                    buy_imbalance_levels.append(p)

            higher_p = self.round_tick(p + self.tick_size)
            if higher_p in self.current_grid:
                higher_ask = self.current_grid[higher_p].ask_vol
                curr_bid = self.current_grid[p].bid_vol
                if (higher_ask == 0 and curr_bid >= 10) or (higher_ask > 0 and (curr_bid / higher_ask) >= self.imbalance_ratio):
                    sell_imbalance_levels.append(p)

        stacked_buy = self._find_consecutive_levels(buy_imbalance_levels)
        stacked_sell = self._find_consecutive_levels(sell_imbalance_levels)

        bar_range = max(high_p - low_p, self.tick_size)
        is_buying_absorption = (bar_delta > 0 and (high_p - close_p) >= (0.6 * bar_range))
        is_selling_absorption = (bar_delta < 0 and (close_p - low_p) >= (0.6 * bar_range))

        top_price = sorted_prices[-1]
        bottom_price = sorted_prices[0]
        is_poor_high = (self.current_grid[top_price].bid_vol > 0 and self.current_grid[top_price].ask_vol > 0)
        is_poor_low = (self.current_grid[bottom_price].bid_vol > 0 and self.current_grid[bottom_price].ask_vol > 0)

        fp_bar = FootprintBar(
            timestamp=timestamp,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=volume,
            grid=dict(self.current_grid),
            bar_delta=bar_delta,
            cumulative_delta=self.running_cvd,
            poc_price=poc_price,
            stacked_buy_imbalances=stacked_buy,
            stacked_sell_imbalances=stacked_sell,
            is_buying_absorption=is_buying_absorption,
            is_selling_absorption=is_selling_absorption,
            is_poor_high=is_poor_high,
            is_poor_low=is_poor_low
        )

        self.historical_bars.append(fp_bar)
        self.current_grid.clear()
        self.current_ticks.clear()
        return fp_bar

    def _find_consecutive_levels(self, levels: List[float]) -> List[float]:
        if len(levels) < self.stacked_count:
            return []
        levels = sorted(levels)
        consecutive = []
        temp = [levels[0]]

        for i in range(1, len(levels)):
            if abs(levels[i] - levels[i - 1] - self.tick_size) < 1e-4:
                temp.append(levels[i])
            else:
                if len(temp) >= self.stacked_count:
                    consecutive.extend(temp)
                temp = [levels[i]]

        if len(temp) >= self.stacked_count:
            consecutive.extend(temp)
        return consecutive

    def is_single_time_framing(self, bars_count: int = 3, direction: str = "UP") -> bool:
        if len(self.historical_bars) < bars_count:
            return False

        recent = self.historical_bars[-bars_count:]
        if direction == "UP":
            return all(recent[i].low > recent[i - 1].low for i in range(1, len(recent)))
        elif direction == "DOWN":
            return all(recent[i].high < recent[i - 1].high for i in range(1, len(recent)))
        return False
