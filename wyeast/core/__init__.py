"""
wyeast.core — stdlib-pure shared runtime for all pipeline steps.

Nothing in this package may import third-party modules: it must load under
venv-phase1, venv-phase1b, venv-phase2, venv-phase3, and venv-phase4 alike
(enforced by tests/unit/test_core_stdlib_pure.py).
"""
