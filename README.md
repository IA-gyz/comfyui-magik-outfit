# Magik Outfit

Pick a whole outfit on a neutral mannequin, swap any piece, and output the result as a STRING.

Node: **Magik Outfit** (`Magik.Outfit`, category `Magik/Prompt`). Output: `outfit` (STRING).
Widgets: `format` (tags / natural / both), `separator` (`\n` and `\t` are converted), `prefix`,
`random_source` (what the picker's Random button draws from: `catalog`, `attire` or `any`).
The picker replaces the hidden `state` widget (`{"v":1,"outfit":…,"overrides":{…}}`; an override value may also be `{"tags": "..."}`, a tag-only slot with no art).

## Install
Copy this folder into `ComfyUI/custom_nodes/` and restart. Requires a ComfyUI with the V3 node API.
Saved outfits go to `<ComfyUI user dir>/magik_outfit/`; extra pieces can be added there by hand
(`pieces.json`, same schema as `data/pieces.json`, layers under `assets/`).

## What is protected
- A malformed entry in your hand-edited `pieces.json` / `outfits.json` is skipped and listed in the node's status
  line instead of breaking the catalog. A file that is not valid JSON is reported, never silently ignored, and
  saving or deleting an outfit **refuses** to rewrite it (you would lose what you typed): fix or remove it first.
- `tools/make_placeholders.py` regenerates the placeholder art and **overwrites the catalog**. It records a
  fingerprint and refuses to run again once anything was edited or replaced (including by `art.py import`);
  `--force` overrides.
- `art.py import` is transactional: if baking or validation fails, every file it touched is restored.
  `--prune` is ignored with `--only` (and says so).
- The hidden state is untrusted (workflow files get shared): it is parsed defensively and cannot touch prototypes.
- The Reload button (↻) re-fetches the catalog *and* every cached layer image, so new art shows up without a page refresh.

## Layout
- `outfit_core.py` catalog, selection (`resolve`), string assembly, compositing (no ComfyUI imports)
- `outfit_routes.py` aiohttp routes; `magik_outfit.py` the V3 node
- `web/outfit_logic.js` JS mirror of the Python logic; `web/outfit_ui.js` the picker; `web/magik_outfit.js` wiring
- `data/` catalog JSON (`mannequin.json`, `pieces.json`, `outfits.json`, `attire.json`, `presets.json`);
  `assets/` mannequin, piece layers and baked tiles (512x1024 WebP with alpha)

## Random sources
The Random button draws from `random_source`:
- `catalog` (default): a random outfit tile, respecting the search/style/favorites filters.
- `attire`: one outfit drawn from the bundled Danbooru attire tag pools (`data/attire.json`,
  sections of the Danbooru "attire" tag-group wiki; data and generator from
  [Comfyui-Anima-Tools](https://github.com/nregret/Comfyui-Anima-Tools), MIT, (c) nregret).
  Exactly one main garment is drawn (a top+bottom pair, a uniform or a traditional outfit),
  then decoration always and legwear/footwear with 60% probability each. A drawn tag that
  appears in a catalog piece's `tags` selects that piece (so it shows in the preview); any
  other slot becomes a tag-only entry `{"tags": "..."}`, shown in accent italics on the chip -
  it goes into the output string but cannot be baked into a saved outfit tile.

## Presets browser
`data/presets.json` holds 430 curated clothing presets (bilingual names, tags, categories,
traits, preview image) from [Comfyui-Anima-Tools](https://github.com/nregret/Comfyui-Anima-Tools)
(MIT, (c) nregret). The picker's **Presets** tab browses them with search (name/tags, EN+ZH),
category chips, sorting and paging; preview images are referenced from the author's CDN
(jsDelivr `Dressing-doll` repo) rather than bundled. Clicking a preset maps its tags onto
catalog pieces where possible (`preset_to_slot_map`) and tag-only slots otherwise.
- `tools/` `make_placeholders.py`, `bake_tiles.py`, `validate_catalog.py`, and the art pipeline (`art.py`, `art_pipeline/`)

## Making the real art (`tools/art.py`)
spec (`art/spec.json`) -> ComfyUI renders (one per piece) -> transparent layers -> catalog.
Nothing here depends on a particular model: you supply the workflow.

1. **Mannequin base.** Render your neutral mannequin once (A-pose, plain background, 1:2 aspect such as
   1024x2048) and save it as `art/work/base_gray.png`. For garments close to the mannequin's colour
   (grey, white, ...) also render a dark one, `art/work/base_dark.png`, and add
   `{"name": "dark", "base": "base_dark.png"}` to `variants` in the spec. Every piece is then rendered on
   each variant and the best contrast wins; a grey garment on a grey mannequin is otherwise invisible.
2. **Workflow.** Build an image-edit workflow (mannequin image + instruction -> image), give these nodes the
   title shown (right-click > Title) and export with *Save (API Format)*:

   | title | node | |
   |---|---|---|
   | `MAGIK_BASE_IMAGE` | LoadImage | required |
   | `MAGIK_PROMPT` | node with a text input | required |
   | `MAGIK_OUTPUT` | SaveImage / PreviewImage | required |
   | `MAGIK_NEGATIVE`, `MAGIK_SEED` | text node, node with `seed`/`noise_seed` | optional |
   | `MAGIK_MASK` | LoadImage | optional: receives the slot-region mask for an inpaint-lock workflow |

   `python tools/art.py check-workflow wf_api.json` tells you what is missing.
3. **Spec.** `python tools/art.py init-spec` (done: `art/spec.json`). Edit each piece's `render.desc`, the prompt
   template, the per-slot `regions` (rectangles that must fit *your* mannequin) and the `extract` settings.
4. **Run.**

       python tools/art.py render  --workflow wf_api.json --only top_blouse   # drop --only for everything
       python tools/art.py extract --only top_blouse    # layers + art/work/contact_sheet.png + qa.json
       python tools/art.py import  --only top_blouse    # assets/, data/, bake tiles, validate

   `render` skips pieces that already have a render (`--force` redoes them) and uses one fixed seed. `--dry-run`
   prints the prompts. `import` refuses pieces whose QA has errors (`--allow-qa-errors` overrides) and only
   touches pieces that have an extracted layer; `--prune` also removes catalog pieces and outfits that are not in
   the spec, and refuses if an outfit would be left pointing at a removed piece.
5. **QA codes** (`extract` prints them; look at the contact sheet too).

   | code | level | meaning / fix |
   |---|---|---|
   | `empty` | error | nothing differs from the base: garment missing, or too close to the mannequin's colour -> add a variant |
   | `too_small` / `too_large` | error | under 150 px, or over 45% of the canvas: the model changed far more or less than the garment |
   | `outside_region` | error | garment-sized change outside the slot region -> widen the region, tighten the prompt |
   | `low_contrast` | warn | garment within 40/255 of the mannequin on average; parts may be missing -> add a variant |
   | `drift` | warn | the model redraws the whole image; use an inpaint-lock workflow (`MAGIK_MASK`) or lower the denoise |
   | `fragmented` | warn | more separate parts than expected (`max_components`) |

   Extraction tolerates moderate drift (noise of sigma 6 still recovers pieces with IoU > 0.97 on the synthetic
   tests), but real model output is not Gaussian noise: judge by the contact sheet. A segmentation node's
   garment masks can replace the difference alpha: save them as `<piece_id>.png` and pass `--mask-dir`.

What is *not* automated: checking that the generated garment really matches its description (run a tagger or
look at the sheet), and fit between pieces (the `z` and `hides` fields cover most cases).

## Tests
    python -m unittest discover -s tests -v          # Pillow, numpy, aiohttp (node tests need ComfyUI's comfy_api)
    python tests/make_parity_fixtures.py             # only after changing the catalog or the Python logic
                                                     # (includes 400 fuzzed hostile states and Unicode text cases)
    node --test tests/test_logic.mjs                 # JS == Python on 800 randomized cases
    npm i -D jsdom                                   # (or JSDOM_DIR=/path/containing/node_modules)
    node --test tests/test_ui.mjs tests/test_wiring.mjs
