"""The stdlib smoke suite split into subject-sized modules."""
from __future__ import annotations

import sys

from .. import creds as _creds
from .. import server as _server

# The test bodies retain their original relative imports.
creds = _creds
server = _server
sys.modules[f"{__name__}.creds"] = _creds
sys.modules[f"{__name__}.server"] = _server
