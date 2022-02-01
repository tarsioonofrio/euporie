"""This package defines the euporie application and its components."""

__app_name__ = "euporie"
__version__ = "0.2.0-dev"
__logo__ = "⚈"
__strapline__ = "A TUI editor for Jupyter notebooks"
__author__ = "Josiah Outram Halstead"
__email__ = "josiah@halstead.email"
__copyright__ = f"© 2021, {__author__}"
__license__ = "MIT"

from euporie import (
    app,
    box,
    cell,
    commands,
    completion,
    config,
    containers,
    filters,
    graphics,
    kernel,
    key_binding,
    keys,
    log,
    markdown,
    menu,
    notebook,
    output,
    render,
    scroll,
    style,
    suggest,
    tab,
    terminal,
    text,
)

__all__ = [
    "app",
    "box",
    "cell",
    "commands",
    "completion",
    "config",
    "containers",
    "filters",
    "graphics",
    "kernel",
    "key_binding",
    "keys",
    "log",
    "markdown",
    "menu",
    "notebook",
    "output",
    "render",
    "scroll",
    "style",
    "suggest",
    "tab",
    "terminal",
    "text",
]
