"""Hoard Link: the shared model backend for agent-controlled apps.

A tiny, dependency-light library (stdlib + ``httpx``) that answers, for a
capability (``llm``, ``vision``, ``embeddings``, ``tts``, ``stt``,
``image``, ``video``, ``music``): which server and model to use right now,
and why — favoring servers other local apps (and Faustus) already have
resident, so a GPU-bound machine is never asked to load a second copy of
the same kind of model.

See ``README.md`` for the resolution order, the policies and how an app
vendors this package.
"""

from .config import CapabilityConfig, LinkConfig
from .errors import BackendError, HoardLinkError, Unavailable
from .gpu import GpuMemory, gpu_free_mb
from .link import Link
from ._comfy import ComfyClient
from .types import CAPABILITIES, ChatResult, OutputFile, Resolution, Usage

__version__ = "0.1.1"

__all__ = [
    "__version__",
    "Link",
    "LinkConfig",
    "CapabilityConfig",
    "ComfyClient",
    "Resolution",
    "ChatResult",
    "Usage",
    "OutputFile",
    "Unavailable",
    "BackendError",
    "HoardLinkError",
    "gpu_free_mb",
    "GpuMemory",
    "CAPABILITIES",
]
