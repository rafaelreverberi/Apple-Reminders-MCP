# OpenAI Secure MCP Tunnel

OpenAI's [official Secure MCP Tunnel documentation](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
defines an outbound-only connection: `tunnel-client` polls OpenAI, forwards work
to the private loopback MCP, and returns responses. No public domain, inbound
firewall rule, `ngrok`, Cloudflare Tunnel, or model API key is needed.

## One-time OpenAI setup

1. In [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels),
   create/select a tunnel associated with the target Platform organization and
   ChatGPT workspace.
2. Create a runtime API key. The runtime principal needs **Tunnels Read + Use**;
   tunnel creation requires **Read + Manage**.
3. Set `CONTROL_PLANE_TUNNEL_ID` and `CONTROL_PLANE_API_KEY` in `.env`. This is
   not a normal `OPENAI_API_KEY` for model inference.
4. Install the latest ARM64 `tunnel-client` from the download link in Platform
   tunnel settings, or set `TUNNEL_CLIENT_BIN` to its absolute path.

`./start.sh` owns only the profile `apple-reminders-mcp` inside
`state/tunnel-profiles`, regenerates it from `.env`, runs `doctor --explain`, and
starts the tunnel with a loopback ephemeral health address.

Manual equivalent:

```bash
set -a; . ./.env; set +a
tunnel-client init --force \
  --sample sample_mcp_remote_no_auth \
  --profile apple-reminders-mcp \
  --profile-dir "$PWD/state/tunnel-profiles" \
  --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" \
  --mcp-server-url http://127.0.0.1:13003/mcp \
  --health-listen-addr 127.0.0.1:0
tunnel-client doctor --profile apple-reminders-mcp \
  --profile-dir "$PWD/state/tunnel-profiles" --explain
tunnel-client run --profile apple-reminders-mcp \
  --profile-dir "$PWD/state/tunnel-profiles"
```

In ChatGPT Settings → Plugins, create a developer-mode app, choose **Tunnel**,
select or paste the tunnel ID, then scan tools. Keep the runtime running. If it
does not appear, check workspace association, developer-mode access, and Tunnels
Read + Use. Re-run doctor and inspect `logs/tunnel.log`; never paste the key into
chat or diagnostics.

