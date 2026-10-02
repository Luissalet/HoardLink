"""Compatibility shim: the Faustus mail helper moved to ``hoard_link/mail_helper.py`` (0.8, the commons).

The one helper now serves the hub's mail gateway AND every app (``hoard_link.fam_mail.FaustusHelper``). This file keeps the old
module path working:

* ``from hoard_link.hub import mail_helper`` exposes everything the real module defines (public and private names alike);
* ``python hub/mail_helper.py <faustus root>`` (JSON on stdin) runs the real file as a script.

Nothing is implemented here.
"""

from __future__ import annotations

import os
import sys

_REAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mail_helper.py")

if __name__ == "__main__":
    # Run as a script this folder is sys.path[0] and holds the hub's own mcp.py, config.py ...: Faustus's packages must not be shadowed.
    _HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
    import runpy

    runpy.run_path(_REAL, run_name="__main__")
else:
    from .. import mail_helper as _real

    globals().update({k: v for k, v in vars(_real).items() if not (k.startswith("__") and k.endswith("__"))})
    __file__ = _real.__file__          # tools that locate the script through this module get the real one
