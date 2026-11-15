"""Minimal torch stub for CI: magik_outfit._image_tensor only needs from_numpy.

tests/stubs is appended to sys.path only when the real comfy_api is missing, and
appended (not prepended) so a real installed torch always wins over this stub.
"""


def from_numpy(array):
    return array
