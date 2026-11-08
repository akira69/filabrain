# Filament Research for FilaMan

Filament Research is a FilaMan integration plugin maintained by `akira69`. It uses the FilaMan library, checks manufacturer and catalog sources, and presents proposals for review. It supports an OpenAI-compatible model server, including Ollama, and can expose its tools to a separate MCP client.

## Install

Build the plugin ZIP with Python 3.

```sh
python3 scripts/package_plugin.py
```

Upload the ZIP from `dist/` under **Admin → System → Plugins**. Enable **Filament Research**, then open it from the FilaMan sidebar. Configure the model and optional search server in the plugin's settings.

The standalone plugin page uses FilaMan's existing plugin loader. The initial compatibility target is FilaMan's `devel` revision `c8c76c4f`. The proposed `feat/plugin-filament-hooks` branch in the FilaMan fork adds optional native editor and selected-filament actions. Keep the prototype FilaMan branch and its data separate when trying the ZIP on a stock installation.

Rebuild the bundled page after editing files in `frontend/`.

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

The search service stays on that Docker network and serves JSON results. PDF extraction uses `pdftotext` when it is available on the FilaMan host.

## External MCP connections

Enable MCP in the plugin settings. Configure the MCP client for Streamable HTTP at `/api/v1/filament-research/mcp` on the FilaMan origin. Use a FilaMan API key as the bearer token. The key inherits FilaMan permissions. The plugin checks those permissions for each tool.

Remote connections require HTTPS. If an HTTPS reverse proxy terminates TLS for FilaMan, configure the proxy to preserve the public `Host` and overwrite `X-Forwarded-Proto` with the scheme it received. Set `FILAMENT_RESEARCH_MCP_TRUSTED_PROXY_IPS` in the FilaMan process or container environment to the exact source IP of that proxy as seen by FilaMan. Separate multiple proxy IPs with commas. Keep direct clients off the proxy addresses.

## Verify a package

Run the smoke test with the Python environment from a stock FilaMan checkout.

```sh
python scripts/smoke_install.py /path/to/filaman-system dist/filament_research-0.1.0.zip
```

The test installs the ZIP into a temporary plugin directory and uses an isolated SQLite database.

[MIT license](LICENSE.md)
