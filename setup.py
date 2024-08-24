from setuptools import setup, Extension
from Cython.Build import cythonize


extensions = [
    Extension("euporie.core", ["euporie/core.py"]),
    Extension("euporie.terminal", ["euporie/terminal.py"]),
    # Adicione outros módulos que deseja compilar com Cython
]

ext_modules = cythonize(
    extensions, compiler_directives={"language_level": 3, "profile": False}
)

setup(
    name="euporie",  # Required
    ext_modules=ext_modules,
)
