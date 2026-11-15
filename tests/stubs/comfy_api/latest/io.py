"""Minimal stub of comfy_api.latest.io: just enough for magik_outfit.py's node tests."""


class NodeOutput:
    def __init__(self, *args):
        self.result = args


class _Input:
    def __init__(self, id, **kwargs):
        self.id = id
        self.optional = kwargs.pop("optional", False)
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Output:
    def __init__(self, id, **kwargs):
        self.id = id
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Type:
    @staticmethod
    def Input(id, **kwargs):
        return _Input(id, **kwargs)

    @staticmethod
    def Output(id, **kwargs):
        return _Output(id, **kwargs)


class String(_Type):
    pass


class Combo(_Type):
    pass


class Boolean(_Type):
    pass


class Int(_Type):
    pass


class Image(_Type):
    pass


class Schema:
    def __init__(self, *, node_id, inputs=None, outputs=None, **kwargs):
        self.node_id = node_id
        self.inputs = [i for i in (inputs or []) if not getattr(i, "optional", False)]
        self.outputs = list(outputs or [])
        for key, value in kwargs.items():
            setattr(self, key, value)


class ComfyNode:
    @classmethod
    def GET_SCHEMA(cls):
        return cls.define_schema()
