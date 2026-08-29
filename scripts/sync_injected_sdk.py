#!/usr/bin/env python3
"""Print canonical Maeyr.py for monorepo copy-check."""

from maeyr.runtime.inject import to_module_source

if __name__ == "__main__":
    print(to_module_source(), end="")
