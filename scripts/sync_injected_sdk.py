#!/usr/bin/env python3
"""Print canonical ViksaAI.py for monorepo copy-check."""

from viksa_ai.runtime.inject import to_module_source

if __name__ == "__main__":
    print(to_module_source(), end="")
