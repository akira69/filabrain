# FilaBrain for FilaMan

![FilaBrain AI connected to FilaMan over Ethernet](assets/filabrain-wide.webp)

FilaBrain is a FilaMan integration plugin maintained by `akira69`. It uses the FilaMan library, checks manufacturer and catalog sources, and presents proposals for review. It supports an OpenAI-compatible model server, including Ollama, and can expose its tools to a separate MCP client.

## Install

Build the plugin ZIP with Python 3.

```sh
python3 scripts/package_plugin.py
```

Upload the ZIP from `dist/` under **Admin → System → Plugins**. Enable **FilaBrain**, then open it from the FilaMan sidebar. Configure the model and optional search server in the plugin's settings. Open **Guide** from the FilaBrain page or chat for Ollama, OpenAI and Claude model setup, research workflows, and external MCP connections.

The standalone plugin page uses FilaMan's existing plugin loader. The initial compatibility target is FilaMan's `devel` revision `c8c76c4f`. FilaMan’s optional editor and selected-filament actions are part of the proposed host extensions in [FilaMan PR #204](https://github.com/Fire-Devils/filaman-system/pull/204).

The brain-and-reel mark is bundled as `filabrain/icon.svg`. Hosts with [FilaMan PR #205’s plugin icon support](https://github.com/Fire-Devils/filaman-system/pull/205) use it in the sidebar.

Rebuild the bundled page after editing the icon or files in `frontend/`.

```sh
npm ci
npm run build
python3 scripts/package_plugin.py
```

## Optional SearXNG search

Attach the search container to the Docker network used by FilaMan and enter `http://searxng:8080` as the search URL in the plugin.

```sh
FILAMAN_NETWORK=your-filaman-network docker compose -f compose.searxng.yml up -d
```

The search service stays on that Docker network and serves JSON results. When a public product page blocks access, FilaBrain can check a dated Wayback Machine capture if one is available. PDF extraction uses `pdftotext` when it is available on the FilaMan host.

## External MCP connections

Enable MCP in the plugin settings. Configure the MCP client for Streamable HTTP at `/api/v1/filabrain/mcp` on the FilaMan origin. Use a FilaMan API key as the bearer token. The key inherits FilaMan permissions. The plugin checks those permissions for each tool.

Remote connections require HTTPS. If an HTTPS reverse proxy terminates TLS for FilaMan, configure the proxy to preserve the public `Host` and overwrite `X-Forwarded-Proto` with the scheme it received. Set `FILABRAIN_MCP_TRUSTED_PROXY_IPS` in the FilaMan process or container environment to the exact source IP of that proxy as seen by FilaMan. Separate multiple proxy IPs with commas. Keep direct clients off the proxy addresses.

## Verify a package

Run the smoke test with the Python environment from a stock FilaMan checkout.

```sh
python scripts/smoke_install.py /path/to/filaman-system dist/filabrain-0.1.12.zip
```

The test installs the ZIP into a temporary plugin directory and uses an isolated SQLite database.

[MIT license](LICENSE.md)
