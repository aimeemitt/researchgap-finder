"""Shared code for the Research Gap Finder web app (app.py) and AI addon (server.py)."""

import time
import xml.etree.ElementTree as ET

import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
OPENALEX = "https://api.openalex.org/works"
ICITE = "https://icite.od.nih.gov/api/pubs"
TOOL_NAME = "research-gap-finder"
MUST_READ_POOL = 200  # how many PubMed hits to check for citation counts
MUST_READ_COUNT = 10

# Field -> (who the student is, what the thesis is called, which source to use)
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


# ---------------- PubMed ----------------
def search_pubmed(term, max_results, email, ncbi_key=""):
    """Returns (articles, total hits, up to 200 PMIDs for the must-read list)."""
    params = {
        "db": "pubmed",
        "term": term,
        "retmax": max(MUST_READ_POOL, max_results),
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
    all_pmids, total = data.get("idlist", []), int(data.get("count", 0))
    if not all_pmids:
        return [], 0, []
    pmids = all_pmids[:max_results]

    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml", "tool": TOOL_NAME, "email": email}
    if ncbi_key:
        params["api_key"] = ncbi_key
    time.sleep(0.4)  # stay under 3 requests/second
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
    return articles, total, all_pmids


def pubmed_must_read(pmids):
    r = requests.get(
        ICITE,
        params={"pmids": ",".join(pmids), "fl": "pmid,title,year,citation_count"},
        timeout=60,
    )
    r.raise_for_status()
    papers = sorted(r.json().get("data", []), key=lambda p: p.get("citation_count") or 0, reverse=True)
    return [
        {
            "id": f"PMID {p['pmid']}",
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/",
            "title": p.get("title") or "",
            "year": str(p.get("year") or ""),
            "citations": p.get("citation_count") or 0,
        }
        for p in papers[:MUST_READ_COUNT]
    ]


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


def openalex_must_read(term, openalex_key):
    params = {
        "search": term,
        "per_page": MUST_READ_COUNT,
        "sort": "cited_by_count:desc",
        "select": "id,doi,title,publication_year,cited_by_count",
        "api_key": openalex_key,
    }
    r = requests.get(OPENALEX, params=params, timeout=60)
    r.raise_for_status()
    return [
        {
            "id": f"OpenAlex {(w.get('id') or '').rsplit('/', 1)[-1]}",
            "url": w.get("doi") or w.get("id"),
            "title": w.get("title") or "",
            "year": str(w.get("publication_year") or ""),
            "citations": w.get("cited_by_count") or 0,
        }
        for w in r.json().get("results", [])
    ]


# ---------------- One call for everything ----------------
def get_literature(term, field, max_results, email="", ncbi_key="", openalex_key=""):
    """Returns (articles, total hits, must-read list, source name)."""
    source = FIELDS[field][2]
    if source == "PubMed":
        articles, total, all_pmids = search_pubmed(term, max_results, email, ncbi_key)
        try:
            must_read = pubmed_must_read(all_pmids) if all_pmids else []
        except requests.RequestException:
            must_read = []
    else:
        articles, total = search_openalex(term, max_results, openalex_key)
        try:
            must_read = openalex_must_read(term, openalex_key)
        except requests.RequestException:
            must_read = []
    return articles, total, must_read, source


# ---------------- Text pieces ----------------
def must_read_markdown(must_read):
    return "\n".join(
        f"{i}. **{p['title']}** ({p['year']}) · {p['citations']} citations · [{p['id']}]({p['url']})"
        for i, p in enumerate(must_read, 1)
    ) or "No citation data available."


def analyzed_markdown(articles):
    return "\n".join(f"- {a['year']} – {a['title']} · [{a['id']}]({a['url']})" for a in articles)


def prompt_intro(term, articles, field):
    student, thesis, _ = FIELDS[field]
    corpus = "\n\n".join(
        f"{a['id']} ({a['year']}; {', '.join(a['types'])})\n"
        f"Title: {a['title']}\nAbstract: {a['abstract'] or '[no abstract]'}"
        for a in articles
    )
    return f"""You are helping a {student} find a {thesis} topic in the field of {field}.

Search term: "{term}"
Below are {len(articles)} of the most relevant publications.

{corpus}"""


def prompt_sections(articles):
    return f"""## 1. Related keywords
10-15 related keywords or subtopics. For each: the term in bold, then one short plain-language explanation.

## 2. Overview of the field
3-5 sentences: what is well studied and what kinds of studies or publications dominate.

## 3. Possible knowledge gaps
3-5 candidate gaps. For each:
- **Gap:** one sentence
- **Why it looks like a gap:** evidence from the publications
- **Supporting references:** only IDs from the list above (e.g. "{articles[0]['id']}")
- **Possible thesis question:** one concrete research question
- **Feasibility for a doctoral thesis:** low / medium / high, with one reason

## 4. Suggested next searches
3-5 search strings to check whether each gap is real."""


RULES = "Rules: cite only IDs listed above. Never invent references. If the publications are not enough to judge something, say so."


def build_prompt(term, articles, field):
    """Prompt for the web app."""
    return f"""{prompt_intro(term, articles, field)}

Answer in Markdown with exactly these sections:

{prompt_sections(articles)}

{RULES}"""
