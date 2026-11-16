# Notice

This repository is a wrapper around **MiroFish** (https://github.com/666ghj/MiroFish). It does not contain MiroFish: `bootstrap` clones upstream at the commit pinned in `ops/UPSTREAM.md` into `mirofish/`, which is git-ignored and never committed here.

## Upstream license

Upstream's `LICENSE` file (read at commit `7657031ac01184afe2cb220f5ee3545573b5e843` on 2026-11-14) is the **GNU Affero General Public License, version 3** (AGPL-3.0, "Version 3, 19 November 2007", Free Software Foundation). MiroFish is subject to that license. Re-check this file if the pinned commit changes.

## Patches are derived works

Anything in `ops/patches/` modifies upstream MiroFish source. Those patches are derived from upstream and are subject to the AGPL-3.0. In particular, if you run a modified MiroFish as a network service that other people use, the AGPL requires offering them its corresponding source. Private use on your own machine does not trigger that obligation.

## License of the wrapper code

The wrapper code in this repository (everything outside `ops/patches/`) is licensed under the GNU Affero General Public License v3.0; the full text is in `LICENSE`.

## Third-party tools

`ops/tools/install_gitleaks.py` downloads [gitleaks](https://github.com/gitleaks/gitleaks) (MIT license) into the git-ignored `.tools/` directory for secret scanning. It is not redistributed in this repository.
