# Make tests/adapters a package so the ``conftest`` under it does not have a
# module-name collision with the top-level ``tests/conftest.py`` (shared loop
# core helpers). In pytest's prepend import mode, without __init__.py both
# conftest files are named ``conftest``, so a root-level
# ``from conftest import ...`` can accidentally pick up this adapter conftest.
# With __init__.py in place, this subtree is imported as ``adapters.*``, which
# avoids the collision.
