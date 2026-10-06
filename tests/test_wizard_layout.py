"""Wizard controls stay readable when the page exceeds the display height."""

import pytest
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QComboBox, QDoubleSpinBox, QLineEdit, QPushButton, QScrollArea, QWizard

from desktop_app import setup_wizard as ui


@pytest.mark.parametrize("page_type", [
    ui.OpenAICompatiblePage, ui.ModelsPage, ui.ProviderChoicePage,
    ui.DictationPage, ui.SearchProvidersPage,
    ui.WelcomePage, ui.OllamaInstallPage, ui.OllamaServerPage,
    ui.WhisperSetupPage, ui.LocationPage, ui.MCPPage, ui.CompletePage,
])
@pytest.mark.parametrize("width,height", [(700, 600), (700, 800), (960, 780)])
def test_controls_fit_on_short_display(qapp, monkeypatch, page_type, width, height):
    # Exercise real styled layouts without discovery, installs or config writes.
    monkeypatch.setattr(page_type, "initializePage", lambda self: None)
    wizard = QWizard()
    wizard.setWizardStyle(QWizard.WizardStyle.ModernStyle)
    wizard.setStyleSheet(ui.themed_stylesheet())
    page = page_type()
    wizard.addPage(page)
    wizard.resize(width, height)
    wizard.show()
    QTest.qWait(100)
    try:
        assert wizard.height() <= height
        if isinstance(page, ui.OpenAICompatiblePage):
            page._connect_status.setText("Could not load models. " * 10)
            page._use_ollama_embed.show()
            page._openai_link_cb.setChecked(True)
            page._openai_link_cb.setChecked(False)
            QTest.qWait(100)
            assert wizard.height() <= height
        for control in page.findChildren((QComboBox, QLineEdit, QPushButton)):
            if control.isVisible() and not isinstance(control.parentWidget(), QComboBox):
                assert control.height() >= control.minimumSizeHint().height(), (
                    type(control).__name__, control.height(), control.minimumSizeHint().height()
                )
        scrolls = page.findChildren(QScrollArea)
        assert scrolls
        scroll = scrolls[0]
        assert scroll.horizontalScrollBar().maximum() == 0
        if width == 960:
            assert scroll.verticalScrollBar().maximum() == 0
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        bottom = scroll.widget().mapTo(
            scroll.viewport(), QPoint(0, scroll.widget().height())
        )
        assert bottom.y() <= scroll.viewport().height()
        button = wizard.button(QWizard.WizardButton.FinishButton)
        assert button.isVisible()
        assert wizard.rect().contains(button.geometry())
    finally:
        wizard.close()


def test_wizard_initial_size_fits_available_screen(qapp, monkeypatch):
    monkeypatch.setattr(ui.WhisperSetupPage, "initializePage", lambda self: None)
    wizard = ui.SetupWizard()
    wizard.show()
    QTest.qWait(100)
    try:
        assert wizard.frameGeometry().height() <= wizard.screen().availableGeometry().height()
    finally:
        wizard.close()


@pytest.mark.parametrize("width,height", [(700, 600), (960, 780)])
def test_expanded_memory_estimates_remain_usable(qapp, monkeypatch, width, height):
    monkeypatch.setattr(ui.OpenAICompatiblePage, "initializePage", lambda self: None)
    wizard = QWizard()
    wizard.setStyleSheet(ui.themed_stylesheet())
    page = ui.OpenAICompatiblePage()
    wizard.addPage(page)
    wizard.resize(width, height)
    wizard.show()
    page._memory_toggle.click()
    QTest.qWait(100)
    try:
        assert wizard.height() <= height
        scroll = page.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        for estimate in page.findChildren(QDoubleSpinBox):
            assert estimate.isVisible()
            assert estimate.height() >= estimate.minimumSizeHint().height()
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        bottom = scroll.widget().mapTo(scroll.viewport(), QPoint(0, scroll.widget().height()))
        assert bottom.y() <= scroll.viewport().height()
        assert wizard.button(QWizard.WizardButton.FinishButton).isVisible()
    finally:
        wizard.close()


def _visible_rect(widget, ancestor):
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _stop_centre_x(slider, value, ancestor):
    from PyQt6.QtWidgets import QStyle, QStyleOptionSlider

    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    option.sliderPosition = option.sliderValue = value
    handle = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, slider
    )
    return slider.mapTo(ancestor, handle.center()).x()


@pytest.mark.parametrize("english", [True, False])
@pytest.mark.parametrize("width,height", [(700, 600), (960, 780)])
def test_whisper_slider_labels_sit_at_their_stops_without_overlap(qapp, monkeypatch, english, width, height):
    from types import SimpleNamespace
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication, QLabel

    # Five stops (turbo offered) is the tightest case.
    monkeypatch.setattr(ui, "_is_faster_whisper_turbo_supported", lambda: True)
    monkeypatch.setattr(ui, "load_settings",
                        lambda: SimpleNamespace(whisper_model="small.en" if english else "small"))
    wizard = QWizard()
    wizard.setStyleSheet(ui.themed_stylesheet())
    page = ui.WhisperSetupPage()
    wizard.addPage(page)
    wizard.resize(width, height)
    wizard.show()
    QTest.qWait(100)
    try:
        # The app opens the wizard from inside its running event loop, where Qt holds deferred
        # deletions until the dialog closes: settle layouts only, never deferred deletes.
        page.initializePage()
        page._on_language_changed(not english)
        page._on_language_changed(english)
        for _ in range(3):
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        card = page._model_info_label.parentWidget()
        visible = [l for l in card.findChildren(QLabel) if l.isVisible() and l.text()]
        names = sorted((l for l in visible if l.objectName() == "model_name"), key=lambda l: l.x())
        sizes = sorted((l for l in visible if l.objectName() == "model_size"), key=lambda l: l.x())
        options = page._get_current_model_options()
        assert [l.text() for l in names] == [o[1] for o in options]
        assert len(sizes) == len(options)

        rects = [_visible_rect(l, card) for l in visible]
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                assert not a.intersects(b), (a, b)

        slider = page._model_slider
        slider_rect = _visible_rect(slider, card)
        info_top = _visible_rect(page._model_info_label, card).top()
        for value, (name, size, option) in enumerate(zip(names, sizes, options)):
            stop = _stop_centre_x(slider, value, card)
            name_rect, size_rect = _visible_rect(name, card), _visible_rect(size, card)
            assert option[2] in size.text()
            assert name_rect.left() <= stop <= name_rect.right()
            assert size_rect.left() <= stop <= size_rect.right()
            assert name_rect.bottom() < slider_rect.top()
            assert slider_rect.bottom() < size_rect.top()
            assert size_rect.bottom() < info_top
        scroll = page.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
    finally:
        wizard.close()


@pytest.mark.parametrize("installed", [False, True])
def test_whisper_install_buttons_fit_status_text(qapp, monkeypatch, installed):
    from PyQt6.QtWidgets import QStyle, QStyleOptionButton

    monkeypatch.setattr(ui, "is_apple_silicon", lambda: True)
    monkeypatch.setattr(ui.WhisperSetupPage, "initializePage", lambda self: None)
    monkeypatch.setattr(ui, "check_mlx_whisper_status", lambda: ui.MLXWhisperStatus(
        is_apple_silicon=True,
        is_ffmpeg_installed=installed,
        ffmpeg_path="/opt/homebrew/bin/ffmpeg" if installed else None,
        is_mlx_whisper_installed=installed,
    ))
    wizard = QWizard()
    wizard.setStyleSheet(ui.themed_stylesheet())
    page = ui.WhisperSetupPage()
    wizard.addPage(page)
    wizard.resize(700, 600)
    wizard.show()
    page._refresh_mlx_status()
    QTest.qWait(100)
    try:
        for button in (page.install_ffmpeg_btn, page.install_mlx_btn):
            assert button.isVisible()
            assert button.isEnabled() is not installed
            option = QStyleOptionButton()
            button.initStyleOption(option)
            contents = button.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, option, button
            )
            assert contents.height() >= button.fontMetrics().height()
            assert contents.width() >= button.fontMetrics().horizontalAdvance(button.text())
    finally:
        wizard.close()
