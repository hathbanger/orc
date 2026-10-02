"""Repository tests (also importable with python -m unittest test.<module>).

Importing the package points FUSION_LAYA_PYTHON at `false`, so a test that
reaches the Laya runtime without a fake backend fails fast instead of loading
the real model. `make test` sets the same value before discovery; this covers
`python -m unittest test.<module>`, the form agents use."""
import os
import shutil

os.environ.setdefault('FUSION_LAYA_PYTHON', shutil.which('false') or '/usr/bin/false')
