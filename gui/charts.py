"""Lightweight chart widgets using QPainter for the trading dashboard."""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QPainter, QPen, QColor, QBrush, QFont, QLinearGradient


class MiniLineChart(QWidget):
    """Simple line chart for equity curves, cumulative PnL, etc."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = []       # list of float values
        self._labels = []     # list of x-axis labels (optional)
        self._title = ""
        self._line_color = QColor("#4488ff")
        self._fill = True
        self._zero_line = True
        self._show_dots = False
        self.setMinimumHeight(120)

    def set_data(self, values: list, labels: list = None, title: str = "",
                 color: str = "#4488ff", fill: bool = True, zero_line: bool = True):
        self._data = values
        self._labels = labels or []
        self._title = title
        self._line_color = QColor(color)
        self._fill = fill
        self._zero_line = zero_line
        self.update()

    def paintEvent(self, event):
        if not self._data or len(self._data) < 2:
            painter = QPainter(self)
            painter.setPen(QColor("#555"))
            painter.drawText(self.rect(), Qt.AlignCenter, self._title or "No data")
            painter.end()
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()

        margin_l, margin_r, margin_t, margin_b = 50, 10, 24, 20
        chart_w = w - margin_l - margin_r
        chart_h = h - margin_t - margin_b

        # Title
        if self._title:
            painter.setPen(QColor("#888"))
            painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
            painter.drawText(margin_l, 16, self._title)

        data = self._data
        min_v = min(data)
        max_v = max(data)
        if self._zero_line:
            min_v = min(min_v, 0)
            max_v = max(max_v, 0)
        v_range = max_v - min_v if max_v != min_v else 1

        def to_point(i, v):
            x = margin_l + (i / (len(data) - 1)) * chart_w
            y = margin_t + chart_h - ((v - min_v) / v_range) * chart_h
            return QPointF(x, y)

        # Zero line
        if self._zero_line and min_v < 0 < max_v:
            zero_y = to_point(0, 0).y()
            painter.setPen(QPen(QColor("#444"), 1, Qt.DashLine))
            painter.drawLine(QPointF(margin_l, zero_y), QPointF(w - margin_r, zero_y))

        # Fill gradient
        if self._fill:
            from PySide6.QtGui import QPolygonF
            polygon = QPolygonF()
            for i, v in enumerate(data):
                polygon.append(to_point(i, v))
            # Close at bottom
            baseline = to_point(len(data) - 1, min_v if not self._zero_line else min(0, min_v))
            polygon.append(QPointF(to_point(len(data) - 1, 0 if self._zero_line and min_v <= 0 else min_v).x(), margin_t + chart_h))
            polygon.append(QPointF(margin_l, margin_t + chart_h))

            grad = QLinearGradient(0, margin_t, 0, margin_t + chart_h)
            fill_color = QColor(self._line_color)
            fill_color.setAlpha(50)
            grad.setColorAt(0, fill_color)
            fill_color.setAlpha(5)
            grad.setColorAt(1, fill_color)
            painter.setBrush(QBrush(grad))
            painter.setPen(Qt.NoPen)
            painter.drawPolygon(polygon)

        # Line
        painter.setPen(QPen(self._line_color, 2))
        for i in range(len(data) - 1):
            painter.drawLine(to_point(i, data[i]), to_point(i + 1, data[i + 1]))

        # Y-axis labels
        painter.setPen(QColor("#666"))
        painter.setFont(QFont("Segoe UI", 8))
        for v in [max_v, (max_v + min_v) / 2, min_v]:
            y = to_point(0, v).y()
            label = f"${v:,.0f}" if abs(v) >= 1 else f"{v:.2f}"
            painter.drawText(QRectF(0, y - 8, margin_l - 4, 16), Qt.AlignRight | Qt.AlignVCenter, label)

        # X-axis labels (show a few evenly spaced)
        if self._labels:
            n_labels = min(5, len(self._labels))
            step = max(1, len(self._labels) // n_labels)
            for i in range(0, len(self._labels), step):
                pt = to_point(i, min_v)
                painter.drawText(QRectF(pt.x() - 25, margin_t + chart_h + 2, 50, 16),
                                 Qt.AlignCenter, self._labels[i])

        painter.end()


class MiniBarChart(QWidget):
    """Simple bar chart for daily PnL, symbol breakdown, etc."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = []      # list of (label, value) tuples
        self._title = ""
        self._positive_color = QColor("#00cc66")
        self._negative_color = QColor("#cc3333")
        self.setMinimumHeight(100)

    def set_data(self, data: list, title: str = ""):
        """data: list of (label, value) tuples"""
        self._data = data
        self._title = title
        self.update()

    def paintEvent(self, event):
        if not self._data:
            painter = QPainter(self)
            painter.setPen(QColor("#555"))
            painter.drawText(self.rect(), Qt.AlignCenter, self._title or "No data")
            painter.end()
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()

        margin_l, margin_r, margin_t, margin_b = 50, 10, 24, 24
        chart_w = w - margin_l - margin_r
        chart_h = h - margin_t - margin_b

        # Title
        if self._title:
            painter.setPen(QColor("#888"))
            painter.setFont(QFont("Segoe UI", 9, QFont.Bold))
            painter.drawText(margin_l, 16, self._title)

        values = [v for _, v in self._data]
        min_v = min(min(values), 0)
        max_v = max(max(values), 0)
        v_range = max_v - min_v if max_v != min_v else 1

        n = len(self._data)
        bar_w = max(2, min(20, chart_w // n - 2))
        spacing = chart_w / n

        zero_y = margin_t + chart_h - ((-min_v) / v_range * chart_h)

        # Zero line
        painter.setPen(QPen(QColor("#444"), 1))
        painter.drawLine(QPointF(margin_l, zero_y), QPointF(w - margin_r, zero_y))

        # Bars
        painter.setFont(QFont("Segoe UI", 7))
        for i, (label, val) in enumerate(self._data):
            x = margin_l + i * spacing + (spacing - bar_w) / 2
            bar_h = abs(val) / v_range * chart_h

            if val >= 0:
                y = zero_y - bar_h
                painter.setBrush(QBrush(self._positive_color))
            else:
                y = zero_y
                painter.setBrush(QBrush(self._negative_color))

            painter.setPen(Qt.NoPen)
            painter.drawRect(QRectF(x, y, bar_w, bar_h))

            # Label
            if n <= 30:
                painter.setPen(QColor("#666"))
                painter.drawText(
                    QRectF(margin_l + i * spacing, margin_t + chart_h + 2, spacing, 18),
                    Qt.AlignCenter, label
                )

        # Y-axis labels
        painter.setPen(QColor("#666"))
        painter.setFont(QFont("Segoe UI", 8))
        for v in [max_v, 0, min_v]:
            y = margin_t + chart_h - ((v - min_v) / v_range * chart_h)
            painter.drawText(QRectF(0, y - 8, margin_l - 4, 16),
                             Qt.AlignRight | Qt.AlignVCenter, f"${v:,.0f}")

        painter.end()


class DonutChart(QWidget):
    """Simple donut/pie chart for win/loss ratio, etc."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._segments = []  # list of (label, value, color_str)
        self._center_text = ""
        self._center_sub = ""
        self.setMinimumSize(100, 100)

    def set_data(self, segments: list, center_text: str = "", center_sub: str = ""):
        """segments: list of (label, value, '#color')"""
        self._segments = segments
        self._center_text = center_text
        self._center_sub = center_sub
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        size = min(self.width(), self.height())
        margin = 8
        outer = size - 2 * margin
        inner = outer * 0.6
        cx = self.width() / 2
        cy = self.height() / 2

        rect = QRectF(cx - outer / 2, cy - outer / 2, outer, outer)

        total = sum(v for _, v, _ in self._segments) if self._segments else 0
        if total <= 0:
            painter.setPen(QColor("#444"))
            painter.drawText(self.rect(), Qt.AlignCenter, "No data")
            painter.end()
            return

        start_angle = 90 * 16  # Start from top
        for label, value, color_str in self._segments:
            span = int(value / total * 360 * 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(color_str)))
            painter.drawPie(rect, start_angle, span)
            start_angle += span

        # Inner circle (donut hole)
        inner_rect = QRectF(cx - inner / 2, cy - inner / 2, inner, inner)
        painter.setBrush(QBrush(QColor("#1a1a2e")))
        painter.drawEllipse(inner_rect)

        # Center text
        painter.setPen(QColor("#e0e0e0"))
        painter.setFont(QFont("Segoe UI", 14, QFont.Bold))
        painter.drawText(QRectF(cx - inner / 2, cy - 14, inner, 24),
                         Qt.AlignCenter, self._center_text)
        if self._center_sub:
            painter.setPen(QColor("#888"))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(QRectF(cx - inner / 2, cy + 8, inner, 16),
                             Qt.AlignCenter, self._center_sub)

        painter.end()
