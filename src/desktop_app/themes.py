"""
🎨 Jarvis UI Theme

The one look every Jarvis window wears: the cinematic cyan/blue HUD of the live
orb. The orb's own colours (``ORB_PALETTE``) are the single source;
``HUD_COLORS`` derives the UI palette from them, ``HUD_THEME_STYLESHEET``
renders the shared Qt stylesheet from that palette, and ``LINE_ICONS`` are
the line icons drawn in it. Window modules name roles (object names and a few
dynamic properties) and never carry colours or stylesheets of their own.

Nothing drawn on screen uses emoji: text is plain, and icons are the line
icons below.
"""

from __future__ import annotations

import os
import tempfile
from string import Template
from typing import Optional

# Colours of the live orb (``orb_widget``). The orb paints its states from
# these, and the HUD palette below is derived from them, so every window and
# the orb stay one look. Green, amber and red are the status hues.
ORB_PALETTE = {
    "backdrop": "#05070c",
    "cyan": "#22d3ee",
    "cyan_light": "#67e8f9",
    "sky": "#38bdf8",
    "blue_light": "#60a5fa",
    "blue": "#3b82f6",
    "indigo": "#818cf8",
    "slate_dark": "#334155",
    "slate": "#475569",
    "slate_mid": "#64748b",
    "slate_light": "#94a3b8",
    "white": "#ffffff",
    "green": "#22c55e",
    "green_light": "#4ade80",
    "amber": "#f59e0b",
    "amber_light": "#fbbf24",
    "red": "#ef4444",
    "red_light": "#f87171",
}

# The UI palette, taken from the orb. Muted text is the orb's lightest slate:
# its mid slate is under 4.5:1 on the orb backdrop. Success, warning and error
# keep their familiar hues. ``hairline`` is the faint cyan edge of HUD panels
# and dividers; ``panel_top`` is the top-lit end of a panel's gradient.
HUD_COLORS = {
    "bg_primary": ORB_PALETTE["backdrop"],
    "bg_secondary": "#0b1220",
    "bg_tertiary": "#111c30",
    "bg_card": "#0d1626",
    "bg_hover": "#16233b",
    "panel_top": "#0f1b2e",

    "accent_primary": ORB_PALETTE["cyan"],
    "accent_secondary": ORB_PALETTE["cyan_light"],
    "accent_deep": ORB_PALETTE["blue"],
    "accent_indigo": ORB_PALETTE["indigo"],
    "accent_glow": "rgba(34, 211, 238, 0.15)",
    "indigo_glow": "rgba(129, 140, 248, 0.12)",
    "accent_muted": ORB_PALETTE["slate_dark"],

    "text_primary": "#e2e8f0",
    "text_secondary": "#cbd5e1",
    "text_muted": ORB_PALETTE["slate_light"],

    "border": "#1e293b",
    "border_strong": ORB_PALETTE["slate"],
    "border_glow": "rgba(34, 211, 238, 0.3)",
    "hairline": "rgba(34, 211, 238, 0.16)",

    "success": ORB_PALETTE["green"],
    "success_light": ORB_PALETTE["green_light"],
    "success_deep": "#16a34a",
    "success_glow": "rgba(34, 197, 94, 0.12)",
    "success_border": "rgba(34, 197, 94, 0.3)",
    "warning": ORB_PALETTE["amber"],
    "warning_light": ORB_PALETTE["amber_light"],
    "warning_glow": "rgba(245, 158, 11, 0.12)",
    "warning_border": "rgba(245, 158, 11, 0.3)",
    "error": ORB_PALETTE["red"],
    "error_light": ORB_PALETTE["red_light"],
    "error_glow": "rgba(239, 68, 68, 0.15)",
    "error_border": "rgba(239, 68, 68, 0.35)",
}

# The light variant of the UI palette, for the web chat's light mode (the Qt windows stay dark). It has
# the same keys as ``HUD_COLORS``; text and accents meet WCAG AA (4.5:1) on the surfaces they sit on.
HUD_COLORS_LIGHT = {
    "bg_primary": "#f4f7fb",
    "bg_secondary": "#ffffff",
    "bg_tertiary": "#e6edf6",
    "bg_card": "#ffffff",
    "bg_hover": "#dbe6f3",
    "panel_top": "#ffffff",

    "accent_primary": "#0b6a84",
    "accent_secondary": "#0b6f8a",
    "accent_deep": "#1d4ed8",
    "accent_indigo": "#4f46e5",
    "accent_glow": "rgba(14, 116, 144, 0.12)",
    "indigo_glow": "rgba(79, 70, 229, 0.08)",
    "accent_muted": "#cbd5e1",

    "text_primary": "#0f172a",
    "text_secondary": "#334155",
    "text_muted": "#475569",

    "border": "#cbd5e1",
    "border_strong": "#94a3b8",
    "border_glow": "rgba(14, 116, 144, 0.35)",
    "hairline": "rgba(14, 116, 144, 0.22)",

    "success": "#15803d",
    "success_light": "#16a34a",
    "success_deep": "#166534",
    "success_glow": "rgba(21, 128, 61, 0.10)",
    "success_border": "rgba(21, 128, 61, 0.3)",
    "warning": "#b45309",
    "warning_light": "#d97706",
    "warning_glow": "rgba(180, 83, 9, 0.10)",
    "warning_border": "rgba(180, 83, 9, 0.3)",
    "error": "#b91c1c",
    "error_light": "#dc2626",
    "error_glow": "rgba(185, 28, 28, 0.10)",
    "error_border": "rgba(185, 28, 28, 0.35)",
}

# Type: the system UI face, and a monospace face for values, codes and logs.
FONT_UI = "'Segoe UI', '.AppleSystemUIFont', sans-serif"
FONT_MONO = "'Cascadia Mono', Consolas, 'SF Mono', Menlo, monospace"

_TOKENS = {**HUD_COLORS, "font_ui": FONT_UI, "font_mono": FONT_MONO}


# The shared Qt stylesheet. Colours are ``$palette_key`` tokens filled from
# ``HUD_COLORS``. Roles are object names (``QLabel#eyebrow``) and, for states
# that change at run time, dynamic properties set through ``set_role`` and
# ``set_state``.
_THEME_TEMPLATE = Template("""
    QMainWindow, QDialog, QWizard, QWizardPage {
        background-color: $bg_primary;
    }

    QWidget {
        background-color: $bg_primary;
        color: $text_primary;
        font-family: $font_ui;
    }

    /* ---- Text roles ---------------------------------------------------- */

    QLabel {
        color: $text_primary;
        background: transparent;
    }

    QLabel#eyebrow {
        color: $accent_primary;
        font-size: 10px;
        font-weight: 600;
    }

    QLabel#title {
        font-size: 20px;
        font-weight: 600;
        color: $text_primary;
    }

    QLabel#title-error {
        font-size: 20px;
        font-weight: 600;
        color: $error_light;
    }

    QLabel#subtitle {
        font-size: 12px;
        color: $text_muted;
    }

    QLabel#section_title {
        font-size: 14px;
        font-weight: 600;
        color: $accent_secondary;
    }

    QLabel#heading {
        font-size: 13px;
        font-weight: 600;
        color: $text_primary;
    }

    QLabel#body {
        font-size: 13px;
        color: $text_secondary;
    }

    QLabel#heading_large {
        font-size: 15px;
        font-weight: 600;
        color: $text_primary;
    }

    QLabel#emphasis {
        font-size: 14px;
        font-weight: 600;
        color: $text_primary;
    }

    QLabel#description {
        font-size: 13px;
        color: $text_muted;
    }

    QLabel#detail {
        font-size: 12px;
        color: $text_secondary;
    }

    QLabel#detail_muted {
        font-size: 12px;
        color: $text_muted;
        padding-top: 2px;
    }

    QLabel#detail_error {
        font-size: 12px;
        color: $error_light;
        padding-top: 2px;
    }

    QLabel#caption {
        font-size: 10px;
        color: $text_muted;
    }

    QLabel#hint_indented {
        font-size: 11px;
        color: $text_muted;
        padding-left: 24px;
    }

    QLabel#memory_summary {
        font-size: 13px;
        color: $text_secondary;
    }

    QLabel[tone="success"] { color: $success_light; }
    QLabel[tone="warning"] { color: $warning_light; }
    QLabel[tone="error"] { color: $error_light; }
    QLabel[tone="muted"] { color: $text_muted; }

    QLabel#muted {
        color: $text_muted;
    }

    QLabel#hint {
        font-size: 11px;
        color: $text_muted;
    }

    QLabel#key {
        font-size: 13px;
        color: $text_muted;
    }

    QLabel#value {
        font-size: 13px;
        color: $text_primary;
        font-family: $font_mono;
    }

    QLabel#code {
        font-size: 28px;
        font-weight: 700;
        color: $accent_primary;
        font-family: $font_mono;
    }

    QLabel#link {
        color: $accent_secondary;
        font-family: $font_mono;
    }

    QLabel#empty_state {
        color: $text_muted;
        font-size: 14px;
        padding: 40px;
    }

    QLabel#entry_text {
        color: $text_primary;
        font-size: 14px;
        padding: 4px 0;
    }

    QLabel#badge, QLabel#badge-success, QLabel#badge-warning {
        border-radius: 4px;
        font-size: 11px;
        font-weight: 700;
        padding: 2px 8px;
    }

    QLabel#badge {
        background-color: $accent_glow;
        color: $accent_secondary;
        border: 1px solid $border_glow;
    }

    QLabel#badge-success {
        background-color: $success_glow;
        color: $success_light;
        border: 1px solid $success_border;
    }

    QLabel#badge-warning {
        background-color: $warning_glow;
        color: $warning_light;
        border: 1px solid $warning_border;
    }

    QLabel#chip {
        color: $text_muted;
        background-color: $bg_tertiary;
        border-radius: 3px;
        font-size: 10px;
        padding: 1px 5px;
    }

    QLabel#bullet {
        color: $accent_primary;
        font-size: 14px;
    }

    QLabel#change {
        color: $text_secondary;
        font-size: 12px;
    }

    QLabel#disclosure {
        color: $text_muted;
        font-size: 14px;
        padding-left: 4px;
    }

    QFrame#version_header {
        background-color: $bg_card;
        border: 1px solid $hairline;
        border-radius: 8px;
    }

    QFrame#version_header:hover {
        background-color: $bg_hover;
        border-color: $border_glow;
    }

    QWidget#version_content {
        background-color: $bg_secondary;
        border: 1px solid $hairline;
        border-top: none;
        border-bottom-left-radius: 8px;
        border-bottom-right-radius: 8px;
    }

    QWidget#version_content QWidget {
        background: transparent;
    }

    QLabel#field_label {
        color: $text_secondary;
    }

    QLabel#accent_hint {
        color: $accent_secondary;
        font-size: 11px;
    }

    QLabel#detail_panel {
        background-color: $bg_secondary;
        border: 1px solid $hairline;
        border-radius: 8px;
        padding: 12px;
        color: $text_muted;
        font-size: 12px;
    }

    QLabel#notice_error {
        background: $error_glow;
        border: 1px solid $error_border;
        border-radius: 8px;
        padding: 10px 14px;
        color: $error_light;
        font-size: 12px;
    }

    QLabel#status-success { color: $success_light; }
    QLabel#status-warning { color: $warning_light; }
    QLabel#status-error { color: $error_light; }
    QLabel#status-muted { color: $text_muted; }

    QFrame#divider {
        background-color: $hairline;
        border: none;
        min-height: 1px;
        max-height: 1px;
    }

    /* ---- Panels -------------------------------------------------------- */

    QFrame#card, QWidget#download_card, QFrame#dictation_card {
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 $panel_top, stop:1 $bg_card);
        border: 1px solid $hairline;
        border-radius: 10px;
    }

    QFrame#card {
        padding: 16px;
    }

    QFrame#dictation_card:hover {
        border-color: $border_glow;
    }

    QFrame#card QWidget, QWidget#download_card QWidget, QFrame#dictation_card QWidget {
        background: transparent;
    }

    QWidget#clear, QWidget#memoryDetails {
        background: transparent;
    }

    QFrame#inset {
        background-color: $bg_secondary;
        border: 1px solid $hairline;
        border-radius: 8px;
    }

    QFrame#inset QLabel {
        border: none;
        background: transparent;
    }

    /* ---- Inputs -------------------------------------------------------- */

    QTextEdit, QPlainTextEdit {
        background-color: $bg_secondary;
        color: $text_primary;
        border: 1px solid $border;
        border-radius: 8px;
        padding: 10px;
        selection-background-color: $border_glow;
        selection-color: $accent_secondary;
    }

    QTextEdit:focus, QPlainTextEdit:focus {
        border-color: $accent_primary;
    }

    QTextEdit#console, QTextBrowser#console {
        font-family: $font_mono;
        font-size: 11px;
        color: $text_muted;
    }

    QLineEdit {
        background-color: $bg_secondary;
        color: $text_primary;
        border: 1px solid $border;
        border-radius: 6px;
        padding: 7px 10px;
        selection-background-color: $border_glow;
    }

    QLineEdit:hover {
        border-color: $border_strong;
    }

    QLineEdit:focus {
        border-color: $accent_primary;
    }

    QLineEdit::placeholder {
        color: $text_muted;
    }

    /* ---- Buttons ------------------------------------------------------- */

    QPushButton {
        background-color: $bg_tertiary;
        color: $text_primary;
        border: 1px solid $hairline;
        border-radius: 6px;
        padding: 8px 18px;
        font-weight: 600;
    }

    QPushButton:hover {
        background-color: $accent_glow;
        border-color: $accent_primary;
        color: $accent_secondary;
    }

    QPushButton:pressed {
        background-color: $border_glow;
    }

    QPushButton:focus {
        border-color: $border_glow;
    }

    QPushButton:disabled {
        background-color: $bg_secondary;
        color: $text_muted;
        border-color: $border;
    }

    QPushButton[compact="true"] {
        padding: 4px 12px;
        font-size: 12px;
    }

    QPushButton#primary {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 $accent_primary, stop:1 $accent_deep);
        color: $bg_primary;
        border: none;
    }

    QPushButton#primary:hover {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 $accent_secondary, stop:1 $accent_primary);
        color: $bg_primary;
    }

    QPushButton#primary:disabled {
        background: $border;
        color: $text_muted;
    }

    QPushButton#secondary {
        background-color: $bg_tertiary;
        color: $text_primary;
    }

    QPushButton#secondary:hover {
        background-color: $accent_glow;
        border-color: $accent_primary;
        color: $accent_secondary;
    }

    QPushButton#danger {
        background-color: transparent;
        border-color: $error_border;
        color: $error_light;
    }

    QPushButton#danger:hover {
        background-color: $error_glow;
        border-color: $error;
        color: $error_light;
    }

    QPushButton#success {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 $success, stop:1 $success_deep);
        color: $bg_primary;
        border: none;
    }

    QPushButton#success:hover {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 $success_light, stop:1 $success);
        color: $bg_primary;
    }

    QPushButton#toggle {
        background-color: $bg_secondary;
        color: $text_secondary;
    }

    QPushButton#toggle:checked {
        background-color: $accent_glow;
        border-color: $accent_primary;
        color: $accent_secondary;
    }

    /* ---- Choices ------------------------------------------------------- */

    QComboBox {
        background-color: $bg_secondary;
        color: $text_primary;
        border: 1px solid $border;
        border-radius: 6px;
        padding: 7px 10px;
        min-width: 120px;
    }

    QComboBox:hover {
        border-color: $accent_primary;
    }

    QComboBox::drop-down {
        border: none;
        width: 24px;
    }

    QComboBox::down-arrow {
        margin-right: 8px;
    }

    QComboBox QAbstractItemView {
        background-color: $bg_card;
        color: $text_primary;
        border: 1px solid $border_glow;
        border-radius: 6px;
        selection-background-color: $accent_glow;
        selection-color: $accent_secondary;
    }

    QCheckBox {
        color: $text_primary;
        spacing: 8px;
        background: transparent;
    }

    QCheckBox::indicator {
        width: 18px;
        height: 18px;
        border: 1px solid $border_strong;
        border-radius: 4px;
        background-color: transparent;
    }

    QCheckBox::indicator:hover {
        border-color: $accent_primary;
    }

    QCheckBox::indicator:checked {
        background-color: $accent_primary;
        border-color: $accent_primary;
    }

    QCheckBox::indicator:disabled {
        border-color: $border;
    }

    QRadioButton {
        color: $text_primary;
        spacing: 8px;
        background: transparent;
    }

    QRadioButton::indicator {
        width: 18px;
        height: 18px;
        border: 1px solid $border_strong;
        border-radius: 9px;
        background-color: $bg_secondary;
    }

    QRadioButton::indicator:hover {
        border-color: $accent_primary;
    }

    QRadioButton::indicator:checked {
        background-color: $accent_primary;
        border-color: $accent_primary;
    }

    QRadioButton#choice {
        font-size: 15px;
        font-weight: 600;
    }

    QCheckBox#option, QRadioButton#option {
        font-size: 14px;
        color: $text_primary;
        padding: 4px 0;
    }

    QSlider {
        background: transparent;
    }

    QSlider:horizontal {
        min-height: 32px;
    }

    QSlider::groove:horizontal {
        border: 1px solid $border;
        height: 4px;
        background: $bg_tertiary;
        border-radius: 2px;
        margin: 0;
    }

    QSlider::sub-page:horizontal {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 $accent_deep, stop:1 $accent_primary);
        border-radius: 2px;
    }

    QSlider::handle:horizontal {
        background: $accent_primary;
        border: none;
        width: 16px;
        height: 16px;
        margin: -6px 0;
        border-radius: 8px;
    }

    QSlider::handle:horizontal:hover {
        background: $accent_secondary;
    }

    QProgressBar {
        background-color: $bg_secondary;
        border: 1px solid $hairline;
        border-radius: 4px;
        min-height: 8px;
        max-height: 14px;
        text-align: center;
        font-size: 10px;
        color: $text_secondary;
    }

    QProgressBar::chunk {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 $accent_deep, stop:1 $accent_primary);
        border-radius: 3px;
    }

    /* ---- Scrolling ----------------------------------------------------- */

    QScrollArea {
        background: transparent;
        border: none;
    }

    QScrollArea > QWidget > QWidget {
        background: transparent;
    }

    QScrollBar:vertical {
        background: transparent;
        width: 8px;
        margin: 0;
    }

    QScrollBar::handle:vertical {
        background-color: $border_strong;
        border-radius: 4px;
        min-height: 30px;
    }

    QScrollBar::handle:vertical:hover {
        background-color: $accent_primary;
    }

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
    }

    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
        background: transparent;
    }

    QScrollBar:horizontal {
        background: transparent;
        height: 8px;
    }

    QScrollBar::handle:horizontal {
        background-color: $border_strong;
        border-radius: 4px;
        min-width: 30px;
    }

    QScrollBar::handle:horizontal:hover {
        background-color: $accent_primary;
    }

    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
        width: 0;
    }

    /* ---- Groups, tabs and lists ---------------------------------------- */

    QGroupBox {
        background-color: $bg_card;
        border: 1px solid $hairline;
        border-radius: 10px;
        margin-top: 12px;
        padding: 16px;
        padding-top: 24px;
        font-weight: 600;
    }

    QGroupBox::title {
        subcontrol-origin: margin;
        left: 16px;
        padding: 0 8px;
        color: $accent_primary;
        font-size: 11px;
    }

    QTabWidget::pane {
        background-color: $bg_card;
        border: 1px solid $hairline;
        border-radius: 10px;
        top: -1px;
    }

    QTabBar::tab {
        background-color: transparent;
        color: $text_muted;
        border: none;
        border-bottom: 2px solid transparent;
        padding: 10px 18px;
        margin-right: 4px;
        font-weight: 600;
    }

    QTabBar::tab:selected {
        color: $accent_secondary;
        border-bottom-color: $accent_primary;
    }

    QTabBar::tab:hover:!selected {
        color: $text_primary;
        border-bottom-color: $border_strong;
    }

    QSpinBox, QDoubleSpinBox {
        background-color: $bg_secondary;
        color: $text_primary;
        border: 1px solid $border;
        border-radius: 6px;
        padding: 7px 10px;
    }

    QSpinBox:focus, QDoubleSpinBox:focus {
        border-color: $accent_primary;
    }

    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {
        background-color: $bg_tertiary;
        border: none;
        width: 20px;
    }

    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
        background-color: $accent_glow;
    }

    QListWidget {
        background-color: $bg_secondary;
        color: $text_primary;
        border: 1px solid $hairline;
        border-radius: 10px;
        padding: 6px;
        outline: none;
    }

    QListWidget::item {
        padding: 8px 12px;
        border-radius: 4px;
        border-left: 2px solid transparent;
        color: $text_secondary;
    }

    QListWidget::item:selected {
        background-color: $accent_glow;
        color: $accent_secondary;
        border-left-color: $accent_primary;
    }

    QListWidget::item:hover:!selected {
        background-color: $bg_hover;
        color: $text_primary;
    }

    QListWidget#nav {
        background-color: $bg_secondary;
        padding: 8px 6px;
    }

    QListWidget#nav::item {
        padding: 9px 12px;
        margin: 1px 0;
    }

    /* ---- Dialogs, tooltips and menus ----------------------------------- */

    QMessageBox {
        background-color: $bg_primary;
    }

    QMessageBox QLabel {
        color: $text_primary;
    }

    QToolTip {
        background-color: $bg_card;
        color: $text_primary;
        border: 1px solid $border_glow;
        border-radius: 4px;
        padding: 6px 10px;
    }

    QMenu {
        background-color: $bg_card;
        color: $text_primary;
        border: 1px solid $border_glow;
        border-radius: 8px;
        padding: 6px;
    }

    QMenu::item {
        padding: 7px 24px 7px 10px;
        border-radius: 4px;
        border-left: 2px solid transparent;
        color: $text_secondary;
    }

    QMenu::item:selected {
        background-color: $accent_glow;
        color: $accent_secondary;
        border-left-color: $accent_primary;
    }

    QMenu::item:disabled {
        color: $text_muted;
        background: transparent;
        border-left-color: transparent;
    }

    QMenu::icon {
        padding-left: 6px;
    }

    QMenu::indicator {
        width: 16px;
        height: 16px;
        padding-left: 4px;
    }

    QMenu::separator {
        height: 1px;
        background-color: $hairline;
        margin: 6px 10px;
    }

    QMenu::right-arrow {
        width: 10px;
        height: 10px;
    }

    /* ---- Status rows shared by the wizard and dialogs ------------------- */

    QWidget#status_row {
        background: transparent;
    }

    QWidget#status_row QLabel {
        font-size: 14px;
    }

    QWidget#status_row[compact="true"] QLabel {
        font-size: 12px;
    }

    /* ---- Setup wizard -------------------------------------------------- */

    QWizard {
        background: $bg_primary;
    }

    QWizard QPushButton {
        min-width: 80px;
        padding: 9px 16px;
    }

    QWizard QLabel#qt_watermark_label {
        background: transparent;
    }

    QWizard QFrame#card {
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 $panel_top, stop:1 $bg_secondary);
        border: 1px solid $hairline;
        border-radius: 12px;
        padding: 0;
    }

    QWizard QFrame#card[selected="true"] {
        border-color: $accent_primary;
        background: $bg_tertiary;
    }

    QWizard QLabel#title {
        color: $text_primary;
        font-size: 28px;
        font-weight: 700;
    }

    QWizard QLabel#subtitle {
        color: $text_secondary;
        font-size: 14px;
    }

    QWizard QLabel#setupBrand {
        color: $accent_primary;
        font-size: 11px;
        font-weight: 700;
    }

    QWizard QLabel#setupStage {
        color: $text_muted;
        font-size: 11px;
        padding: 6px 10px;
        border-bottom: 2px solid $border;
    }

    QWizard QLabel#setupStage[active="true"] {
        color: $text_primary;
        border-color: $accent_primary;
    }

    QWizard QLabel#section_title {
        color: $text_primary;
        font-size: 15px;
        font-weight: 600;
    }

    QWizard QLabel#panel_title {
        color: $accent_secondary;
        font-size: 16px;
        font-weight: 600;
    }

    QWizard QPushButton#setupNext {
        background: $accent_primary;
        color: $bg_primary;
        border: none;
        font-weight: 600;
    }

    QWizard QPushButton#setupNext:hover {
        background: $accent_secondary;
        color: $bg_primary;
    }

    QWizard QPushButton#setupNext:disabled {
        background: $bg_tertiary;
        color: $text_muted;
    }

    QWizard QComboBox QLineEdit {
        padding: 0;
        border: none;
        background: transparent;
    }

    QWizard QScrollArea {
        border: none;
        background: transparent;
    }

    QWizard QTextEdit#console {
        font-size: 10px;
    }

    QWizard QLabel#model_name {
        font-size: 11px;
        color: $text_primary;
    }

    QWizard QLabel#model_role {
        font-size: 15px;
        font-weight: 600;
        color: $accent_secondary;
    }

    QWizard QLabel#model_role_fast {
        font-size: 15px;
        font-weight: 600;
        color: $accent_indigo;
    }

    QWizard QLabel#model_size {
        font-size: 9px;
        color: $text_muted;
    }

    QWizard QLabel#model_info {
        background: $bg_tertiary;
        border: 1px solid $hairline;
        border-radius: 6px;
        padding: 6px 10px;
        color: $text_primary;
        font-size: 11px;
    }

    QWizard QLabel#tip {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 $accent_glow, stop:1 $indigo_glow);
        border: 1px solid $border_glow;
        border-radius: 8px;
        padding: 12px 16px;
        color: $accent_secondary;
        font-size: 13px;
    }

    QWizard QLabel#notice_error {
        font-size: 13px;
        padding: 12px 16px;
    }
""")


def build_theme_stylesheet(colours: dict[str, str]) -> str:
    """Render the shared stylesheet template with a palette."""
    return _THEME_TEMPLATE.substitute({"font_ui": FONT_UI, "font_mono": FONT_MONO, **colours})


HUD_THEME_STYLESHEET = build_theme_stylesheet(HUD_COLORS)


# The chat window's own look: a phone-like glass surface, SMS bubbles in the
# orb's colours and a composer with round send and stop buttons.
_CHAT_TEMPLATE = Template("""
    QMainWindow#chatWindow, QWidget#phoneShell { background: transparent; }
    QWidget#chatSurface {
        background: $bg_primary; border: 1px solid $border;
        border-radius: 28px;
    }
    QWidget#chatSurface QWidget { background: transparent; }
    QLabel { color: $text_primary; }
    QLabel#chatEyebrow { color: $text_muted; font-size: 10px; letter-spacing: 2px; }
    QLabel#chatName { color: $text_primary; font-size: 24px; font-weight: 600; }
    QLabel#chatCapsule {
        color: $accent_secondary; background: $bg_secondary;
        border: 1px solid $border_glow; border-radius: 12px;
        padding: 5px 12px; font-size: 10px; letter-spacing: 2px;
    }
    QPushButton#chatWindowControl {
        color: $text_secondary; background: transparent; border: none;
        border-radius: 18px; padding: 0; font-size: 19px; min-width: 36px;
    }
    QPushButton#chatWindowControl:hover { background: $bg_hover; color: $text_primary; }
    QPushButton#chatWindowControl:focus { border: 1px solid $accent_primary; }
    QLabel#chatEmptyTitle { font-size: 24px; font-weight: 600; color: $text_primary; }
    QLabel#chatEmptyDetail { font-size: 13px; color: $text_secondary; }
    QWidget#chatSurface QWidget#chatComposer {
        background: $bg_secondary; border: 1px solid $border_glow; border-radius: 22px;
    }
    QWidget#chatSurface QLabel#chatHomeIndicator { background: $border_strong; border-radius: 2px; }
    QWidget#chatSurface QScrollArea#chatTranscript { background-color: $bg_primary; border: none; }
    QWidget#chatSurface QPlainTextEdit#chatInput {
        background-color: $bg_secondary; color: $text_primary;
        border: none; border-radius: 14px; padding: 8px 4px;
        font-family: $font_ui; font-size: 14px;
    }
    QWidget#chatSurface QPlainTextEdit#chatInput:focus { border-color: $accent_primary; }
    QWidget#chatSurface QPushButton#chatSend, QWidget#chatSurface QPushButton#chatStop {
        border: none; border-radius: 19px; padding: 0;
    }
    QWidget#chatSurface QPushButton#chatSend { background-color: $accent_primary; }
    QWidget#chatSurface QPushButton#chatSend:hover { background-color: $accent_secondary; }
    QWidget#chatSurface QPushButton#chatSend:disabled { background-color: $accent_muted; }
    QWidget#chatSurface QPushButton#chatStop { background-color: $error; }
    QWidget#chatSurface QPushButton#chatStop:hover { background-color: $error_light; }
    QWidget#chatSurface QPushButton[chatRole="rewind"] {
        background-color: transparent; color: $text_muted; border: none;
        border-radius: 12px; font-size: 13px; padding: 2px;
    }
    QWidget#chatSurface QPushButton[chatRole="rewind"]:hover { background-color: $bg_hover; color: $accent_secondary; }
    QWidget#chatSurface QPushButton[chatRole="rewind"]:disabled { color: $border; }
    QWidget#chatSurface QLabel#chatStatus { color: $text_secondary; font-size: 12px; padding: 2px 4px; }
    QWidget#chatSurface QLabel#chatStatus[busy="true"] { color: $accent_indigo; }
    QWidget#chatSurface QLabel#chatPresence { color: $text_muted; font-size: 12px; }
    QWidget#chatSurface QLabel#chatPresence[presence="online"] { color: $accent_primary; }
    QWidget#chatSurface QLabel#chatPresence[presence="typing"] { color: $accent_indigo; }
    QWidget#chatSurface QLabel#bubble[kind="user"] {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 $accent_primary, stop:1 $accent_deep);
        color: $bg_primary; border-radius: 18px; border-bottom-right-radius: 5px;
        padding: 12px 16px; font-size: 14px;
    }
    QWidget#chatSurface QLabel#bubble[kind="assistant"] {
        background-color: $bg_secondary; color: $text_primary;
        border: 1px solid $border_glow; border-radius: 18px; border-bottom-left-radius: 5px;
        padding: 12px 16px; font-size: 14px;
    }
    QWidget#chatSurface QLabel#chatTime { color: $text_muted; font-size: 11px; }
    QWidget#chatSurface QLabel#chatNotice { color: $text_muted; font-size: 12px; }
    QScrollBar:vertical { background: transparent; width: 5px; margin: 0; }
    QScrollBar::handle:vertical { background: $border_strong; border-radius: 2px; min-height: 28px; }
    QScrollBar::handle:vertical:hover { background: $accent_primary; }
""")

CHAT_THEME_STYLESHEET = _CHAT_TEMPLATE.substitute(_TOKENS)


# Small surfaces that wear the HUD outside the shared stylesheet's windows:
# the mode badge over the orb and the splash screen's status line.
MODE_BADGE_STYLESHEET = Template(
    "QLabel { background-color: $bg_primary; color: $accent_secondary;"
    " border: 1px solid $border_glow; border-radius: 9px;"
    " padding: 2px 9px; font-size: 11px; font-weight: 600; }"
).substitute(_TOKENS)

FACE_STYLESHEET = Template("background-color: $bg_primary;").substitute(_TOKENS)

SPLASH_STATUS_STYLESHEET = Template(
    "QLabel { color: $text_secondary; background: transparent; }"
).substitute(_TOKENS)


# ---- Line icons ------------------------------------------------------------
#
# 24-unit line drawings, stroked in a palette colour with round caps. Shared by
# the Qt windows (``line_icon``) and the memory viewer page (inline SVG).
LINE_ICONS: dict[str, str] = {
    "logs": '<rect x="5" y="3.5" width="14" height="17" rx="2"/><path d="M8.5 8.5h7M8.5 12h7M8.5 15.5h4"/>',
    "memory": ('<circle cx="6" cy="7" r="2.2"/><circle cx="18" cy="7" r="2.2"/><circle cx="12" cy="17.5" r="2.2"/>'
               '<path d="M8.2 7h7.6M7.2 8.9l3.7 6.7M16.8 8.9l-3.7 6.7"/>'),
    "microphone": ('<rect x="9" y="3.5" width="6" height="11" rx="3"/>'
                   '<path d="M6 11.5a6 6 0 0 0 12 0M12 17.5v3M9 20.5h6"/>'),
    "chat": '<path d="M5 5.5h14a1.5 1.5 0 0 1 1.5 1.5v8.5a1.5 1.5 0 0 1-1.5 1.5h-8l-4.5 3.5v-3.5H5a1.5 1.5 0 0 1-1.5-1.5V7A1.5 1.5 0 0 1 5 5.5z"/>',
    "phone": '<rect x="7" y="2.5" width="10" height="19" rx="2"/><path d="M11 18h2"/>',
    "orb": '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4"/><path d="M3.5 12h3M17.5 12h3"/>',
    "switch": '<path d="M4 8h13.5M14 4.5L17.5 8 14 11.5M20 16H6.5M10 12.5L6.5 16l3.5 3.5"/>',
    "pause": '<path d="M9 6v12M15 6v12"/>',
    "trash": ('<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 10.5v6M14 10.5v6"/>'),
    "sliders": ('<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/>'
                '<circle cx="9" cy="17" r="2"/>'),
    "gear": ('<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21'
             'M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/>'
             '<circle cx="12" cy="12" r="6.5"/>'),
    "pulse": '<path d="M3 12h4l2.5-6 4.5 12 2.5-6H21"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4v4h-4"/>',
    "chip": ('<rect x="7" y="7" width="10" height="10" rx="1.5"/><rect x="10" y="10" width="4" height="4"/>'
             '<path d="M10 4v3M14 4v3M10 17v3M14 17v3M4 10h3M4 14h3M17 10h3M17 14h3"/>'),
    "folder": '<path d="M3.5 7a1.5 1.5 0 0 1 1.5-1.5h4l2 2h8a1.5 1.5 0 0 1 1.5 1.5v8.5a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5z"/>',
    "database": ('<ellipse cx="12" cy="6" rx="7" ry="2.5"/><path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6'
                 'M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>'),
    "play": '<path d="M8 5.5l11 6.5-11 6.5z"/>',
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/>',
    "power": '<path d="M12 3.5v8M7.2 6.3a7.5 7.5 0 1 0 9.6 0"/>',
    "home": '<path d="M4 11l8-6.5 8 6.5M6 9.5V19.5h12V9.5M10 19.5v-5h4v5"/>',
    "cloud": '<path d="M7.5 18.5h9.5a4 4 0 0 0 .5-8 5.5 5.5 0 0 0-10.6 1.6A3.3 3.3 0 0 0 7.5 18.5z"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "fit": '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/>',
    "spark": '<path d="M12 3.5l2 6.5 6.5 2-6.5 2-2 6.5-2-6.5-6.5-2 6.5-2z"/>',
    "import": '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 19.5h14"/>',
    "broom": '<path d="M14.5 3.5l-4 9M7 12.5h8l2.5 8h-13z M9.5 16.5l-.8 4M13.5 16.5l.3 4"/>',
    "calendar": ('<rect x="4" y="5.5" width="16" height="14.5" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>'),
    "tag": '<path d="M3.5 12.5V4.5a1 1 0 0 1 1-1h8l8 8-9 9z"/><circle cx="8" cy="8" r="1.5"/>',
    "tree": '<path d="M6 4v16M6 8h6M6 15h6"/><rect x="12" y="5.5" width="7" height="5" rx="1"/><rect x="12" y="12.5" width="7" height="5" rx="1"/>',
    "meal": '<path d="M7 3.5v7M5 3.5v4a2 2 0 0 0 4 0v-4M7 10.5v10M16.5 3.5c-2 1.5-2.5 4-2.5 7h3v10"/>',
    "diary": '<path d="M5 4.5h11a2 2 0 0 1 2 2v13H7a2 2 0 0 1-2-2z"/><path d="M5 17.5a2 2 0 0 1 2-2h11M9 8.5h5"/>',
    "edit": '<path d="M15.5 4.5l4 4L9 19H5v-4z"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "cross": '<path d="M6 6l12 12M18 6L6 18"/>',
    "alert": '<path d="M12 4l9 16H3z"/><path d="M12 10v4.5M12 17.2v.3"/>',
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8v.3"/>',
    "document": '<path d="M6 3.5h8l4 4v13H6z"/><path d="M14 3.5v4h4"/>',
    "moon": '<path d="M19 14.5A7.5 7.5 0 1 1 9.5 5a6 6 0 0 0 9.5 9.5z"/>',
    "shield": '<path d="M12 3.5l7 3v5.5c0 4.2-3 7.4-7 8.5-4-1.1-7-4.3-7-8.5V6.5z"/>',
    "chevron": '<path d="M9.5 6l6 6-6 6"/>',
    "arrow_up": '<path d="M12 19V5.5M6.5 11L12 5.5l5.5 5.5"/>',
    "arrow_down": '<path d="M12 5v13.5M6.5 13l5.5 5.5 5.5-5.5"/>',
    "rewind": '<path d="M5.5 12a6.5 6.5 0 1 0 2-4.7M5 4v4h4"/>',
    "dot": '<circle cx="12" cy="12" r="4.5" fill="currentColor" stroke="none"/>',
}

# Colours an icon takes in each state of the control drawing it.
_ICON_MODE_COLOURS = {"normal": "text_secondary", "active": "accent_secondary", "disabled": "text_muted"}


def line_icon_svg(name: str, colour: str, size: int = 24, stroke: float = 1.6) -> str:
    """One line icon as a standalone SVG document stroked in ``colour``."""
    body = LINE_ICONS[name].replace("currentColor", colour)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
            f'fill="none" stroke="{colour}" stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


def _icon_pixmap(name: str, colour: str, size: int):
    from PyQt6.QtGui import QPixmap
    pixmap = QPixmap()
    pixmap.loadFromData(line_icon_svg(name, colour, size).encode("utf-8"), "SVG")
    return pixmap


def line_icon(name: str, colour_key: Optional[str] = None):
    """A ``QIcon`` of a line icon: secondary text colour, light cyan when active, muted when disabled.

    ``colour_key`` paints every state in one palette colour instead (a status dot, for example).
    """
    from PyQt6.QtGui import QIcon
    icon = QIcon()
    modes = {"normal": QIcon.Mode.Normal, "active": QIcon.Mode.Active,
             "disabled": QIcon.Mode.Disabled, "selected": QIcon.Mode.Selected}
    for state, mode in modes.items():
        key = colour_key or _ICON_MODE_COLOURS.get(state, "accent_secondary")
        for size in (16, 32, 48):
            icon.addPixmap(_icon_pixmap(name, HUD_COLORS[key], size), mode)
    return icon


def icon_file(name: str, colour_key: str = "accent_secondary") -> str:
    """Path of a line icon written once per process, for rich text and stylesheets."""
    directory = _icons_directory()
    path = os.path.join(directory, f"line_{name}_{colour_key}.svg")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(line_icon_svg(name, HUD_COLORS[colour_key]))
    return path.replace("\\", "/")


# ---- Roles -----------------------------------------------------------------

def set_role(widget, role: str) -> None:
    """Give ``widget`` a stylesheet role (its object name) and restyle it at once."""
    if widget.objectName() == role:
        return
    widget.setObjectName(role)
    _repolish(widget)


def set_state(widget, name: str, value) -> None:
    """Set a dynamic property the stylesheet selects on (``[selected="true"]``) and restyle at once."""
    value = ("true" if value else "false") if isinstance(value, bool) else value
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    _repolish(widget)


def _repolish(widget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def link(url: str, text: str) -> str:
    """A rich-text link in the accent colour, for labels that open links."""
    from html import escape
    return (f"<a href='{escape(url, quote=True)}' style='color: {HUD_COLORS['accent_primary']};'>"
            f"{escape(text)}</a>")


def eyebrow(text: str, parent=None):
    """A small upper-case, wide-tracked cyan caption (``JARVIS / SETTINGS``)."""
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QLabel
    label = QLabel(text, parent)
    label.setObjectName("eyebrow")
    font = label.font()
    font.setCapitalization(QFont.Capitalization.AllUppercase)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.5)
    label.setFont(font)
    return label


def divider(parent=None):
    """A 1 px cyan hairline between a header and the content under it."""
    from PyQt6.QtWidgets import QFrame
    line = QFrame(parent)
    line.setObjectName("divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


def hud_heading(section: str, title: str, subtitle: str = ""):
    """The HUD header of a window: ``JARVIS / <SECTION>`` eyebrow, title and optional muted subtitle.

    Returns ``(widget, title_label, subtitle_label)``; the subtitle label is hidden while empty.
    """
    from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(eyebrow(f"Jarvis  /  {section}"))
    title_label = QLabel(title)
    title_label.setObjectName("title")
    layout.addWidget(title_label)
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("subtitle")
    subtitle_label.setWordWrap(True)
    subtitle_label.setVisible(bool(subtitle))
    layout.addWidget(subtitle_label)
    return widget, title_label, subtitle_label


# ---- Indicator icons in the stylesheet --------------------------------------

_CHECKMARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
    f'<path d="M4 9l3.5 3.5L14 5" stroke="{HUD_COLORS["bg_primary"]}" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
)

_RADIO_DOT_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
    f'<circle cx="9" cy="9" r="4" fill="{HUD_COLORS["bg_primary"]}"/></svg>'
)

_MENU_CHECK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 18">'
    f'<path d="M4 9.5l3.2 3.2L14 5.8" stroke="{HUD_COLORS["accent_primary"]}" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
)

_ARROW_UP_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">'
    f'<path d="M2.5 7.5L6 4l3.5 3.5" stroke="{HUD_COLORS["text_muted"]}" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
)

_ARROW_DOWN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">'
    f'<path d="M2.5 4.5L6 8l3.5-3.5" stroke="{HUD_COLORS["text_muted"]}" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
)

_ARROW_RIGHT_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">'
    f'<path d="M4.5 2.5L8 6l-3.5 3.5" stroke="{HUD_COLORS["accent_primary"]}" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
)

# Cached icon directory (created once per process)
_icon_dir: str | None = None


def _icons_directory() -> str:
    global _icon_dir
    if _icon_dir is None:
        _icon_dir = tempfile.mkdtemp(prefix="jarvis_theme_")
    return _icon_dir


_ICON_STYLESHEET_TEMPLATE = """
    QCheckBox::indicator:checked {{
        image: url({check});
    }}
    QRadioButton::indicator:checked {{
        image: url({radio});
    }}
    QMenu::indicator:checked {{
        image: url({menu_check});
    }}
    QMenu::right-arrow {{
        image: url({arrow_right});
    }}
    QComboBox::down-arrow {{
        image: url({arrow_down});
        width: 10px;
        height: 10px;
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: url({arrow_up});
        width: 10px;
        height: 10px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: url({arrow_down});
        width: 10px;
        height: 10px;
    }}
"""


def _ensure_icons() -> dict[str, str]:
    """Write indicator SVGs to a temp directory, return {name: path} mapping."""
    directory = _icons_directory()
    icons = {
        "check": _CHECKMARK_SVG,
        "radio": _RADIO_DOT_SVG,
        "menu_check": _MENU_CHECK_SVG,
        "arrow_up": _ARROW_UP_SVG,
        "arrow_down": _ARROW_DOWN_SVG,
        "arrow_right": _ARROW_RIGHT_SVG,
    }
    paths: dict[str, str] = {}
    for name, svg in icons.items():
        path = os.path.join(directory, f"{name}.svg")
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(svg)
        paths[name] = path.replace("\\", "/")
    return paths


def themed_stylesheet(extra: str = "") -> str:
    """The shared stylesheet with its SVG indicator icons, followed by ``extra`` rules."""
    icon_css = _ICON_STYLESHEET_TEMPLATE.format(**_ensure_icons())
    return HUD_THEME_STYLESHEET + icon_css + extra


def apply_theme(widget, extra: str = "") -> None:
    """Apply the Jarvis theme to a Qt widget, including SVG-based indicator icons."""
    widget.setStyleSheet(themed_stylesheet(extra))


def apply_application_theme(app) -> None:
    """Theme the whole application, so dialogs and menus created without a themed parent match too."""
    app.setStyleSheet(themed_stylesheet())
