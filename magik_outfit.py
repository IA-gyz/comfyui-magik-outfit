"""Magik Outfit: pick an outfit visually on a mannequin, output an outfit STRING.

The node itself is small: a hidden JSON `state` widget (written by the JS picker)
plus a few text-assembly widgets. All logic lives in outfit_core.py.
"""
import os

from comfy_api.latest import ComfyExtension, io

from .outfit_core import FORMATS, Catalog, build_string, parse_state, resolve
from .outfit_routes import add_routes

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE = '{"v":1,"outfit":null,"overrides":{}}'


def _user_dir():
    """<ComfyUI user dir>/magik_outfit, or None if folder_paths is unavailable."""
    try:
        import folder_paths
        return os.path.join(folder_paths.get_user_directory(), "magik_outfit")
    except Exception:
        return None


CATALOG = Catalog(HERE, _user_dir())


def _register_routes():
    try:
        from server import PromptServer
    except Exception:
        return  # not running inside ComfyUI (tests, tools)
    instance = getattr(PromptServer, "instance", None)
    if instance is not None:
        add_routes(instance.routes, CATALOG)


_register_routes()


class MagikOutfit(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="Magik.Outfit",
            display_name="Magik Outfit",
            category="Magik/Prompt",
            description="Pick a whole outfit on a neutral mannequin, then swap any piece. "
                        "Outputs the outfit as a text description.",
            search_aliases=["outfit", "clothes", "clothing", "wardrobe", "mannequin"],
            inputs=[
                io.String.Input(
                    "state", default=DEFAULT_STATE, socketless=True,
                    tooltip="Selection state, written by the outfit picker. Not meant to be edited by hand."),
                io.Combo.Input(
                    "format", options=list(FORMATS), default="tags",
                    tooltip="tags: booru-style tags. natural: plain phrases. "
                            "both: phrases, then a new line with the tags."),
                io.String.Input(
                    "separator", default=", ",
                    tooltip="Joins the pieces. \\n and \\t are converted to a new line / tab."),
                io.String.Input(
                    "prefix", default="",
                    tooltip="Optional text placed before the outfit, e.g. 'wearing'."),
                io.Combo.Input(
                    "random_source", options=["catalog", "attire", "any"], default="catalog",
                    optional=True,
                    tooltip="What the picker's Random button draws from: catalog outfits, the bundled "
                            "danbooru attire tag pools (data/attire.json), or either."),
            ],
            outputs=[io.String.Output(id="outfit", display_name="outfit")],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        # Re-run when the catalog files change (edited tags, saved outfits), not only when inputs do.
        return repr(CATALOG.stamp())

    @classmethod
    def execute(cls, state, format, separator, prefix, random_source="catalog"):
        snap = CATALOG.get()
        outfit_id, overrides = parse_state(state)
        outfit = snap.outfits.get(outfit_id) if outfit_id else None
        slot_map = resolve(outfit, overrides, snap.pieces)
        modified = outfit is None or slot_map != outfit["pieces"]
        text = build_string(slot_map, outfit, modified, snap.mannequin, snap.pieces,
                            format, separator, prefix)
        return io.NodeOutput(text)


class MagikOutfitExtension(ComfyExtension):
    async def get_node_list(self):
        return [MagikOutfit]


async def comfy_entrypoint():
    return MagikOutfitExtension()
