# review_manager_mon

## Supabase MCP

This project is configured to use the hosted Supabase MCP server.

Project ref:

```text
xhjjoxzwpgqlodflaiix
```

### Codex

Codex MCP configuration is stored in your global Codex config, not inside the
repository. This machine is configured as:

```toml
[mcp_servers.supabase]
url = "https://mcp.supabase.com/mcp?project_ref=xhjjoxzwpgqlodflaiix"
bearer_token_env_var = "SUPABASE_ACCESS_TOKEN"
```

Set a Supabase access token in your shell before starting Codex:

```sh
export SUPABASE_ACCESS_TOKEN="your-supabase-access-token"
```

Then restart Codex so the MCP server is loaded for the session.

### Cursor

Cursor can use the project-local MCP config at `.cursor/mcp.json`.

Supabase's hosted MCP server can also authenticate through browser OAuth in
clients that support it. For clients that do not, use a Supabase personal access
token via environment variables.
