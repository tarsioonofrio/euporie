"""Defines input history loaders."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prompt_toolkit.history import History

if TYPE_CHECKING:
    from typing import Iterable

    from euporie.core.kernel import NotebookKernel

log = logging.getLogger(__name__)


class KernelHistory(History):
    """Load the kernel's command history."""

    def __init__(self, kernel: "NotebookKernel", n: "int" = 1000) -> "None":
        """Create a new instance of the kernel history loader."""
        super().__init__()
        self.kernel = kernel
        # How many items to load
        self.n = n

    def load_history_strings(self) -> "Iterable[str]":
        """Load lines from kernel history."""
        log.debug("Loading kernel history")
        result = self.kernel.history(n=self.n, hist_access_type="tail")
        for item in reversed(result or []):
            # Each item is a thruple: (session, line_number, input)
            yield item[2]

    def store_string(self, string: "str") -> "None":
        """Don't store strings."""
        pass
