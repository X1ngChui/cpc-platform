#import "../../template/cpc-doc.typ": *

= Development and release <g-dev>

== Conventions

- replaceable parts (policies, sensors) are connected through abstract interfaces and entry points
  (@g-policy); new implementations do not modify the platform;
- state and rules inside a data centre are implemented in `cpc.datacenter` only, called by both the
  simulator and the site agent; what is specific to simulation or live experiments lives in `cpc.sim` and
  `cpc.site`;
- failures follow "report at startup, isolate and report explicitly at run time": no fallback chains, and
  missing data are never replaced by defaults, model estimates or other sources;
- the platform contains no parsers of raw data: new data sources are converted by scripts
  (`examples/datasets/`); synthetic data are for tests only;
- HTTP clients inside the platform use `trust_env=False`, so calls between services bypass the system
  proxy; external downloads use the system proxy settings;
- no secrets, host addresses or personal information are committed: local deployment files are excluded in
  `.gitignore` (`deploy/cloud/*/scenario.yaml`, `.env`, ...);
- code style: four-space indentation, `snake_case`, type annotations; comments, logs and command-line
  output of the code are English; the documentation exists in Chinese and English with the same content.

== Tests

```bash
pytest                                    # unit tests
# end to end: one process per service, including isolation of sensor and site faults
python scripts/smoke_test.py --offline
python scripts/smoke_test.py              # the same with the default scenario (datasets needed)
```

Unit tests use `tests/data/scenario.yaml`: its grid signals are a converted dataset committed with the
tests (`tests/data/datasets/cn-grid-2026-07`), its workload a synthetic dataset generated on first use.
Tests of the data-centre runtime need no clock and call it with explicit times (see
`tests/test_datacenter.py`); sensors without real hardware are tested on fake sysfs trees or command output
(see `tests/test_sensors.py`); failure handling with external dependencies injects connection failures,
503s and timeouts through `httpx.MockTransport` and checks state and metrics (see `tests/test_faults.py`).

== Continuous integration and release

GitHub Actions has two workflows: `ci.yml` runs the unit tests and the offline smoke test on Linux x86_64
and aarch64 and builds the documentation; `image.yml` builds amd64 and arm64 images and pushes them to
`ghcr.io/<user>/cpc-platform` (pushes to `main` give the tags `main` and the commit hash, `v*` tags give
version tags and `latest`). Third-party actions are pinned to commit SHAs and updated by Dependabot.

Versions follow semantic versioning and are released with `vX.Y.Z` tags. The maintainers build and push the
Docker Hub image by hand:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
       -t <user>/cpc-platform:<version> --push .
```

== Documentation

```bash
pip install -e ".[docs]"
python docs/build.py                # builds docs/pdf/{design,guide}-{zh,en}.pdf
python docs/figures/export.py       # exports the figures from draw.io sources (draw.io desktop needed)
```

The documentation is written in Typst: Chinese in `docs/zh/{design,guide}/`, English in
`docs/en/{design,guide}/`, sharing the template `docs/template/cpc-doc.typ`. Figure sources are in
`docs/figures/src/{zh,en}/` (draw.io, mainly black and white) and the exported PDF figures in
`docs/figures/{zh,en}/`; check the rendered figures for misrouted edges and hidden elements after exporting.
References are in `docs/refs.yml` (GB/T 7714 style in Chinese, IEEE style in English). Chinese text uses
the Noto CJK fonts (Debian/Ubuntu package `fonts-noto-cjk`). Code and interface changes update the
documentation in both languages, and the PDFs are rebuilt.
