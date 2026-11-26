# Research Gap Finder (addon for your AI)

An addon for your AI chat app. It searches real literature (PubMed for medicine and dentistry, OpenAlex for other fields) and helps your AI suggest possible knowledge gaps for a doctoral thesis, backed by real references.

Works in any app that supports MCP addons, for example LM Studio and Claude Desktop.

## Setup

1. Install `uv`: open Terminal and run
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Open your AI app's MCP settings:
   - **LM Studio:** Program tab → Install → Edit mcp.json
   - **Claude Desktop:** Settings → Developer → Edit Config
3. Paste this, then fill in your email (and an OpenAlex key if you are not in medicine/dentistry; get one free at openalex.org):
   ```json
   {
     "mcpServers": {
       "researchgap-finder": {
         "command": "uvx",
         "args": ["--from", "git+https://github.com/aimeemitt/research-gap-finder", "researchgap-finder"],
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

## Use

Ask your AI, for example:

> Use find_thesis_gaps to find knowledge gaps for "sepsis biomarkers children" in Medicine.

## Note

Suggested gaps are starting points. Always verify them with your own search and your supervisor. Small local models give weaker results than large ones.

## License

MIT
