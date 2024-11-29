#!/usr/bin/env python3

import os.path
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from preflop_advisor.__main__ import main

if __name__ == "__main__":
    main()
