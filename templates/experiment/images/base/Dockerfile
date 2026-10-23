# Experiment measurement base: everything any subject's build needs, because
# measurement containers run --network none with --no-build-isolation.
# World-specific by design — the generic base stays generic.
FROM benchmark-python:1

# FILL: the union of every subject's [build-system] backends and plugins.
# RUN python -m pip install --no-cache-dir cython flit-core poetry-core ...

# FILL (only if a subject is Rust): prefetch crates at build time and bake
# the resolved lockfile — both comparison ends must link identical crate
# versions (the subject ships no Cargo.lock; resolve ONCE, here).
# RUN cargo fetch ... && cp Cargo.lock /opt/crates/
