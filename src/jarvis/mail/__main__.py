"""``python -m jarvis.mail``: add, list, test, sign in and remove email accounts (see ``cli.py``)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
