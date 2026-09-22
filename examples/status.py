"""Print what Hoard Link resolves for every capability on this machine.

Run from a clone after ``pip install -e .``:

    python examples/status.py [path/to/backend.json]

It only probes loopback (never a remote host), never loads a model and
never raises: a capability with no server behind it is printed as
``unavailable`` together with the reasons Hoard Link collected.
"""

import asyncio
import sys

from hoard_link import CAPABILITIES, Link, LinkConfig, __version__


async def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    async with Link(LinkConfig.load(path, app="status-example")) as link:
        status = await link.status()
    print(f"hoard-link {__version__}")
    for cap in CAPABILITIES:
        res = status[cap]
        print(f"{cap:>10}  {res['state']:<11}  {res['reason']}")


if __name__ == "__main__":
    asyncio.run(main())
