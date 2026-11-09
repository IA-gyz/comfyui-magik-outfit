"""Magik Outfit: pick an outfit visually on a mannequin, output an outfit STRING.

The node itself is small: a hidden JSON `state` widget (written by the JS picker)
plus a few text-assembly widgets. All logic lives in outfit_core.py.
"""
import os

from comfy_api.latest import ComfyExtension, io

from .outfit_core import FORMATS, Catalog, build_string, parse_state, random_state_text, resolve
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


def _truthy(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _find_workflow_node(workflow, unique_id):
    if not isinstance(workflow, dict):
        return None
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, dict) and str(node.get("id")) == str(unique_id):
            return node
    return None


def _register_prompt_handler():
    """Queue-time randomize: rewrite the hidden state of Magik.Outfit nodes with randomize=on.

    The drawn selection is also written into the saved workflow's node properties and the
    PNG metadata (extra_pnginfo), and pushed to the frontend so the picker shows it.
    """
    try:
        from server import PromptServer
    except Exception:
        return  # not running inside ComfyUI (tests, tools)
    instance = getattr(PromptServer, "instance", None)
    if instance is None or getattr(instance, "_magik_outfit_resolver_installed", False):
        return
    instance._magik_outfit_resolver_installed = True

    def resolve_randomized(json_data):
        try:
            prompt = json_data.get("prompt")
            if not isinstance(prompt, dict):
                return json_data
            extra_data = json_data.setdefault("extra_data", {})
            if not isinstance(extra_data, dict):
                return json_data
            extra_pnginfo = extra_data.setdefault("extra_pnginfo", {})
            if not isinstance(extra_pnginfo, dict):
                return json_data
            workflow = extra_pnginfo.get("workflow")
            updates = {}
            for node_id, node in list(prompt.items()):
                if not isinstance(node, dict) or node.get("class_type") != "Magik.Outfit":
                    continue
                inputs = node.get("inputs")
                if not isinstance(inputs, dict) or not _truthy(inputs.get("randomize")):
                    continue
                source = inputs.get("random_source")
                source = source if source in ("catalog", "attire", "any") else "catalog"
                try:
                    seed = int(inputs.get("seed"))
                except (TypeError, ValueError):
                    seed = -1
                state_text = random_state_text(CATALOG, source, seed)
                if state_text is None:
                    continue
                inputs["state"] = state_text
                outfit_id, overrides = parse_state(state_text)
                snap = CATALOG.get()
                slot_map = resolve(snap.outfits.get(outfit_id) if outfit_id else None,
                                   overrides, snap.pieces)
                records = extra_pnginfo.setdefault("magik_outfit", {})
                records[str(node_id)] = {"source": source, "seed": seed,
                                         "state": state_text, "resolved": slot_map}
                wf_node = _find_workflow_node(workflow, node_id)
                if wf_node is not None:
                    props = wf_node.setdefault("properties", {})
                    if not isinstance(props, dict):
                        props = {}
                        wf_node["properties"] = props
                    props["magik_outfit_selection"] = {"source": source, "seed": seed, "slot_map": slot_map}
                    wv = wf_node.get("widgets_values")
                    if isinstance(wv, list) and wv:
                        wv[0] = state_text  # state is the first widget
                updates[str(node_id)] = {"state": state_text}
            if updates:
                instance.send_sync("magik.outfit_resolved", {"nodes": updates}, json_data.get("client_id"))
        except Exception as e:
            print(f"[Magik Outfit] randomize-on-run failed: {e}")
        return json_data

    instance.add_on_prompt_handler(resolve_randomized)


_register_prompt_handler()


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
                    tooltip="What random draws come from: catalog outfits, the bundled danbooru attire "
                            "tag pools (data/attire.json), or either."),
                io.Boolean.Input(
                    "randomize", default=False, optional=True,
                    tooltip="At queue time, draw a random selection (from random_source) instead of "
                            "using the picker state. The draw is written back to the node and recorded "
                            "in the PNG metadata."),
                io.Int.Input(
                    "seed", default=-1, min=-1, max=2147483647, optional=True,
                    tooltip="Seed for randomize: -1 draws fresh every run; a fixed seed is reproducible."),
            ],
            outputs=[io.String.Output(id="outfit", display_name="outfit")],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        # Re-run when the catalog files change (edited tags, saved outfits), not only when inputs do.
        keys = ("state", "format", "separator", "prefix", "random_source", "randomize", "seed")
        return repr((CATALOG.stamp(), {k: kwargs.get(k) for k in keys}))

    @classmethod
    def execute(cls, state, format, separator, prefix, random_source="catalog",
                randomize=False, seed=-1):
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
