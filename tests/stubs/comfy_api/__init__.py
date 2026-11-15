"""Minimal stub of ComfyUI's comfy_api package.

Lets tests/test_outfit.py import the node (magik_outfit.py) and run the NodeTests
in CI, where no ComfyUI checkout is on sys.path. Only the surface magik_outfit.py
actually uses is provided. When the real comfy_api is importable (inside ComfyUI)
the test module prefers it and never looks at this stub.
"""
