"""Import-only additions missing from the upstream MoviePilot test stub.

These are test doubles, never packaged/installed into MoviePilot. They do not
claim to validate the real host's storage contract; the SDK coexistence probe
keeps SDK/client/network behavior real and replaces only the absent host.
"""
import sys
import types
from pydantic import BaseModel


class Response(BaseModel):
    success: bool = True
    message: str | None = None
    data: object = None


class StorageUsage(BaseModel):
    total: int = 0
    available: int = 0


class StorageQueryError(Exception):
    pass


def install():
    import app.schemas
    app.schemas.Response = Response
    app.schemas.StorageUsage = StorageUsage
    module = types.ModuleType("app.schemas.exception")
    module.StorageQueryError = StorageQueryError
    sys.modules.setdefault(module.__name__, module)
