# One image per downstream project (copy this directory per evaluator). The
# project's checkout lives at /study/ruler, at its pinned tag, with its own test
# and benchmark dependencies baked in: the network exists at build time, never
# at measure time. Build from the checkout:
#   git clone --depth 1 --branch <tag> <url> /tmp/ruler
#   docker build -f images/ruler/Dockerfile -t swecraft-eval-ruler:1 /tmp/ruler
# A baked tag is an epoch: never rebuild a tag in place.
# The images/base build.
FROM swecraft-experiment-base:1
WORKDIR /study/ruler
# FILL: the project's own test and benchmark dependencies, pinned, e.g.:
# RUN python -m pip install --no-cache-dir "pytest>=8,<9" "hypothesis>=6,<7"
COPY . /study/ruler
# The project itself, as users install it; the evaluator then injects the
# world's subjects over its dependencies with `python -m pip`.
RUN python -m pip install --no-cache-dir --no-deps /study/ruler
