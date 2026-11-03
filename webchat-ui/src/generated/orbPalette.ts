// Generated from desktop_app/themes.py and desktop_app/orb_widget.py by `python -m desktop_app.web_chat_theme`. Do not edit.
export const ORB_PALETTE = {
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
  "red_light": "#f87171"
} as const

export const ORB_LOOKS = {
  "offline": {
    "glow": 0.08,
    "spin": 0.05,
    "think": 0.0,
    "voiceGain": 0.0,
    "stir": 0.01,
    "colour": "#334155",
    "accent": "#475569",
    "label": "offline",
    "swell": 1.0
  },
  "idle": {
    "glow": 0.3,
    "spin": 0.25,
    "think": 0.0,
    "voiceGain": 0.0,
    "stir": 0.025,
    "colour": "#22d3ee",
    "accent": "#38bdf8",
    "label": "system online",
    "swell": 1.0
  },
  "listening": {
    "glow": 0.7,
    "spin": 0.4,
    "think": 0.0,
    "voiceGain": 1.0,
    "stir": 0.045,
    "colour": "#22d3ee",
    "accent": "#60a5fa",
    "label": "listening",
    "swell": 1.0
  },
  "thinking": {
    "glow": 0.6,
    "spin": 0.9,
    "think": 1.0,
    "voiceGain": 0.0,
    "stir": 0.03,
    "colour": "#818cf8",
    "accent": "#38bdf8",
    "label": "thinking",
    "swell": 1.0
  },
  "speaking": {
    "glow": 0.92,
    "spin": 0.45,
    "think": 0.0,
    "voiceGain": 1.0,
    "stir": 0.03,
    "colour": "#67e8f9",
    "accent": "#3b82f6",
    "label": "speaking",
    "swell": 1.45
  },
  "dictating": {
    "glow": 0.75,
    "spin": 0.35,
    "think": 0.0,
    "voiceGain": 1.0,
    "stir": 0.045,
    "colour": "#4ade80",
    "accent": "#22c55e",
    "label": "dictating",
    "swell": 1.0
  },
  "muted": {
    "glow": 0.14,
    "spin": 0.1,
    "think": 0.0,
    "voiceGain": 0.0,
    "stir": 0.01,
    "colour": "#64748b",
    "accent": "#94a3b8",
    "label": "muted",
    "swell": 1.0
  },
  "error": {
    "glow": 0.45,
    "spin": 0.15,
    "think": 0.0,
    "voiceGain": 0.0,
    "stir": 0.02,
    "colour": "#ef4444",
    "accent": "#f87171",
    "label": "error",
    "swell": 1.0
  }
} as const
