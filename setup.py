from setuptools import setup, Extension
from pathlib import Path

from Cython.Build import cythonize

root = Path("./euporie").resolve()
files = [p.as_posix() for p in root.rglob("*.py")]

extensions = [
    Extension(name="*", sources=files),
    # Extension("euporie.core", ["euporie/core.py"]),
    # Extension("euporie.terminal", ["euporie/terminal.py"]),
]

ext_modules = cythonize(
    extensions, compiler_directives={"language_level": 3, "profile": False}
)

setup(
    name="euporie",  # Required
    ext_modules=ext_modules,
)
