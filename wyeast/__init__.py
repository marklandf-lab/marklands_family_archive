"""
wyeast — shared library for the Digital Estate Recovery pipeline.

Lives in the scripts directory alongside the numbered step scripts. Because
Python places a script's own directory on sys.path[0] at launch,
`from wyeast.core.io import atomic_write_json` resolves for any step script
run directly, under every venv.

Layout:
  wyeast.core    stdlib-pure shared runtime (config, paths, logging, io,
                 custody, ollama client, stage registry). MUST stay free of
                 third-party imports so it loads under every step venv.
  wyeast.stages  per-stage implementations, invoked by the thin numbered
                 shim scripts at the repo root. Stage modules may import
                 venv-specific dependencies.

See docs/specs/restructuring-spec.md for the migration plan.
"""
