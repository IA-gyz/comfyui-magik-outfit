"""Drive a ComfyUI server with the user's own API-format workflow.

The workflow is exported with "Save (API Format)" and nodes are found by *title*
(right-click a node > Title), so the pipeline never needs to know which model or
node classes the graph uses:

  MAGIK_BASE_IMAGE   required  a LoadImage node; receives the mannequin render
  MAGIK_PROMPT       required  node whose text input receives the piece prompt
  MAGIK_OUTPUT       required  a SaveImage (or PreviewImage) node; its image is the result
  MAGIK_NEGATIVE     optional  node whose text input receives the negative prompt
  MAGIK_SEED         optional  node with a seed / noise_seed input (kept fixed for consistency)
  MAGIK_MASK         optional  a LoadImage node; receives the slot-region mask (inpaint lock)
"""
import copy
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

TITLES = {
    "base_image": "MAGIK_BASE_IMAGE",
    "prompt": "MAGIK_PROMPT",
    "negative": "MAGIK_NEGATIVE",
    "seed": "MAGIK_SEED",
    "mask": "MAGIK_MASK",
    "output": "MAGIK_OUTPUT",
}
REQUIRED = ("base_image", "prompt", "output")
TEXT_KEYS = ("text", "prompt", "string", "value")
SEED_KEYS = ("seed", "noise_seed")


class WorkflowError(ValueError):
    """The workflow template is unusable (wrong format, missing titled nodes)."""


class ComfyError(RuntimeError):
    """The server could not be reached, rejected the prompt, or the job failed."""


# ---------------------------------------------------------------- workflow
def find_nodes(workflow, titles=TITLES):
    """Map role -> [node ids] for every titled node in an API-format workflow."""
    found = {role: [] for role in titles}
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or "class_type" not in node:
            raise WorkflowError(
                "not an API-format workflow (use 'Save (API Format)'): node "
                f"{node_id!r} has no class_type")
        title = (node.get("_meta") or {}).get("title")
        for role, wanted in titles.items():
            if title == wanted:
                found[role].append(node_id)
    return found


def _set_text(node, value, role, key=None):
    """Write `value` into the node's text input (an explicit `key`, else the first known text key)."""
    inputs = node.setdefault("inputs", {})
    if key:
        if key not in inputs:
            raise WorkflowError(f"{role} node has no input named {key!r}")
        inputs[key] = value
        return
    for k in TEXT_KEYS:
        if isinstance(inputs.get(k), str):  # a linked input is a [node_id, slot] list, which we cannot set
            inputs[k] = value
            return
    raise WorkflowError(f"{role} node has no text input (looked for {', '.join(TEXT_KEYS)}); pass --text-key")


def check_workflow(workflow, titles=TITLES):
    """Return (ok, problems, found) without modifying anything."""
    try:
        found = find_nodes(workflow, titles)
    except WorkflowError as e:
        return False, [str(e)], {}
    problems = []
    for role in REQUIRED:
        if not found[role]:
            problems.append(f"missing a node titled {titles[role]!r}")
        elif len(found[role]) > 1:
            problems.append(f"{len(found[role])} nodes are titled {titles[role]!r}; exactly one is expected")
    for role in ("negative", "seed", "mask"):
        if len(found[role]) > 1:
            problems.append(f"{len(found[role])} nodes are titled {titles[role]!r}; at most one is expected")
    return not problems, problems, found


def prepare_workflow(template, *, base_name, prompt, negative=None, seed=None, mask_name=None,
                     titles=TITLES, text_key=None):
    """Return a patched deep copy of `template` ready to queue, plus the output node id."""
    ok, problems, found = check_workflow(template, titles)
    if not ok:
        raise WorkflowError("; ".join(problems))
    wf = copy.deepcopy(template)

    wf[found["base_image"][0]].setdefault("inputs", {})["image"] = base_name
    _set_text(wf[found["prompt"][0]], prompt, "prompt", text_key)

    if negative is not None and found["negative"]:
        _set_text(wf[found["negative"][0]], negative, "negative", text_key)
    if seed is not None and found["seed"]:
        inputs = wf[found["seed"][0]].setdefault("inputs", {})
        key = next((k for k in SEED_KEYS if k in inputs), None)
        if key is None:
            raise WorkflowError(f"seed node has no {' / '.join(SEED_KEYS)} input")
        inputs[key] = int(seed)
    if mask_name is not None and found["mask"]:
        wf[found["mask"][0]].setdefault("inputs", {})["image"] = mask_name
    return wf, found["output"][0]


# ------------------------------------------------------------------- HTTP
def _multipart(fields, file_field, filename, data, content_type="image/png"):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                  f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n').encode())
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class ComfyClient:
    def __init__(self, server="http://127.0.0.1:8188", timeout=900.0, poll=0.5, request_timeout=60.0):
        self.server = server.rstrip("/")
        self.timeout = timeout
        self.poll = poll
        self.request_timeout = request_timeout
        self.client_id = uuid.uuid4().hex
        # A local ComfyUI must never be routed through an http_proxy from the environment.
        host = urllib.parse.urlsplit(self.server).hostname or ""
        if host in ("localhost", "::1") or host.startswith("127."):
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    # -- low level
    def _request(self, method, route, body=None, headers=None, parse_json=True):
        req = urllib.request.Request(self.server + route, data=body, method=method, headers=headers or {})
        try:
            with self._opener.open(req, timeout=self.request_timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            raise ComfyError(f"{method} {route} -> HTTP {e.code}: {self._error_text(e.read())}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ComfyError(f"cannot reach ComfyUI at {self.server}: {getattr(e, 'reason', e)}") from None
        if not parse_json:
            return data
        try:
            return json.loads(data)
        except ValueError:
            raise ComfyError(f"{method} {route}: server did not return JSON") from None

    @staticmethod
    def _error_text(raw):
        try:
            data = json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")[:300]
        parts = []
        err = data.get("error")
        if isinstance(err, dict):
            parts.append(err.get("message") or err.get("type") or "error")
            if err.get("details"):
                parts.append(str(err["details"]))
        elif err:
            parts.append(str(err))
        for node_id, info in (data.get("node_errors") or {}).items():
            for e in info.get("errors", []):
                parts.append(f"node {node_id}: {e.get('message')} {e.get('details', '')}".strip())
        return "; ".join(parts) or json.dumps(data)[:300]

    # -- API
    def upload_image(self, data, name, overwrite=True):
        """Upload PNG bytes to ComfyUI's input folder; returns the name to put in a LoadImage node."""
        body, ctype = _multipart({"overwrite": "true" if overwrite else "false", "type": "input"},
                                 "image", name, data)
        info = self._request("POST", "/upload/image", body, {"Content-Type": ctype})
        sub = info.get("subfolder") or ""
        return f"{sub}/{info['name']}" if sub else info["name"]

    def queue_prompt(self, workflow):
        payload = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode()
        info = self._request("POST", "/prompt", payload, {"Content-Type": "application/json"})
        if "prompt_id" not in info:
            raise ComfyError(f"unexpected /prompt response: {str(info)[:200]}")
        return info["prompt_id"]

    def wait(self, prompt_id):
        """Block until the job finishes; returns the history entry's outputs dict."""
        deadline = time.monotonic() + self.timeout
        while True:
            history = self._request("GET", f"/history/{urllib.parse.quote(prompt_id)}")
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyError(f"job failed: {self._job_error(status)}")
                if entry.get("outputs") or status.get("completed"):
                    return entry.get("outputs") or {}
            if time.monotonic() > deadline:
                raise ComfyError(f"timed out after {self.timeout:.0f}s waiting for {prompt_id}")
            time.sleep(self.poll)

    @staticmethod
    def _job_error(status):
        for kind, payload in status.get("messages", []):
            if kind == "execution_error" and isinstance(payload, dict):
                return f"{payload.get('node_type', '?')}: {payload.get('exception_message', '').strip()}"
        return "see the ComfyUI console"

    def fetch_image(self, info):
        q = urllib.parse.urlencode({"filename": info["filename"], "subfolder": info.get("subfolder", ""),
                                    "type": info.get("type", "output")})
        return self._request("GET", f"/view?{q}", parse_json=False)

    def run(self, workflow, output_node):
        """Queue `workflow`, wait, and return the first image of `output_node` as PNG bytes."""
        outputs = self.wait(self.queue_prompt(workflow))
        images = (outputs.get(output_node) or {}).get("images") or []
        if not images:
            raise ComfyError(f"the output node ({output_node}) produced no image")
        return self.fetch_image(images[0])
