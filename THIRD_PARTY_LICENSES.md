# Third-party components

Every third-party component in this repository is listed here with its SPDX
identifier, read from the component's own license text.

| Component | Version / source | License (SPDX) | Where |
|---|---|---|---|
| QR Code generator library (Python), © Project Nayuki | current `master` at <https://github.com/nayuki/QR-Code-generator>, commit at fetch time 2026-10-27 | MIT | vendored as `qrcodegen.py` |
| Barlow and Barlow Condensed fonts, © 2017 The Barlow Project Authors | Latin subset WOFF2 from Google Fonts (fonts.gstatic.com), fetched 2026-10-30 | OFL-1.1 | `static/fonts/Barlow*.woff2`; license text in `static/fonts/OFL-Barlow.txt` and `OFL-BarlowCondensed.txt` |
| Big Shoulders Stencil font, © 2019 The Big Shoulders Project Authors | Latin subset WOFF2 from Google Fonts (fonts.gstatic.com), fetched 2026-10-30 | OFL-1.1 | `static/fonts/BigShouldersStencil*.woff2`; license text in `static/fonts/OFL-BigShouldersStencil.txt` |

The fonts are served from this repository, so game nights need no internet
access. Their license text comes from the `google/fonts` repository.

## qrcodegen.py (Project Nayuki)

Vendored verbatim (single file, no modifications) as `qrcodegen.py`. The MIT
license text is present in the file header:

> Copyright (c) Project Nayuki. (MIT License)
> https://www.nayuki.io/page/qr-code-generator-library
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of
> this software and associated documentation files (the "Software"), to deal in
> the Software without restriction, including without limitation the rights to
> use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
> the Software, and to permit persons to whom the Software is furnished to do so,
> subject to the following conditions:
> - The above copyright notice and this permission notice shall be included in
>   all copies or substantial portions of the Software.
> - The Software is provided "as is", without warranty of any kind, express or
>   implied, including but not limited to the warranties of merchantability,
>   fitness for a particular purpose and noninfringement. In no event shall the
>   authors or copyright holders be liable for any claim, damages or other
>   liability, whether in an action of contract, tort or otherwise, arising from,
>   out of or in connection with the Software or the use or other dealings in the
>   Software.

No AGPL, GPL, MPL or source-available code is vendored into this tree.
Apart from the components listed above, this repository is original work under
the MIT `LICENSE`.

## Question content

Generated questions are AI output and are not original works; see the content
policy in README.md. No third-party question dataset is
bundled or reproduced.
