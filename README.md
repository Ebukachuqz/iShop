# iShop

iShop is an agentic voice-shopping assistant for Shopify. Its storefront assistant, **Drake**, helps shoppers find products, compare options, open product pages, manage their cart, inspect cart contents, and continue safely to Shopify checkout using natural speech or typed messages.

iShop is designed for shoppers who may not be comfortable navigating online stores, shoppers with accessibility needs, and anyone who finds it easier to describe what they want than to work through menus and filters. Drake supports English and Nigerian Pidgin interactions, including code-switched requests.

## What Drake can do

- Search the store and display results on Shopify's native search page.
- Resolve follow-ups such as “open the second one” against the results the shopper saw.
- Open a named product or selected variant.
- Compare products and answer product questions using current storefront evidence.
- Add products, change quantities, remove lines, and show the cart.
- Open supported cart drawers, navigate home or back, and scroll incrementally.
- Hand the shopper over to Shopify checkout without entering payment details or placing the order.
- Support manual recording and continuous voice conversations with merchant-selected speech, reasoning, and voice providers.

Product, price, availability, variant, and cart claims come from Shopify evidence. Cart mutations are checked against the current cart and verified after execution before Drake reports success.

## Repository structure

```text
apps/shopify/       Embedded Shopify app and Drake theme extension
services/runtime/   FastAPI voice, reasoning, and shopping runtime
packages/contracts/ Shared browser contracts and JavaScript tests
evals/              Speech and downstream evaluation tooling
scripts/            Workspace commands and validation utilities
```

## Evaluation

Public benchmark results are available in [Evaluation reports](evals/reports/README.md). The current report compares Sahara, Groq Whisper Large v3, AssemblyAI Universal-2, and ElevenLabs Scribe v2 across English, Nigerian Pidgin, Yoruba, and code-switched speech, including WER, CER, coverage, latency, and downstream shopping-task recoverability.

Raw recordings, private manifests, and provider responses are excluded from Git.

## Prerequisites

Install the following before starting:

- Node.js 22.12 through 22.x
- pnpm 9.15.4
- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- A Shopify Partner account and development store
- [Shopify CLI](https://shopify.dev/docs/api/shopify-cli)
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) or another HTTPS/WSS tunnel for the local voice runtime
- Credentials for the providers you intend to enable

Sahara/Intron is the primary Nigerian Pidgin speech provider. The repository also contains supported profiles for Groq, AssemblyAI, ElevenLabs, and Gemini. Availability depends on credentials, account access, and the capability flags in `.env`.

## Install dependencies

```bash
git clone <repository-url>
cd iShop
corepack enable
corepack prepare pnpm@9.15.4 --activate
pnpm install
python -m uv sync --frozen
```

Alternatively, after pnpm and uv are installed:

```bash
pnpm bootstrap
```

## Configure the environment

Copy the example file at the repository root:

```powershell
Copy-Item .env.example .env
```

On macOS or Linux:

```bash
cp .env.example .env
```

At minimum, configure:

- `SHOPIFY_APP_CLIENT_ID` and `SHOPIFY_APP_CLIENT_SECRET`
- `DEV_ALLOWED_SHOP_DOMAINS`
- `DEV_ALLOWED_ORIGINS`
- `SESSION_SIGNING_SECRET` with at least 32 random characters
- `SAHARA_API_KEY` for the default Sahara speech profile
- The API keys for any additional provider profiles you want merchants to select

Keep `.env` private. Provider credentials are server-side and must never be added to the theme extension or committed to Git.

## Link and run the Shopify app

Authenticate Shopify CLI if needed, then link this checkout to your Shopify app:

```bash
pnpm --filter @ishop/shopify-app config:link
```

Start the voice runtime in the first terminal:

```bash
pnpm dev:runtime
```

It listens on `http://127.0.0.1:8000` by default.

Expose the runtime through a secure tunnel in a second terminal:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Copy the generated `https://...trycloudflare.com` hostname. Its WebSocket base URL is:

```text
wss://<generated-hostname>/ws
```

Start the Shopify application in a third terminal:

```bash
pnpm --filter @ishop/shopify-app dev
```

Follow Shopify CLI's prompts to select the development store and install or update the app. Shopify CLI supplies the app's development tunnel automatically.

## Enable Drake on the storefront

1. In Shopify Admin, open **Online Store → Themes → Customize**.
2. Open **App embeds**.
3. Enable **Drake voice shopping**.
4. Set **Voice runtime WebSocket URL** to the secure runtime URL from the previous step, ending in `/ws`.
5. Save the theme.
6. Open the installed iShop app in Shopify Admin and select the desired speech recognition, reasoning, and voice profiles.
7. Open the storefront in a fresh tab and allow microphone access when testing voice.

Provider-setting changes apply to new shopper sessions. Refresh the storefront after changing a profile or restarting either development service.

## Local development commands

```bash
pnpm dev:runtime       # Start the FastAPI voice runtime
pnpm test:unit         # Run JavaScript, Shopify, and Python unit tests
pnpm test:integration  # Run storefront bridge and commerce integration tests
pnpm test:e2e          # Run connected storefront contract checks
pnpm check             # Run validation, privacy, boundary, and type checks
```

To run only the browser contract tests:

```bash
node --test packages/contracts/tests/*.test.js
```

To run the runtime tests directly:

```bash
python -m uv run --frozen --package ishop-runtime pytest services/runtime/tests
```

## Troubleshooting

### The embedded app refuses to connect

Shopify development URLs change between sessions. Start the app through Shopify CLI and do not force an old development URL into the process.

### Drake cannot connect to the voice runtime

Confirm that the runtime and tunnel are both running and that the theme app embed contains the current `wss://.../ws` URL. Temporary Cloudflare hostnames change whenever the tunnel restarts.

### A provider is unavailable in merchant settings

Confirm that its API key and corresponding `ISHOP_ENABLE_*` capability flag are configured. Restart the Shopify app and runtime, then begin a new shopper session.

### Sahara voice sessions fail with a WebSocket protocol error

The runtime reports the provider failure without authorizing a shopping action. For a development demonstration, select another enabled realtime speech provider while the upstream Sahara connection is unavailable.

### Product or cart data appears stale

Refresh the storefront after restarting development services. Drake deliberately rejects stale page, result-set, and cart evidence rather than acting on it.

## Privacy and safety

- Microphone capture starts only after shopper interaction and can be stopped at any time.
- Audio recording is disabled by default.
- Provider credentials remain on the server.
- Store content is treated as untrusted evidence, never executable instruction.
- Cart changes require current Shopify evidence and verified read-back.
- Checkout is a human-controlled handoff; Drake does not enter payment information or place orders.
- Private benchmark recordings and evaluation outputs are excluded from Git.

## License

No open-source license has been granted for this repository. Unless a license file is added, the source remains subject to the repository owner's rights.
