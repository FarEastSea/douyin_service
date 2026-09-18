# Third-party notices

## Douyin A-Bogus implementation

`app/services/vendor/douyin_abogus.py` is derived from
`f2/utils/abogus.py` in
[Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2),
revision `7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3`.

- Upstream author: Johnserf-Seed/f2
- License: Apache License 2.0
- Upstream source file SHA-256:
  `82CC97B63AAB2AC80A5C312FE52850CCC74D6FDFFE6EDAEE4E58ADD14083A3C3`

The upstream attribution and license notice is retained in the derived file.
The complete license text is available at
<https://www.apache.org/licenses/LICENSE-2.0>.

`app/services/vendor/douyin_mstoken_bootstrap.py` contains the Douyin
`msToken` bootstrap request constants from `f2/conf/conf.yaml` at the same
upstream revision. The application implements its own bounded cache,
invalidation and retry lifecycle around that public Apache-2.0 contract.

## Xiaohongshu isolated collector

Jenkins installs the separately isolated collector declared in `xhs-engine.lock`
from [Andy-SoulShell/xhs-downloader](https://github.com/Andy-SoulShell/xhs-downloader).
The collector is not copied into this repository and does not share this
application's Python environment.

- Upstream revision: `cc2bb34036acb12f5a722c95af7bad53ec696d03`
- License: MIT License
- Network boundary: loopback HTTP only; the main application performs media
  persistence and task accounting.

The upstream license text remains in the installed source checkout. The source
and license can also be obtained from the repository link above.

`integrations/xhs_profile_collector.py` follows the public user-profile state
field contract exposed by that pinned revision. Its collection, validation and
bounded incremental-scan logic are implemented in this repository.
