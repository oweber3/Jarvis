"""
Tests for splash_screen.py functionality.

Tests the splash screen component used during application startup.
Note: These tests use headless mode where possible.
"""

import pytest
from unittest.mock import patch, MagicMock
import sys


class TestSplashScreenImport:
    """Tests for splash screen module import."""

    def test_can_import_module(self):
        """splash_screen module should be importable."""
        from desktop_app import splash_screen
        assert splash_screen is not None

    def test_splash_screen_class_exists(self):
        """SplashScreen class should be defined."""
        from desktop_app.splash_screen import SplashScreen
        assert SplashScreen is not None

    def test_splash_has_no_orb_of_its_own(self):
        """The splash reuses the live orb widget instead of painting its own."""
        from desktop_app import splash_screen
        assert not hasattr(splash_screen, "AnimatedOrb")


class TestSplashScreenFunctionality:
    """Tests for splash screen functionality."""

    def test_splash_screen_instantiation(self, qapp):
        """SplashScreen should instantiate without error."""
        from desktop_app.splash_screen import SplashScreen
        splash = SplashScreen()
        assert splash is not None
        splash.close()

    def test_splash_screen_set_status(self, qapp):
        """SplashScreen should allow setting status text."""
        from desktop_app.splash_screen import SplashScreen
        splash = SplashScreen()
        splash.set_status("Test status message")
        assert splash._status_label.text() == "Test status message"
        splash.close()

    def test_splash_screen_close_splash(self, qapp):
        """SplashScreen close_splash should stop animation and close."""
        from desktop_app.splash_screen import SplashScreen
        splash = SplashScreen()
        splash.show()
        assert splash._orb.is_animating()
        splash.close_splash()
        assert not splash._orb.is_animating()

    def test_splash_shows_the_live_orb_in_its_thinking_look(self, qapp):
        """The splash hosts the same OrbWidget as the face window."""
        from desktop_app.orb_widget import OrbState, OrbWidget
        from desktop_app.splash_screen import SplashScreen
        splash = SplashScreen()
        assert isinstance(splash._orb, OrbWidget)
        splash._orb.tick(0.1)
        assert splash._orb.orb_state is OrbState.THINKING
        splash.close()


class TestSplashScreenColors:
    """Tests for splash screen theme colors."""

    def test_panel_is_the_orb_backdrop_with_a_palette_border(self, qapp):
        """The rounded panel is painted from the shared palette."""
        from desktop_app.splash_screen import SplashScreen
        from desktop_app.themes import HUD_COLORS
        splash = SplashScreen()
        splash.show()
        image = splash.grab().toImage()
        # A pixel in the panel margin, outside the orb widget.
        pixel = image.pixelColor(image.width() // 2, 6)
        assert pixel.name() == HUD_COLORS["bg_primary"]
        splash.close()
