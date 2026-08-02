# Third-party notices

## OpenMAIC adapter packages

The Web classroom adapter redistributes these exact npm packages from the
[THU-MAIC/OpenMAIC](https://github.com/THU-MAIC/OpenMAIC) repository:

- `@openmaic/dsl` 0.4.0
- `@openmaic/renderer` 0.0.3
- `@openmaic/importer` 0.1.0

Each published package contains the same `LICENSE` file:

```text
MIT License

Copyright (c) 2026 THU-MAIC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

`@openmaic/renderer/fonts.css` is intentionally not imported because it points
at remotely hosted fonts on `file.maic.chat`. The adapter maps renderer font
families to yFeiSTAI's own `--font-sans` and `--font-serif` variables instead.

## Bundled Web fonts

yFeiSTAI uses `next/font` to package the selected Google Fonts with the Web
application rather than requesting them from Google at runtime. The exact
visual-test copies, source URLs, and SHA-256 digests are recorded in
[`web/tests/e2e/support/fonts/README.md`](web/tests/e2e/support/fonts/README.md).

- Geist: SIL Open Font License 1.1. Copyright 2024 The Geist Project Authors.
  The license copy is
  [`web/tests/e2e/support/fonts/OFL-Geist.txt`](web/tests/e2e/support/fonts/OFL-Geist.txt).
- Lora: SIL Open Font License 1.1. Copyright 2011 The Lora Project Authors,
  with Reserved Font Name "Lora". The license copy is
  [`web/tests/e2e/support/fonts/OFL-Lora.txt`](web/tests/e2e/support/fonts/OFL-Lora.txt).

## CSSwitch

- Project: [SuperJJ007/CSSwitch](https://github.com/SuperJJ007/CSSwitch)
- Source commit: `4e0af6ba7909dca22f1257b168172ecbe4af4836`
- License: MIT
- Copyright: Copyright (c) 2026 shanjunjie
- Adapted concepts: PKCE loopback login, auth generations, atomic credential updates, model-catalog cache invalidation, and redacted operation states.

yFeiSTAI's Codex OAuth support draws on the design concepts listed above and
implements them independently against yFeiSTAI's own settings directory, model
catalog, and provider lifecycle. The MIT license text from that source commit
follows:

```text
MIT License

Copyright (c) 2026 shanjunjie

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
