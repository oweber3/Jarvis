"""Node.js for the wizard's MCP servers: found without a restart, installed with winget only on request.

These tests never run winget or any installer: they only inspect the command that would run.
"""
import os
import sys

import pytest

from desktop_app import node_setup


@pytest.fixture
def no_node(monkeypatch, tmp_path):
    """A PC where npx is not on this process's PATH, the system PATH or the default install folder."""
    def missing(command):
        raise FileNotFoundError(command)

    monkeypatch.setattr("jarvis.tools.external.mcp_client._resolve_command", missing)
    monkeypatch.setattr(node_setup, "_system_path_entries", lambda: [])
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(node_setup.sys, "platform", "win32")
    return tmp_path


def _node_folder(path):
    path.mkdir(parents=True)
    (path / "npx.cmd").write_text("@echo off\n")
    return path


class TestNodeAvailable:
    def test_node_on_this_process_path(self, monkeypatch):
        monkeypatch.setattr("jarvis.tools.external.mcp_client._resolve_command", lambda c: "C:/nodejs/npx.cmd")
        assert node_setup.node_available() is True

    def test_missing_node(self, no_node):
        assert node_setup.node_available() is False

    def test_node_installed_after_start_is_found_on_the_system_path(self, no_node, monkeypatch):
        folder = _node_folder(no_node / "fresh" / "nodejs")
        monkeypatch.setattr(node_setup, "_system_path_entries", lambda: [str(no_node / "other"), str(folder)])
        assert node_setup.node_available() is True
        # MCP servers started later by this process (and the daemon it spawns) find Node too.
        assert str(folder) in os.environ["PATH"].split(os.pathsep)

    def test_node_in_the_default_install_folder_is_found(self, no_node):
        folder = _node_folder(no_node / "Program Files" / "nodejs")
        assert node_setup.node_available() is True
        assert str(folder) in os.environ["PATH"].split(os.pathsep)

    def test_other_platforms_only_use_the_existing_lookup(self, no_node, monkeypatch):
        _node_folder(no_node / "Program Files" / "nodejs")
        monkeypatch.setattr(node_setup.sys, "platform", "darwin")
        assert node_setup.node_available() is False


class TestInstallCommand:
    def test_installs_the_official_lts_package_with_winget(self, monkeypatch):
        monkeypatch.setattr(node_setup, "winget_path", lambda: r"C:\winget.exe")
        command = node_setup.install_command()
        assert command[0] == r"C:\winget.exe" and command[1] == "install"
        assert "OpenJS.NodeJS.LTS" in command
        # Hidden console: winget must never stop to ask a question nobody can see.
        assert "--disable-interactivity" in command

    def test_no_command_without_winget(self, monkeypatch):
        monkeypatch.setattr(node_setup, "winget_path", lambda: None)
        assert node_setup.install_command() is None

    def test_winget_is_windows_only(self, monkeypatch):
        monkeypatch.setattr(node_setup.sys, "platform", "darwin")
        assert node_setup.winget_path() is None

    def test_download_link_is_the_official_site(self):
        assert node_setup.NODE_DOWNLOAD_URL.startswith("https://nodejs.org/")
