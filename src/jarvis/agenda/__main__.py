"""``python -m jarvis.agenda``: add, list, test and remove calendars (see ``cli.py``)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
