"""Reading and writing the ``.trout/`` directory.

``.trout/`` is the single source of truth. The CLI, the web interface, and any
agent driving the tool all read and write the same files, so there is never a
question of which view is authoritative — and a developer can hand-edit any of
it, which is the point. See ``docs/adr/0004-state-on-disk.md``.
"""

from testtrout.store.dotenv import load as load_dotenv
from testtrout.store.paths import QaPaths
from testtrout.store.sync import apply_scan
from testtrout.store.yaml_io import read_model, write_model

__all__ = ["QaPaths", "apply_scan", "load_dotenv", "read_model", "write_model"]
