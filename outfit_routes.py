"""aiohttp routes for Magik Outfit.

  GET  /magik/outfit/catalog                  merged catalog (pieces, outfits, asset URLs)
  GET  /magik/outfit/asset/{source}/{path}    layer / tile / base images
  POST /magik/outfit/save_outfit              {label, pieces, style?, prefix?}
  POST /magik/outfit/delete_outfit            {id}   (user outfits only)

add_routes() takes any aiohttp RouteTableDef-like object (PromptServer.instance.routes
in ComfyUI) so the routes can be tested against a plain aiohttp app.
"""
import asyncio

from aiohttp import web

try:  # package import (inside ComfyUI) or flat import (tests / tools)
    from .outfit_core import CatalogError
except ImportError:
    from outfit_core import CatalogError


def _error(message, status=400):
    return web.json_response({"error": message}, status=status)


async def _json_body(request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def add_routes(routes, catalog):
    @routes.get("/magik/outfit/catalog")
    async def get_catalog(request):
        try:
            return web.json_response(catalog.public())
        except CatalogError as e:
            return _error(str(e), 500)

    @routes.get("/magik/outfit/asset/{source}/{path:.*}")
    async def get_asset(request):
        path = catalog.find_asset(request.match_info["source"], request.match_info["path"])
        if path is None:
            return web.Response(status=404)
        # no-cache = always revalidate (ETag/Last-Modified -> 304), so edited art shows up.
        # Content-Type is set explicitly: the OS mime database may not know .webp.
        content_type = "image/png" if path.lower().endswith(".png") else "image/webp"
        return web.FileResponse(path, headers={"Cache-Control": "no-cache",
                                               "Content-Type": content_type})

    @routes.post("/magik/outfit/save_outfit")
    async def save_outfit(request):
        body = await _json_body(request)
        if body is None:
            return _error("invalid JSON body")
        loop = asyncio.get_running_loop()
        try:
            entry = await loop.run_in_executor(
                None, lambda: catalog.save_user_outfit(
                    body.get("label"), body.get("pieces"), body.get("style"), body.get("prefix", "")))
        except CatalogError as e:
            return _error(str(e))
        except OSError as e:
            return _error(f"could not write outfit: {e.__class__.__name__}", 500)
        outfit = next((o for o in catalog.public()["outfits"] if o["id"] == entry["id"]), None)
        return web.json_response({"outfit": outfit})

    @routes.post("/magik/outfit/delete_outfit")
    async def delete_outfit(request):
        body = await _json_body(request)
        if body is None or not isinstance(body.get("id"), str):
            return _error("invalid request")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: catalog.delete_user_outfit(body["id"]))
        except CatalogError as e:
            return _error(str(e), 404)
        except OSError as e:
            return _error(f"could not write outfits: {e.__class__.__name__}", 500)
        return web.json_response({"ok": True})
