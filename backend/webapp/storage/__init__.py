"""Storage layer adapters.

The current SQLite implementation remains in the repository-level db.py for
compatibility. New storage helpers should live here and call db.py explicitly.
"""

import db

__all__ = ["db"]
