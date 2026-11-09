"""Generate placeholder mannequin + piece layers and the starter catalog.

Flat-color silhouettes on a 512x1024 canvas. Replace with real art later;
keep ids/paths and everything else keeps working.

Run:  python tools/make_placeholders.py [--force]

This OVERWRITES the catalog (data/*.json) and the base/piece art. To protect real work it records a
fingerprint of what it generated and refuses to run again once anything has been edited or replaced
(including by `art.py import`); --force overrides.
"""
import hashlib
import os
import sys

from PIL import Image, ImageDraw

from _common import ASSETS, DATA, save_json

MARKER = os.path.join(DATA, ".placeholder_state")

W, H = 512, 1024


def mir(pts):
    """Mirror a point list across the vertical centre line."""
    return [(W - x, y) for x, y in pts]


def both(pts):
    return [pts, mir(pts)]


# ---- mannequin ----------------------------------------------------------
GRAY = (172, 172, 178, 255)
BODY = [
    ("ellipse", (206, 50, 306, 170)),               # head
    ("poly", [(240, 165), (272, 165), (272, 205), (240, 205)]),  # neck
    ("poly", [(172, 200), (340, 200), (322, 330), (312, 470), (200, 470), (190, 330)]),
    ("poly", [(200, 468), (312, 468), (322, 565), (190, 565)]),  # hips
]
for side in both([(172, 202), (148, 214), (118, 430), (146, 440), (182, 300)]):
    BODY.append(("poly", side))                      # arms
for side in both([(192, 560), (252, 560), (246, 940), (204, 940)]):
    BODY.append(("poly", side))                      # legs
for side in both([(200, 936), (250, 936), (262, 985), (188, 985)]):
    BODY.append(("poly", side))                      # feet


def draw_shapes(shapes, color):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for kind, geo in shapes:
        if kind == "poly":
            d.polygon(geo, fill=color)
        elif kind == "ellipse":
            d.ellipse(geo, fill=color)
        elif kind == "rect":
            d.rectangle(geo, fill=color)
    return img


# ---- pieces -------------------------------------------------------------
def poly_pair(pts):
    return [("poly", p) for p in both(pts)]


TORSO_SHORT = [(172, 200), (340, 200), (322, 330), (316, 440), (196, 440), (190, 330)]
LEGS_FULL = [(192, 560), (252, 560), (246, 930), (204, 930)]
HIPS = [("poly", [(200, 468), (312, 468), (322, 565), (190, 565)])]

PIECES = [
    dict(id="top_tshirt", slot="top", label="T-shirt", tags="t-shirt",
         nl="a t-shirt", color=(70, 120, 200),
         shapes=[("poly", TORSO_SHORT)] + poly_pair([(172, 202), (148, 214), (138, 290), (176, 290)]),
         style=["casual"]),
    dict(id="top_blouse", slot="top", label="Blouse", tags="white shirt, collared shirt",
         nl="a white blouse", color=(240, 225, 230),
         shapes=[("poly", [(172, 200), (340, 200), (322, 330), (316, 430), (196, 430), (190, 330)])]
         + poly_pair([(172, 202), (148, 214), (140, 270), (176, 270)]),
         style=["school", "formal"]),
    dict(id="top_hoodie", slot="top", label="Hoodie", tags="hoodie",
         nl="a hoodie", color=(110, 140, 120),
         shapes=[("poly", [(172, 200), (340, 200), (322, 330), (316, 462), (196, 462), (190, 330)]),
                 ("ellipse", (212, 160, 300, 220))]
         + poly_pair([(172, 202), (146, 214), (116, 432), (148, 444), (182, 300)]),
         style=["casual", "winter"]),
    dict(id="outerwear_jacket", slot="outerwear", label="Jacket", tags="jacket, open jacket",
         nl="an open jacket", color=(130, 85, 55),
         shapes=poly_pair([(168, 198), (256, 198), (256, 452), (194, 452), (186, 330)])
         + poly_pair([(168, 202), (142, 214), (110, 436), (150, 448), (184, 300)]),
         style=["winter", "casual"]),
    dict(id="bottom_jeans", slot="bottom", label="Jeans", tags="jeans, denim",
         nl="jeans", color=(55, 85, 140),
         shapes=HIPS + poly_pair(LEGS_FULL), style=["casual"]),
    dict(id="bottom_pleated_skirt", slot="bottom", label="Pleated skirt", tags="pleated skirt, navy skirt",
         nl="a navy pleated skirt", color=(35, 45, 90),
         shapes=[("poly", [(200, 468), (312, 468), (350, 640), (162, 640)])],
         style=["school"]),
    dict(id="bottom_shorts", slot="bottom", label="Shorts", tags="shorts",
         nl="shorts", color=(190, 170, 120),
         shapes=HIPS + poly_pair([(192, 560), (252, 560), (250, 680), (200, 680)]),
         style=["casual", "summer"]),
    dict(id="onepiece_sundress", slot="onepiece", label="Sundress", tags="sundress, yellow dress",
         nl="a yellow sundress", color=(235, 200, 90),
         shapes=[("poly", [(190, 232), (322, 232), (318, 330), (350, 700), (162, 700)]),
                 ("rect", (200, 200, 216, 234)), ("rect", (296, 200, 312, 234))],
         conflicts=["top", "bottom"], style=["summer"]),
    dict(id="legwear_kneesocks", slot="legwear", label="Knee socks", tags="black kneehighs",
         nl="black knee socks", color=(30, 30, 35),
         shapes=poly_pair([(205, 760), (249, 760), (246, 930), (205, 930)]),
         style=["school"]),
    dict(id="footwear_sneakers", slot="footwear", label="Sneakers", tags="sneakers",
         nl="white sneakers", color=(235, 235, 240),
         shapes=poly_pair([(198, 932), (252, 932), (264, 988), (186, 988)]),
         style=["casual", "summer"]),
    dict(id="footwear_loafers", slot="footwear", label="Loafers", tags="loafers",
         nl="brown loafers", color=(70, 45, 35),
         shapes=poly_pair([(200, 950), (250, 950), (262, 988), (188, 988)]),
         style=["school", "formal"]),
    dict(id="footwear_boots", slot="footwear", label="Boots", tags="boots",
         nl="boots", color=(50, 35, 30),
         shapes=poly_pair([(198, 860), (252, 860), (249, 940), (266, 990), (186, 990), (203, 940)]),
         z=55, hides=["legwear"], style=["winter"]),
    dict(id="headwear_beret", slot="headwear", label="Beret", tags="beret",
         nl="a red beret", color=(170, 50, 60),
         shapes=[("ellipse", (196, 26, 322, 86))], style=["casual"]),
    dict(id="accessory_ribbon", slot="accessory", label="Neck ribbon", tags="neck ribbon",
         nl="a pink neck ribbon", color=(230, 100, 150),
         shapes=[("poly", [(256, 208), (224, 192), (224, 224)]),
                 ("poly", [(256, 208), (288, 192), (288, 224)]),
                 ("ellipse", (246, 198, 266, 218))],
         style=["school"]),
]

OUTFITS = [
    dict(id="casual_01", label="Casual tee", style=["casual"],
         pieces=dict(top="top_tshirt", bottom="bottom_jeans", footwear="footwear_sneakers"), prefix=""),
    dict(id="school_01", label="Sailor uniform", style=["school"],
         pieces=dict(top="top_blouse", bottom="bottom_pleated_skirt", legwear="legwear_kneesocks",
                     footwear="footwear_loafers", accessory="accessory_ribbon"), prefix="school uniform"),
    dict(id="summer_01", label="Summer dress", style=["summer"],
         pieces=dict(onepiece="onepiece_sundress", headwear="headwear_beret", footwear="footwear_sneakers"),
         prefix=""),
    dict(id="winter_01", label="Winter layers", style=["winter"],
         pieces=dict(top="top_hoodie", outerwear="outerwear_jacket", bottom="bottom_jeans",
                     footwear="footwear_boots"), prefix=""),
]

SLOTS = {
    "back": dict(z=0, selectable=False),
    "body": dict(z=10, selectable=False),
    "legwear": dict(z=20, selectable=True),
    "footwear": dict(z=30, selectable=True),
    "bottom": dict(z=40, selectable=True),
    "onepiece": dict(z=45, selectable=True),
    "top": dict(z=50, selectable=True),
    "outerwear": dict(z=60, selectable=True),
    "gloves": dict(z=65, selectable=True),
    "headwear": dict(z=70, selectable=True),
    "accessory": dict(z=80, selectable=True),
}
OUTPUT_ORDER = ["headwear", "onepiece", "top", "outerwear", "bottom", "legwear",
                "footwear", "gloves", "accessory"]


def catalog_fingerprint():
    """Hash of the catalog JSON plus the base/piece art: changes as soon as anything is edited or replaced."""
    paths = [os.path.join(DATA, n) for n in ("mannequin.json", "pieces.json", "outfits.json")]
    for sub in ("base", "pieces"):
        for folder, _, files in os.walk(os.path.join(ASSETS, sub)):
            paths += [os.path.join(folder, f) for f in files]
    digest = hashlib.sha256()
    for path in sorted(p for p in paths if os.path.isfile(p)):
        digest.update(os.path.relpath(path, os.path.dirname(DATA)).replace(os.sep, "/").encode())
        with open(path, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()


def _recorded_fingerprint():
    try:
        with open(MARKER, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def main(argv=None):
    force = "--force" in (sys.argv[1:] if argv is None else argv)
    if not force and os.path.exists(os.path.join(DATA, "pieces.json")) \
            and _recorded_fingerprint() != catalog_fingerprint():
        print("refusing to run: the catalog in data/ and assets/ is not the untouched placeholder set, so "
              "regenerating would overwrite real art or your edits.\nUse --force if that is what you want.")
        return 1
    os.makedirs(os.path.join(ASSETS, "base"), exist_ok=True)
    draw_shapes(BODY, GRAY).save(os.path.join(ASSETS, "base", "mannequin.webp"), lossless=True)

    pieces_json = []
    for p in PIECES:
        name = p["id"].split("_", 1)[1]
        rel = f"pieces/{p['slot']}/{name}.webp"
        os.makedirs(os.path.join(ASSETS, "pieces", p["slot"]), exist_ok=True)
        draw_shapes(p["shapes"], p["color"] + (255,)).save(os.path.join(ASSETS, rel), lossless=True)
        pieces_json.append(dict(
            id=p["id"], slot=p["slot"], label=p["label"], tags=p["tags"], nl=p["nl"],
            layer=rel, back_layer=None, z=p.get("z"), hides=p.get("hides", []),
            conflicts=p.get("conflicts", []), style=p["style"], colorable=False, bbox=None))

    outfits_json = []
    for o in OUTFITS:
        outfits_json.append(dict(o, tile=f"outfits/{o['id']}.webp"))

    save_json("mannequin.json", dict(
        canvas=[W, H], tile=[192, 384], base="base/mannequin.webp",
        slots=SLOTS, output_order=OUTPUT_ORDER))
    save_json("pieces.json", pieces_json)
    save_json("outfits.json", outfits_json)
    print(f"placeholders: {len(pieces_json)} pieces, {len(outfits_json)} outfits")
    # complete the catalog (tiles + bboxes), then remember exactly what we produced
    import bake_tiles
    bake_tiles.main([])
    with open(MARKER, "w", encoding="utf-8") as f:
        f.write(catalog_fingerprint() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
