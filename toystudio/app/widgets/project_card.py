"""Project card for the home screen: 16:9 preview, status, name (max 2 lines), type and date."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
    QResizeEvent,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget

from toystudio.app.theme.apply import set_variant
from toystudio.app.widgets.elided import ElidedLabel
from toystudio.app.widgets.status_badge import Status, StatusBadge
from toystudio.core.format import format_timecode

_CARD_MIN_W = 260
_CARD_MAX_W = 480
_CARD_W = 280  # fallback width before the first layout
_MARGIN = 12
_NAME_LINES = 2


class _NameLabel(QLabel):
    """Wraps at width and elides after two lines."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setProperty("role", "heading")
        self.setMinimumHeight(self._line_h() * _NAME_LINES)

    def _line_h(self) -> int:
        return self.fontMetrics().lineSpacing()

    def set_name(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self.update()

    def lines(self) -> list[str]:
        width = self.width() if self.width() > 1 else _CARD_W - 2 * _MARGIN
        layout = QTextLayout(self._full, self.font())
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(opt)
        layout.beginLayout()
        spans: list[tuple[int, int]] = []
        while (line := layout.createLine()).isValid():
            line.setLineWidth(width)
            spans.append((line.textStart(), line.textLength()))
        layout.endLayout()
        out = [self._full[s : s + n].rstrip() for s, n in spans[:_NAME_LINES]]
        if len(spans) > _NAME_LINES:
            start = spans[_NAME_LINES - 1][0]
            out[-1] = self.fontMetrics().elidedText(
                self._full[start:], Qt.TextElideMode.ElideRight, width
            )
        return out

    def sizeHint(self) -> QSize:
        return QSize(_CARD_W - 2 * _MARGIN, self._line_h() * _NAME_LINES)

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setPen(self.palette().windowText().color())
        p.setFont(self.font())
        for i, text in enumerate(self.lines()):
            p.drawText(
                QRect(0, i * self._line_h(), self.width(), self._line_h()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                text,
            )


class ProjectCard(QFrame):
    opened = Signal()
    context_requested = Signal(QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ProjectCard")
        self.setMinimumWidth(_CARD_MIN_W)
        self.setMaximumWidth(_CARD_MAX_W)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._pixmap: QPixmap | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, _MARGIN)
        lay.setSpacing(8)
        self._preview = QLabel()
        self._preview.setObjectName("ProjectPreview")
        self._preview.setFixedHeight(_CARD_W * 9 // 16)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge = StatusBadge(self._preview)
        self._badge.move(8, 8)
        self._duration = QLabel(self._preview)
        self._duration.setObjectName("ProjectDuration")
        self._duration.setProperty("role", "timecode")
        lay.addWidget(self._preview)
        body = QVBoxLayout()
        body.setContentsMargins(_MARGIN, 0, _MARGIN, 0)
        body.setSpacing(4)
        self._name = _NameLabel()
        self._meta = ElidedLabel()
        self._meta.setProperty("role", "caption")
        self._meta.setProperty("tone", "muted")
        self._progress_line = ElidedLabel()
        self._progress_line.setProperty("role", "caption")
        self._progress_line.setProperty("tone", "teal")
        self._bar = QProgressBar()
        set_variant(self._bar, "bar", "thin")
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 100)
        for w in (self._name, self._meta, self._bar, self._progress_line):
            body.addWidget(w)
        lay.addLayout(body)
        self._status: Status = "ready"
        self._percent: int | None = None
        self._eta: str | None = None

    def set_data(
        self,
        name: str,
        kind_text: str,
        date_text: str,
        duration_us: int | None,
        status: str,
        percent: int | None,
        eta_text: str | None,
        preview: QPixmap | None,
    ) -> None:
        self._status = status  # type: ignore[assignment]
        self._percent, self._eta = percent, eta_text
        self._name.set_name(name)
        self._meta.setText(" · ".join(t for t in (kind_text, date_text) if t))
        self._badge.setVisible(status != "files")  # nothing to report before analysis
        if status != "files":
            self._badge.set_status(self._status, percent)
            self._badge.adjustSize()
        self._pixmap = preview if preview is not None and not preview.isNull() else None
        self._layout_preview()
        self._duration.setVisible(duration_us is not None)
        if duration_us is not None:
            self._duration.setText(format_timecode(duration_us))
            self._duration.adjustSize()
            self._place_duration()
        analysing = status == "analysis"
        self._bar.setVisible(analysing and percent is not None)
        self._bar.setValue(percent or 0)
        self._progress_line.setVisible(analysing)
        self.retranslate()

    def _place_duration(self) -> None:
        self._duration.move(
            self.width() - self._duration.width() - 8,
            self._preview.height() - self._duration.height() - 8,
        )

    def _layout_preview(self) -> None:
        w = max(self.width(), 1)
        h = w * 9 // 16
        self._preview.setFixedHeight(h)
        if self._pixmap is not None:
            self._preview.setPixmap(
                self._pixmap.scaled(
                    w,
                    h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self._preview.clear()
        self._place_duration()

    def resizeEvent(self, e: QResizeEvent) -> None:
        super().resizeEvent(e)
        self._layout_preview()

    def retranslate(self) -> None:
        if self._status == "analysis":
            parts = [QCoreApplication.translate("StatusBadge", "Анализ")]
            if self._percent is not None:
                parts.append(f"{self._percent}%")
            if self._eta:
                parts.append(self._eta)
            self._progress_line.setText(" · ".join(parts))
        if self._status != "files":
            self._badge.retranslate()

    def changeEvent(self, e: QEvent) -> None:
        if e.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(e)

    # ---- queries (tests)
    def name_lines(self) -> list[str]:
        return self._name.lines()

    def progress_text(self) -> str:
        return self._progress_line.full_text()

    # ---- interaction
    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.opened.emit()
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit()
        else:
            super().keyPressEvent(e)

    def contextMenuEvent(self, e: QContextMenuEvent) -> None:
        self.context_requested.emit(e.globalPos())
