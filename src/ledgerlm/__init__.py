"""LedgerLM: local-first cost ledger for LLM API calls."""

from ledgerlm.tagging import tags
from ledgerlm.wrapper import wrap

__version__ = "0.0.1"

__all__ = ["__version__", "tags", "wrap"]
