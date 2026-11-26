# Research Gap Finder (addon for your AI)

An addon for your AI chat app. It searches real literature (PubMed for medicine and dentistry, OpenAlex for other fields) and helps your AI suggest possible knowledge gaps for a doctoral thesis, backed by real references.

**Addon address:**

```
https://researchgap-finder.onrender.com/mcp
```

## Before you start

- **Medicine or dentistry:** you only need your email address.
- **All other fields:** you also need a free OpenAlex key. Go to [openalex.org](https://openalex.org), sign up, open Settings and copy your API key.

## Add it to Claude

Works on the free Claude plan.

1. Open [claude.ai](https://claude.ai) → **Customize** → **Connectors**.
2. Click **Add** → **Add custom connector**.
3. Name: `Research Gap Finder`. URL: the addon address above. Click **Add**.
4. Click **Connect**. A page opens: enter your email (and your OpenAlex key if you have one). Click **Connect**.

## Add it to ChatGPT

1. Open [chatgpt.com/plugins](https://chatgpt.com/plugins) in your browser (not the phone app).
2. Click **+** → **Add custom MCP server**.
   If you don't see this, turn on Developer mode first: Settings → Apps → Advanced settings → **Developer mode**.
3. Name: `Research Gap Finder`. Server URL: the addon address above. Authentication: **OAuth**.
4. Confirm the warning, click **Create**, then install it.
5. A page opens: enter your email (and your OpenAlex key if you have one). Click **Connect**.

## Use

Ask your AI, for example:

> Use Research Gap Finder to find knowledge gaps for "sepsis biomarkers children" in Medicine.

The first search after a quiet period can take about a minute while the addon wakes up. If it fails, just ask again.

## Your data

Your email and key are not stored on the addon's server. They are kept encrypted inside your AI app's connection. To change them, remove the addon in your AI app and add it again.

## Note

Suggested gaps are starting points. Always verify them with your own search and your supervisor.

<details>
<summary>Advanced: run the addon on your own computer (LM Studio, Claude Desktop)</summary>

1. Install `uv`: open Terminal and run
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Open your AI app's MCP settings:
   - **LM Studio:** Program tab → Install → Edit mcp.json
   - **Claude Desktop:** Settings → Developer → Edit Config
3. Paste this, then fill in your email (and an OpenAlex key if you are not in medicine/dentistry):
   ```json
   {
     "mcpServers": {
       "researchgap-finder": {
         "command": "uvx",
         "args": ["--from", "git+https://github.com/aimeemitt/researchgap-finder", "researchgap-finder"],
         "env": {
           "NCBI_EMAIL": "you@example.com",
           "OPENALEX_API_KEY": ""
         }
       }
     }
   }
   ```
   On a Mac, if the app can't find `uvx`, replace `"uvx"` with the full path: run `which uvx` in Terminal and paste the result.
4. Save and restart the app.

Small local models give weaker results than large ones.

</details>

## License

MIT
