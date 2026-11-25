import os
import time
import xml.etree.ElementTree as ET

import requests
from mcp.server.mcpserver import MCPServer

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
OPENALEX = "https://api.openalex.org/works"
TOOL_NAME = "research-gap-finder"

FIELDS = {
    "Medicine": ("medical student", "doctoral thesis (Doktorarbeit)", "PubMed"),
    "Dentistry": ("dentistry student", "doctoral thesis (Doktorarbeit)", "PubMed"),
    "Law": ("law student", "doctoral thesis (Dissertation)", "OpenAlex"),
    "Psychology": ("psychology student", "doctoral thesis (Dissertation)", "OpenAlex"),
    "Economics / Business": ("economics student", "doctoral thesis (Dissertation)", "OpenAlex"),
    "Natural sciences": ("science student", "doctoral thesis (Dissertation)", "OpenAlex"),
    "Engineering / Computer science": ("engineering student", "doctoral thesis (Dissertation)", "OpenAlex"),
    "Social sciences / Humanities": ("humanities student", "doctoral thesis (Dissertation)", "OpenAlex"),
}

mcp = MCPServer("Research Gap Finder")


# ---------------- PubMed ----------------
def search_pubmed(term, max_results, email, ncbi_key):
    params = {
        "db": "pubmed",
        "term": term,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance",
        "tool": TOOL_NAME,
        "email": email,
    }
    if ncbi_key:
        params["api_key"] = ncbi_key
    r = requests.get(EUTILS + "esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()["esearchresult"]
    pmids, total = data.get("idlist", []), int(data.get("count", 0))
    if not pmids:
        return [], 0

    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml", "tool": TOOL_NAME, "email": email}
    if ncbi_key:
        params["api_key"] = ncbi_key
    time.sleep(0.4)
    r = requests.get(EUTILS + "efetch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    articles = []
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//PMID", default="")
        title_el = art.find(".//ArticleTitle")
        title = "".join(title_el.itertext()) if title_el is not None else ""
        parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            text = "".join(ab.itertext()).strip()
            parts.append(f"{label}: {text}" if label else text)
        year = art.findtext(".//PubDate/Year") or art.findtext(".//PubDate/MedlineDate", default="")[:4]
        types = [pt.text for pt in art.findall(".//PublicationType") if pt.text]
        articles.append(
            {
                "id": f"PMID {pmid}",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "title": title,
                "abstract": " ".join(parts),
                "year": year,
                "types": types,
            }
        )
    return articles, total


# ---------------- OpenAlex ----------------
def rebuild_abstract(inverted_index):
    if not inverted_index:
        return ""
    positions = [(i, word) for word, idxs in inverted_index.items() for i in idxs]
    return " ".join(word for _, word in sorted(positions))


def search_openalex(term, max_results, openalex_key):
    params = {
        "search": term,
        "per_page": max_results,
        "select": "id,doi,title,publication_year,type,abstract_inverted_index",
        "api_key": openalex_key,
    }
    r = requests.get(OPENALEX, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    total = data.get("meta", {}).get("count", 0)
    articles = []
    for w in data.get("results", []):
        short_id = (w.get("id") or "").rsplit("/", 1)[-1]
        articles.append(
            {
                "id": f"OpenAlex {short_id}",
                "url": w.get("doi") or w.get("id"),
                "title": w.get("title") or "",
                "abstract": rebuild_abstract(w.get("abstract_inverted_index")),
                "year": str(w.get("publication_year") or ""),
                "types": [w.get("type")] if w.get("type") else [],
            }
        )
    return articles, total


# ---------------- Instructions for the user's AI ----------------
def instructions(term, articles, total, field, source):
    student, thesis, _ = FIELDS[field]
    corpus = "\n\n".join(
        f"{a['id']} ({a['year']}; {', '.join(a['types'])})\nLink: {a['url']}\n"
        f"Title: {a['title']}\nAbstract: {a['abstract'] or '[no abstract]'}"
        for a in articles
    )
    return f"""SEARCH COMPLETE. Do NOT call find_thesis_gaps again. Use the publications below to write your final answer now.

You are helping a {student} find a {thesis} topic in the field of {field}.

Search term: "{term}"
Total {source} hits for this term: {total}
Below are {len(articles)} of the most relevant publications.

{corpus}

Answer in Markdown with exactly these sections:

## 1. Related keywords
10-15 related keywords or subtopics. For each: the term in bold, then one short plain-language explanation.

## 2. Overview of the field
3-5 sentences: what is well studied and what kinds of studies or publications dominate.

## 3. Possible knowledge gaps
3-5 candidate gaps. For each:
- **Gap:** one sentence
- **Why it looks like a gap:** evidence from the publications
- **Supporting references:** only IDs from the list above, with their links
- **Possible thesis question:** one concrete research question
- **Feasibility for a doctoral thesis:** low / medium / high, with one reason

## 4. Suggested next searches
3-5 search strings to check whether each gap is real.

Rules: cite only IDs listed above. Never invent references. If the publications are not enough to judge something, say so.
End with: "These gaps are starting points. Verify them with your own search and your supervisor."

REMINDER: The search is already done. Do NOT call any tool again. Write the answer now."""


# ---------------- The tool the AI can use ----------------
@mcp.tool()
def find_thesis_gaps(keyword: str, field: str = "Medicine", max_results: int = 15) -> str:
    """Search real literature for a keyword and return publications plus instructions
    to suggest doctoral thesis knowledge gaps.

    field must be one of: Medicine, Dentistry, Law, Psychology, Economics / Business,
    Natural sciences, Engineering / Computer science, Social sciences / Humanities.
    Medicine and Dentistry use PubMed; all other fields use OpenAlex.
    """
    if field not in FIELDS:
        return f"Unknown field '{field}'. Use one of: {', '.join(FIELDS)}."
    max_results = max(5, min(int(max_results), 100))
    source = FIELDS[field][2]
    try:
        if source == "PubMed":
            email = os.getenv("NCBI_EMAIL", "")
            if not email:
                return "Setup missing: add NCBI_EMAIL to this addon's settings."
            articles, total = search_pubmed(keyword, max_results, email, os.getenv("NCBI_API_KEY", ""))
        else:
            key = os.getenv("OPENALEX_API_KEY", "")
            if not key:
                return "Setup missing: add OPENALEX_API_KEY to this addon's settings."
            articles, total = search_openalex(keyword, max_results, key)
    except requests.RequestException as e:
        return f"{source} error: {e}"
    if not articles:
        return f"No {source} results for '{keyword}'. Try a broader keyword."
    return instructions(keyword, articles, total, field, source)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
