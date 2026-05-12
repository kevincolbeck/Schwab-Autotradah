"""Matplotlib chart widgets embedded in PySide6 for backtest results."""

from typing import List

import matplotlib
matplotlib.use("QtAgg")

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates

from backtest.portfolio import EquityPoint

# Dark theme colors matching gui/styles.py
BG_COLOR = "#1a1a2e"
AXIS_COLOR = "#3a3a5c"
TEXT_COLOR = "#e0e0e0"
GRID_COLOR = "#2a2a4e"
GREEN = "#00ff88"
RED = "#ff4444"
BLUE = "#4488ff"
YELLOW = "#ffaa00"
PURPLE = "#8888cc"


class EquityCurveChart(FigureCanvas):
    """Equity curve with drawdown overlay and regime shading."""

    def __init__(self, parent=None, width=10, height=5, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi, facecolor=BG_COLOR)
        super().__init__(self.fig)
        self.setParent(parent)

    def plot(self, equity_curve: List[EquityPoint], starting_capital: float):
        self.fig.clear()

        ax1 = self.fig.add_subplot(211)
        ax2 = self.fig.add_subplot(212, sharex=ax1)

        dates = [ep.date for ep in equity_curve]
        equities = [ep.equity for ep in equity_curve]
        drawdowns = [-ep.drawdown_pct for ep in equity_curve]
        regimes = [ep.regime for ep in equity_curve]

        # Equity line
        ax1.plot(dates, equities, color=GREEN, linewidth=1.0, label="Equity")
        ax1.axhline(y=starting_capital, color=PURPLE, linestyle="--", linewidth=0.5, alpha=0.5)

        # Regime shading
        self._shade_regimes(ax1, dates, regimes, min(equities) * 0.95, max(equities) * 1.05)

        # Drawdown fill
        ax2.fill_between(dates, drawdowns, 0, color=RED, alpha=0.3)
        ax2.plot(dates, drawdowns, color=RED, linewidth=0.8)

        for ax in [ax1, ax2]:
            ax.set_facecolor(BG_COLOR)
            ax.tick_params(colors=TEXT_COLOR, labelsize=8)
            ax.spines["bottom"].set_color(AXIS_COLOR)
            ax.spines["left"].set_color(AXIS_COLOR)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.grid(True, color=GRID_COLOR, linewidth=0.3)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            ax.xaxis.set_major_locator(mdates.YearLocator())

        ax1.set_ylabel("Equity ($)", color=TEXT_COLOR, fontsize=9)
        ax1.legend(loc="upper left", fontsize=8, facecolor=BG_COLOR, edgecolor=AXIS_COLOR, labelcolor=TEXT_COLOR)
        ax2.set_ylabel("Drawdown (%)", color=TEXT_COLOR, fontsize=9)
        ax2.set_xlabel("Date", color=TEXT_COLOR, fontsize=9)

        self.fig.tight_layout()
        self.draw()

    def _shade_regimes(self, ax, dates, regimes, ymin, ymax):
        regime_colors = {
            "TREND": (*_hex_to_rgb(GREEN), 0.05),
            "CHOP": (*_hex_to_rgb(YELLOW), 0.05),
            "RISK_OFF": (*_hex_to_rgb(RED), 0.08),
        }
        i = 0
        while i < len(dates):
            regime = regimes[i]
            j = i
            while j < len(dates) and regimes[j] == regime:
                j += 1
            color = regime_colors.get(regime, (1, 1, 1, 0.02))
            ax.axvspan(dates[i], dates[min(j - 1, len(dates) - 1)], color=color)
            i = j


class TradeDistributionChart(FigureCanvas):
    """Bar chart of R-multiple per trade."""

    def __init__(self, parent=None, width=10, height=4, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi, facecolor=BG_COLOR)
        super().__init__(self.fig)
        self.setParent(parent)

    def plot(self, trades: list):
        self.fig.clear()
        ax = self.fig.add_subplot(111)

        r_values = [t["pnl_r"] for t in trades]
        colors = [GREEN if r > 0 else RED for r in r_values]

        ax.bar(range(len(r_values)), r_values, color=colors, width=0.8)
        ax.axhline(y=0, color=PURPLE, linewidth=0.5)

        ax.set_facecolor(BG_COLOR)
        ax.tick_params(colors=TEXT_COLOR, labelsize=8)
        ax.spines["bottom"].set_color(AXIS_COLOR)
        ax.spines["left"].set_color(AXIS_COLOR)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(True, axis="y", color=GRID_COLOR, linewidth=0.3)
        ax.set_ylabel("R-Multiple", color=TEXT_COLOR, fontsize=9)
        ax.set_xlabel("Trade #", color=TEXT_COLOR, fontsize=9)

        self.fig.tight_layout()
        self.draw()


class RHistogramChart(FigureCanvas):
    """Histogram of R-multiple distribution."""

    def __init__(self, parent=None, width=10, height=4, dpi=100):
        self.fig = Figure(figsize=(width, height), dpi=dpi, facecolor=BG_COLOR)
        super().__init__(self.fig)
        self.setParent(parent)

    def plot(self, trades: list):
        self.fig.clear()
        ax = self.fig.add_subplot(111)

        r_values = [t["pnl_r"] for t in trades]
        if not r_values:
            return

        ax.hist(r_values, bins=30, color=BLUE, alpha=0.7, edgecolor=AXIS_COLOR)
        ax.axvline(x=0, color=PURPLE, linewidth=1, linestyle="--")

        mean_r = sum(r_values) / len(r_values)
        ax.axvline(x=mean_r, color=YELLOW, linewidth=1, linestyle="-", label=f"Mean: {mean_r:.2f}R")

        ax.set_facecolor(BG_COLOR)
        ax.tick_params(colors=TEXT_COLOR, labelsize=8)
        ax.spines["bottom"].set_color(AXIS_COLOR)
        ax.spines["left"].set_color(AXIS_COLOR)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(True, axis="y", color=GRID_COLOR, linewidth=0.3)
        ax.set_ylabel("Count", color=TEXT_COLOR, fontsize=9)
        ax.set_xlabel("R-Multiple", color=TEXT_COLOR, fontsize=9)
        ax.legend(fontsize=8, facecolor=BG_COLOR, edgecolor=AXIS_COLOR, labelcolor=TEXT_COLOR)

        self.fig.tight_layout()
        self.draw()


def _hex_to_rgb(hex_color: str):
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
