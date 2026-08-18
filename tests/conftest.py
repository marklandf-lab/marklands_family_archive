import sys
from pathlib import Path

# The repo root is the scripts directory: step scripts get it on sys.path[0]
# when run directly. Tests need the same so `wyeast` (incl. wyeast.embed and
# wyeast.core.io) imports identically to production.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
