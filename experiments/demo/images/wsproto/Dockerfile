FROM benchmark-python:1

WORKDIR /study/ruler

# Install the project's declared runtime and focused-test dependencies first.
# Keeping this layer ahead of the source copy lets ordinary source changes reuse it.
COPY pyproject.toml README.rst LICENSE MANIFEST.in /study/ruler/
RUN python -m pip install --no-cache-dir "h11>=0.16.0,<1" "pytest>=8.3.3,<9" "hypothesis>=6.119.4,<7"

# Build and install the same pure-Python wheel users receive.  A later full copy
# restores the exact checkout (including tests, benchmarks, and the adapter)
# without making evaluator-only edits invalidate the install layer.
COPY src /study/ruler/src
RUN python -m pip install --no-cache-dir --no-deps /study/ruler
COPY . /study/ruler

# battery v2.2: the evaluator injects subjects with `python -m pip`; a uv-managed ruler venv has no pip
RUN python -m pip --version >/dev/null 2>&1 || python -m ensurepip --upgrade
