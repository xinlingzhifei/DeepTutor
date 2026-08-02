# Playwright font fixtures

These files make the visual baseline independent of Google Fonts network
availability. They are used only by the Playwright-managed Next.js server.

| File | Official source | SHA-256 | License |
| --- | --- | --- | --- |
| `geist-latin.woff2` | `https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwcGFU.woff2` | `19f9c92546aa300c312235e3125af1b81394d8db9a4bc4a425cd5b641d2d54e1` | [SIL OFL 1.1](./OFL-Geist.txt) |
| `lora-latin.woff2` | `https://fonts.gstatic.com/s/lora/v37/0QIvMX1D_JOuMwr7Iw.woff2` | `ddb8c66035104e233fc024669183aad3738b6daa16deee2ebb1241bd0f98ace1` | [SIL OFL 1.1](./OFL-Lora.txt) |

The corresponding CSS endpoints are:

- `https://fonts.googleapis.com/css2?family=Geist:wght@100..900&display=swap`
- `https://fonts.googleapis.com/css2?family=Lora:wght@400..700&display=swap`

The license copies come from the official Google Fonts repository:

- `https://github.com/google/fonts/blob/main/ofl/geist/OFL.txt`
- `https://github.com/google/fonts/blob/main/ofl/lora/OFL.txt`
