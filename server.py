"""Research Gap Finder as an MCP addon for local AI apps (LM Studio, Claude Desktop, ...)."""

import os

import requests
from mcp.server.mcpserver import MCPServer

from core import (
    FIELDS,
    RULES,
    analyzed_markdown,
    get_literature,
    must_read_markdown,
    prompt_intro,
    prompt_sections,
)

mcp = MCPServer("Research Gap Finder")


def instructions(term, articles, total, field, source, must_read):
    return f"""SEARCH COMPLETE. Do NOT call find_thesis_gaps again. Use the publications below to write your final answer now.

{prompt_intro(term, articles, field)}

Write your answer in Markdown in EXACTLY this layout. Do not add anything before or after it.

# 🔬 Research Gap Finder
*Prototype. Suggested gaps are starting points – always verify them with your own search and your supervisor.*

> {source} hits in total: {total} · analyzing {len(articles)} publications

{prompt_sections(articles)}

## 5. Must-read papers (most cited)
Copy this list exactly as it is:
{must_read_markdown(must_read)}

---
## Analyzed publications
Copy this list exactly as it is:
{analyzed_markdown(articles)}

{RULES}

REMINDER: The search is already done. Do NOT call any tool again. Write the answer now."""


@mcp.tool()
def find_thesis_gaps(keyword: str, field: str = "Medicine", max_results: int = 15) -> str:
    """Search real literature for a keyword and return publications plus instructions
    to suggest doctoral thesis knowledge gaps, plus a must-read list of the most cited papers.

    field must be one of: Medicine, Dentistry, Law, Psychology, Economics / Business,
    Natural sciences, Engineering / Computer science, Social sciences / Humanities.
    Medicine and Dentistry use PubMed; all other fields use OpenAlex.
    """
    if field not in FIELDS:
        return f"Unknown field '{field}'. Use one of: {', '.join(FIELDS)}."
    max_results = max(5, min(int(max_results), 100))
    source = FIELDS[field][2]
    email = os.getenv("NCBI_EMAIL", "")
    openalex_key = os.getenv("OPENALEX_API_KEY", "")
    if source == "PubMed" and not email:
        return "Setup missing: add NCBI_EMAIL to this addon's settings."
    if source == "OpenAlex" and not openalex_key:
        return "Setup missing: add OPENALEX_API_KEY to this addon's settings."
    try:
        articles, total, must_read, source = get_literature(
            keyword, field, max_results, email, os.getenv("NCBI_API_KEY", ""), openalex_key
        )
    except requests.RequestException as e:
        return f"{source} error: {e}"
    if not articles:
        return f"No {source} results for '{keyword}'. Try a broader keyword."
    return instructions(keyword, articles, total, field, source, must_read)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
