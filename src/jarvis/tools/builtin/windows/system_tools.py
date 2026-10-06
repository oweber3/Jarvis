"""Audio, media, system-information, settings and input tools, grouped for registration."""

from .input_control import InputControlTool
from .media_control import MediaControlTool
from .system_info import SystemInfoTool
from .system_settings import SystemSettingsTool
from .system_volume import SystemVolumeTool

SYSTEM_TOOL_CLASSES = (SystemVolumeTool, MediaControlTool, SystemInfoTool, SystemSettingsTool, InputControlTool)
