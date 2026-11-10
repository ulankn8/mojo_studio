"""Screen 3: analysis progress - five stages, overall bar, time left, stop/resume.

The screen only displays its view-model. The Shell keeps the view-models of running
analyses alive and drives the project card, the taskbar and the finish notification.
"""

from __future__ import annotations

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from toystudio.app.i18n import current_language
from toystudio.app.platform.notify import Notifier
from toystudio.app.theme.apply import set_variant, token
from toystudio.app.viewmodels.analysis_vm import AnalysisVM, DraftKind, FeedItem
from toystudio.app.widgets.analysis_stage import AnalysisStage
from toystudio.app.widgets.banner import Banner
from toystudio.app.widgets.clock_dialog import SKIPPED, ClockDialog
from toystudio.app.widgets.empty_state import EmptyState
from toystudio.app.widgets.icons import icon
from toystudio.app.widgets.moment_card import PREVIEW_H, PREVIEW_W, MomentCard
from toystudio.core.format import format_timecode
from toystudio.core.models import ProjectFormat

FEED_COALESCE_MS = 200
_DRAFT_LABELS = {
    "film": QT_TRANSLATE_NOOP("AnalysisScreen", "Черновик до ресторана"),
    "restaurant": QT_TRANSLATE_NOOP("AnalysisScreen", "Черновик ресторана"),
    "premiere": QT_TRANSLATE_NOOP("AnalysisScreen", "Собрать черновик для Premiere"),
    "express": QT_TRANSLATE_NOOP("AnalysisScreen", "Собрать экспресс-видео"),
    "sde": QT_TRANSLATE_NOOP("AnalysisScreen", "Собрать клип Love Story"),
}
_DRAFT_ERRORS = {
    "no_synced_cameras": QT_TRANSLATE_NOOP(
        "AnalysisScreen", "Камеры не совпали со звуком — черновик не из чего собрать."
    ),
    "nothing_to_keep": QT_TRANSLATE_NOOP(
        "AnalysisScreen", "В записи не нашлось речи и танцев — черновик пустой."
    ),
    "clocks": QT_TRANSLATE_NOOP(
        "AnalysisScreen", "Часы камер не сверены — сверьте их, чтобы собрать черновик."
    ),
    "error": QT_TRANSLATE_NOOP("AnalysisScreen", "Не удалось собрать черновик."),
}
FEED_LIMIT = 50  # cards shown; the rest is a single «и ещё N» line


class AnalysisScreen(QWidget):
    def __init__(
        self,
        vm: AnalysisVM,
        notifier: Notifier | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.vm = vm
        self._notifier = notifier or Notifier(QApplication.windowIcon())
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)
        columns = QHBoxLayout()
        columns.setSpacing(32)

        # ---- left: progress
        left = QVBoxLayout()
        left.setSpacing(8)
        self.error_banner = Banner("error")
        self.error_banner.hide()
        self.overall_bar = QProgressBar()
        set_variant(self.overall_bar, "bar", "thin")
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setTextVisible(False)
        self.eta_label = QLabel()
        self.eta_label.setProperty("role", "timecode-lg")
        self._offline_note = QLabel()
        self._offline_note.setProperty("role", "caption")
        self._offline_note.setProperty("tone", "muted")
        left.addWidget(self.error_banner)
        left.addWidget(self.overall_bar)
        left.addWidget(self.eta_label)
        left.addWidget(self._offline_note)
        left.addSpacing(8)
        self.stage_widgets = [AnalysisStage(v.title) for v in vm.stages]
        for w in self.stage_widgets:
            left.addWidget(w)
        failed_row = QHBoxLayout()
        self.failed_label = QLabel()
        self.failed_label.setProperty("role", "body")
        self.failed_label.setProperty("tone", "error")
        self.failed_label.setWordWrap(True)
        self.retry_button = QPushButton()
        self.replace_hint = QLabel()  # corrupt files fail again on rerun: replace them
        self.replace_hint.setProperty("role", "body")
        self.replace_hint.setProperty("tone", "muted")
        failed_row.addWidget(self.failed_label, 1)
        failed_row.addWidget(self.retry_button)
        failed_row.addWidget(self.replace_hint)
        self._failed_box = QWidget()
        self._failed_box.setLayout(failed_row)
        left.addWidget(self._failed_box)
        missing_row = QHBoxLayout()
        self.missing_label = QLabel()
        self.missing_label.setProperty("role", "body")
        self.missing_label.setProperty("tone", "error")
        self.missing_label.setWordWrap(True)
        self.missing_retry_button = QPushButton()
        missing_row.addWidget(self.missing_label, 1)
        missing_row.addWidget(self.missing_retry_button)
        self._missing_box = QWidget()
        self._missing_box.setLayout(missing_row)
        left.addWidget(self._missing_box)
        self.saved_label = QLabel()
        self.saved_label.setProperty("role", "body")
        self.saved_label.setProperty("tone", "muted")
        self.done_label = QLabel()
        self.done_label.setProperty("role", "heading")
        self.done_label.setProperty("tone", "done")
        left.addWidget(self.saved_label)
        left.addWidget(self.done_label)
        # drafts to finish in Premiere, by format: a quiet panel under «Анализ завершён»
        self._draft_box = QFrame()
        self._draft_box.setObjectName("DraftPanel")
        panel = QVBoxLayout(self._draft_box)
        panel.setContentsMargins(20, 16, 20, 16)
        panel.setSpacing(10)
        self._draft_title = QLabel()
        self._draft_title.setProperty("role", "heading")
        panel.addWidget(self._draft_title)
        draft_row = QHBoxLayout()
        draft_row.setSpacing(8)
        self.draft_buttons: dict[DraftKind, QPushButton] = {}
        kinds: tuple[DraftKind, ...] = ("film", "restaurant", "express", "sde")
        for kind in kinds:
            b = QPushButton()
            b.clicked.connect(lambda _=False, k=kind: self._build(k))
            self.draft_buttons[kind] = b
            draft_row.addWidget(b)
        draft_row.addStretch(1)
        self.open_folder_button = QPushButton()
        set_variant(self.open_folder_button, "variant", "ghost")
        draft_row.addWidget(self.open_folder_button)
        panel.addLayout(draft_row)
        self.draft_label = QLabel()
        self.draft_label.setProperty("role", "body")
        self.draft_label.setWordWrap(True)
        panel.addWidget(self.draft_label)
        left.addSpacing(8)
        left.addWidget(self._draft_box)
        left.addStretch(1)
        columns.addLayout(left, 60)

        # ---- right: found so far, newest moment on top
        right = QVBoxLayout()
        self._found_title = QLabel()
        self._found_title.setProperty("role", "heading")
        self.found_empty = EmptyState("film", "", "", None)
        self._feed_box = QVBoxLayout()
        self._feed_box.setSpacing(8)
        self._feed_box.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._more_label = QLabel()
        self._more_label.setProperty("role", "caption")
        self._more_label.setProperty("tone", "muted")
        feed_body = QWidget()
        feed_body.setLayout(self._feed_box)
        self._feed_scroll = QScrollArea()
        self._feed_scroll.setWidgetResizable(True)
        self._feed_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._feed_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._feed_scroll.setWidget(feed_body)
        right.addWidget(self._found_title)
        right.addWidget(self.found_empty, 1)
        right.addWidget(self._feed_scroll, 1)
        columns.addLayout(right, 40)
        root.addLayout(columns, 1)

        # ---- bottom
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.notify_check = QCheckBox()
        self.notify_check.setChecked(vm.notify_when_done)
        self.minimize_button = QPushButton()
        self.stop_button = QPushButton()
        set_variant(self.stop_button, "variant", "ghost")
        self.resume_button = QPushButton()
        set_variant(self.resume_button, "variant", "primary")
        bottom.addWidget(self.notify_check)
        bottom.addStretch(1)
        for b in (self.minimize_button, self.stop_button, self.resume_button):
            bottom.addWidget(b)
        root.addLayout(bottom)

        self.stop_button.clicked.connect(vm.stop)
        self.open_folder_button.clicked.connect(self._open_draft_folder)
        self.resume_button.clicked.connect(vm.resume)
        self.retry_button.clicked.connect(vm.resume)
        self.missing_retry_button.clicked.connect(vm.resume)
        self.error_banner.action.connect(lambda _key: vm.resume())
        self.minimize_button.clicked.connect(lambda: self._notifier.minimize_to_tray(self.window()))
        self.notify_check.toggled.connect(lambda on: setattr(vm, "notify_when_done", on))
        vm.changed.connect(self._render)
        vm.finished.connect(self._on_finished)
        vm.failed.connect(self._on_failed)
        self._failure: tuple[str, str] | None = None
        self._cards: list[MomentCard] = []
        self._feed_shown: int | None = None  # feed_version on screen
        self._epoch_shown: int | None = None
        self._card_ids: list[int] = []
        self._pixmaps: dict[int, QPixmap] = {}  # scaled previews by moment id
        self.feed_rebuilds = 0
        self._feed_timer = QTimer(self)
        self._feed_timer.setSingleShot(True)
        self._feed_timer.setInterval(FEED_COALESCE_MS)
        self._feed_timer.timeout.connect(self._flush_feed)
        self.retranslate()

    # ---- vm -> widgets
    def _render(self) -> None:
        vm = self.vm
        if vm.running:
            self._failure = None  # a new run supersedes the old crash banner
        for w, view in zip(self.stage_widgets, vm.stages, strict=True):
            w.set_title(view.title)
            w.set_state(view.state, view.found_text, view.percent)
        self.overall_bar.setValue(round(vm.overall_percent))
        self.eta_label.setText(vm.eta_text or "")
        self.eta_label.setVisible(vm.running and bool(vm.eta_text))
        running, resume = vm.running, vm.can_resume
        self.notify_check.setVisible(running)
        self.minimize_button.setVisible(running)
        self.stop_button.setVisible(running)
        self.stop_button.setEnabled(not vm.stopping)
        self.resume_button.setVisible(resume)
        self.saved_label.setVisible(resume)
        self.done_label.setVisible(vm.done and not vm.all_failed)
        self._draft_box.setVisible(vm.can_draft)
        for kind, b in self.draft_buttons.items():
            b.setVisible(kind in vm.draft_kinds)
            first = bool(vm.draft_kinds) and kind == vm.draft_kinds[0]
            set_variant(b, "variant", "primary" if first else "")
        self.draft_label.setText(self.draft_text())
        self.draft_label.setVisible(bool(self.draft_label.text()))
        set_variant(self.draft_label, "tone", "error" if vm.draft_error else "done")
        self.open_folder_button.setVisible(vm.draft is not None)
        self._offline_note.setVisible(running)
        failed = [] if running else vm.failed_files
        self._failed_box.setVisible(bool(failed) and not vm.all_failed)
        self.retry_button.setVisible(vm.failed_retryable)
        self.replace_hint.setVisible(not vm.failed_retryable)
        self.failed_label.setText(
            QCoreApplication.translate("AnalysisScreen", "Не удалось проанализировать: %1").replace(
                "%1", ", ".join(failed)
            )
        )
        missing = [] if running else vm.missing_files
        self._missing_box.setVisible(bool(missing))
        self.missing_label.setText(
            "\n".join(
                QCoreApplication.translate(
                    "AnalysisScreen",
                    "Файл не найден: %1. Подключите диск с ним или замените файл.",
                ).replace("%1", name)
                for name in missing
            )
        )
        self._render_banner()
        self._render_feed()

    def _render_feed(self, force: bool = False) -> None:
        vm = self.vm
        has = bool(vm.feed)
        self.found_empty.setVisible(not has)
        self._feed_scroll.setVisible(has)
        if force or self._epoch_shown != vm.feed_epoch:
            self._rebuild_feed()
        elif self._feed_shown != vm.feed_version and not self._feed_timer.isActive():
            self._feed_timer.start()  # a burst of events becomes one update

    def _rebuild_feed(self) -> None:
        vm = self.vm
        self._feed_timer.stop()
        self.feed_rebuilds += 1
        self._feed_shown, self._epoch_shown = vm.feed_version, vm.feed_epoch
        for card in self._cards:
            self._feed_box.removeWidget(card)
            card.hide()  # out of the layout it would linger at the top-left until deleted
            card.deleteLater()
        self._cards, self._card_ids = [], []
        shown = vm.feed[:FEED_LIMIT]
        self._pixmaps = {k: v for k, v in self._pixmaps.items() if k in {i.id for i in shown}}
        for item in shown:
            card = MomentCard()
            self._fill_card(card, item)
            self._feed_box.addWidget(card)
            self._cards.append(card)
            self._card_ids.append(item.id)
        self._feed_box.removeWidget(self._more_label)
        self._feed_box.addWidget(self._more_label)
        self._update_more()

    def _flush_feed(self) -> None:
        """Prepend the cards of moments found since the last update and trim the tail."""
        vm = self.vm
        if self._epoch_shown != vm.feed_epoch:
            self._rebuild_feed()
            return
        self._feed_shown = vm.feed_version
        known = set(self._card_ids)
        fresh = []
        for item in vm.feed:
            if item.id in known:
                break
            fresh.append(item)
        for item in reversed(fresh[:FEED_LIMIT]):  # oldest of the new ones first
            card = MomentCard()
            self._fill_card(card, item)
            self._feed_box.insertWidget(0, card)
            self._cards.insert(0, card)
            self._card_ids.insert(0, item.id)
        while len(self._cards) > FEED_LIMIT:
            card = self._cards.pop()
            self._pixmaps.pop(self._card_ids.pop(), None)
            self._feed_box.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._update_more()

    def _update_more(self) -> None:
        extra = len(self.vm.feed) - FEED_LIMIT
        self._more_label.setText(self._more_text(extra))
        self._more_label.setVisible(extra > 0)

    def _fill_card(self, card: MomentCard, item: FeedItem) -> None:
        pixmap: QPixmap | None = None
        if item.preview:
            pixmap = self._pixmaps.get(item.id)
            if pixmap is None:
                loaded = QPixmap(str(self.vm.root / item.preview))
                if not loaded.isNull():  # a lost file is just a placeholder
                    pixmap = loaded.scaled(
                        PREVIEW_W,
                        PREVIEW_H,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self._pixmaps[item.id] = pixmap
        ky = current_language() == "ky"
        reasons = [r.ky if ky else r.ru for r in item.reasons]
        card.set_data(
            item.type, item.start_us, item.duration_us, item.quality, pixmap, reasons, item.mark
        )

    @staticmethod
    def _more_text(n: int) -> str:
        return QCoreApplication.translate("AnalysisScreen", "и ещё %1").replace(
            "%1", str(max(n, 0))
        )

    # ---- queries (tests)
    def feed_cards(self) -> list[MomentCard]:
        return list(self._cards)

    def more_text(self) -> str:
        return self._more_label.text() if not self._more_label.isHidden() else ""

    def _render_banner(self) -> None:
        vm = self.vm
        if self._failure is not None and not vm.running:
            code, detail = self._failure
            title = QCoreApplication.translate("AnalysisScreen", "Анализ остановился из-за ошибки.")
            body = QCoreApplication.translate(
                "AnalysisScreen", "Пройденные этапы сохранены. Попробуйте продолжить."
            )
            self.error_banner.set_content(
                title,
                body,
                [("retry", QCoreApplication.translate("AnalysisScreen", "Повторить"))],
                f"{code}: {detail}".strip(": "),
            )
            self.error_banner.show()
        elif vm.all_failed and not vm.running:
            title = QCoreApplication.translate(
                "AnalysisScreen", "Ни один файл не удалось проанализировать."
            )
            body = QCoreApplication.translate("AnalysisScreen", "Проверьте файлы на шаге «Файлы».")
            self.error_banner.set_content(title, body, [])
            self.error_banner.show()
        else:
            self.error_banner.hide()

    def _on_finished(self, _cuts: int) -> None:
        self._failure = None
        self._render()

    def _on_failed(self, code: str, detail: str) -> None:
        self._failure = (code, detail)
        self._render()

    # ---- i18n
    def draft_text(self) -> str:
        vm = self.vm
        if vm.draft is not None:
            st = vm.draft.stats
            return (
                QCoreApplication.translate(
                    "AnalysisScreen", "Черновик: %1, склеек %2, убрано пауз и музыки %3"
                )
                .replace("%1", format_timecode(st.duration_us))
                .replace("%2", str(st.cuts))
                .replace("%3", format_timecode(st.dropped_us))
            )
        if vm.draft_error is None:
            return ""
        return QCoreApplication.translate("AnalysisScreen", _DRAFT_ERRORS.get(vm.draft_error,
                                          _DRAFT_ERRORS["error"]))  # fmt: skip

    def _build(self, kind: DraftKind) -> None:
        """Build a draft; when camera clocks disagree, match them first (or skip) and retry."""
        self.vm.build_draft(kind)
        if self.vm.draft_error != "clocks":
            return
        dialog = ClockDialog(self.vm.root, self)
        result = dialog.exec() if dialog.result() == 0 else dialog.result()
        if result == QDialog.DialogCode.Accepted:
            self.vm.build_draft(kind)
        elif result == SKIPPED:
            self.vm.build_draft(kind, skip_clocks=True)

    def _open_draft_folder(self) -> None:
        if self.vm.draft is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.vm.draft.xml_path.parent)))

    def retranslate(self) -> None:
        self.vm.retranslate()
        for kind, b in self.draft_buttons.items():
            source = _DRAFT_LABELS[kind]
            if kind == "restaurant" and self.vm.format is not ProjectFormat.WEDDING_FULL:
                source = _DRAFT_LABELS["premiere"]
            text = QCoreApplication.translate("AnalysisScreen", source)
            b.setText(text)
            # the theme's padding is not in the size hint: keep the whole label visible
            b.setMinimumWidth(b.fontMetrics().horizontalAdvance(text) + 56)
        self.open_folder_button.setText(
            QCoreApplication.translate("AnalysisScreen", "Открыть папку")
        )
        self.open_folder_button.setIcon(icon("folder", token("ink-muted"), 16))
        self._draft_title.setText(
            QCoreApplication.translate("AnalysisScreen", "Черновики для Premiere")
        )
        self.draft_label.setText(self.draft_text())
        self._offline_note.setText(
            QCoreApplication.translate(
                "AnalysisScreen", "Анализ идёт на этом компьютере, интернет не нужен"
            )
        )
        self._found_title.setText(QCoreApplication.translate("AnalysisScreen", "Уже найдено"))
        self.found_empty.set_texts(
            QCoreApplication.translate(
                "AnalysisScreen", "Моменты появятся после поиска тостов и танцев"
            ),
            "",
            None,
        )
        self.notify_check.setText(
            QCoreApplication.translate("AnalysisScreen", "Уведомить, когда закончится")
        )
        self.notify_check.setIcon(icon("bell", token("ink-muted"), 16))
        self.minimize_button.setText(QCoreApplication.translate("AnalysisScreen", "Свернуть"))
        self.stop_button.setText(QCoreApplication.translate("AnalysisScreen", "Остановить"))
        self.resume_button.setText(
            QCoreApplication.translate("AnalysisScreen", "Продолжить анализ")
        )
        self.retry_button.setText(QCoreApplication.translate("AnalysisScreen", "Повторить"))
        self.missing_retry_button.setText(QCoreApplication.translate("AnalysisScreen", "Повторить"))
        self.replace_hint.setText(
            QCoreApplication.translate("AnalysisScreen", "Замените файл на шаге «Файлы».")
        )
        self.saved_label.setText(
            QCoreApplication.translate("AnalysisScreen", "Пройденные этапы сохранены")
        )
        self.done_label.setText(QCoreApplication.translate("AnalysisScreen", "Анализ завершён"))
        self._render_feed(force=True)  # reasons depend on the language; rebuilt once
        self._render()

    def changeEvent(self, e: QEvent) -> None:
        if e.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(e)

    def flush_feed(self) -> None:
        """Apply pending feed updates now (tests; the timer does it otherwise)."""
        if self._feed_timer.isActive():
            self._feed_timer.stop()
            self._flush_feed()
