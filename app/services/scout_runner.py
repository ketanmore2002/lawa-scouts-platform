"""
LAWA Scouts — Multi-Agent Scout Runner
=======================================
Orchestrates parallel subagents, browser automation fallback,
SerpAPI product search, and E2B sandbox analysis.
"""

import asyncio
import json
import math
import re
import logging
from datetime import datetime, timedelta

from openai import OpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.config import get_settings
from app.models import Scout, Report

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────

_ERROR_SIGNALS = [
    "error:", "could not", "unable to", "cannot access", "not retrieved",
    "not available", "failed to", "no results", "access denied", "blocked",
]

_ECOMMERCE_PLATFORMS = [
    "amazon", "flipkart", "ebay", "walmart", "aliexpress",
    "myntra", "meesho", "etsy", "target", "bestbuy",
]

_PRODUCT_SIGNALS = [
    "buy", "price", "cheapest", "product", "shop", "deal", "offer",
    "discount", "cost", "purchase", "order", "delivery", "shopping",
]

_CHART_SIGNALS = re.compile(
    r"plot|graph|chart|visuali[sz]|diagram|trend|track|"
    r"show\s*me\s*(the\s*)?(plot|graph|chart|trend|data|number|stat)|"
    r"compare.*(data|number|stat|price|growth)|"
    r"analytics|analysis|performance\s+over|growth\s+of",
    re.IGNORECASE,
)

_FORMAT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bpdf\b", re.I), "pdf"),
    (re.compile(r"\b(excel|xlsx|spreadsheet)\b", re.I), "excel"),
    (re.compile(r"\b(html)\b", re.I), "html"),
    (re.compile(r"\b(pptx|powerpoint|presentation|slides)\b", re.I), "pptx"),
    (re.compile(r"\b(txt|text\s*file|plain\s*text)\b", re.I), "txt"),
    (re.compile(r"\bcsv\b", re.I), "csv"),
]

_JSON_SCHEMA = """{
  "title": "Descriptive report title",
  "summary": "2-3 sentence executive summary",
  "stats": [{"label": "Total Found", "value": "12", "color": "default"}],
  "columns": [
    {"key": "name", "label": "Name", "type": "text"},
    {"key": "category", "label": "Category", "type": "badge"},
    {"key": "tags", "label": "Tags", "type": "tags"},
    {"key": "link", "label": "Link", "type": "link"}
  ],
  "rows": [{"name": "Item", "category": "Type A", "tags": ["a"], "link": "https://..."}],
  "insights": ["Key insight 1"],
  "filter_columns": ["category"]
}"""

_COLUMN_GUIDE = """Column types: "text", "badge" (categorical), "tags" (array), "date", "link" (action button).
Rules: 5-9 columns, aim for 5-20 rows, include "link" column with real URLs, add "link_label" to each row.
filter_columns: badge-type column keys for dropdown filters.
stats: 3-5 metrics, colors: "default" (teal), "green", "red", "blue".
Return the raw JSON object only — no markdown fences."""


# ────────────────────────────────────────────────────────
# Shared Helpers
# ────────────────────────────────────────────────────────

def _get_openai_client() -> OpenAI:
    return OpenAI(api_key=get_settings().openai_api_key)


def _scout_text(scout: Scout) -> str:
    return f"{scout.topic} {scout.keywords or ''} {scout.description or ''}".lower()


def _build_source_instructions(scout: Scout) -> str:
    """Build prompt instructions from include/exclude source lists."""
    parts = []
    if scout.include_sources:
        sources = [s.strip() for s in scout.include_sources.splitlines() if s.strip()]
        if sources:
            parts.append(
                "IMPORTANT — Focus your search on these specific sources/domains:\n"
                + "\n".join(f"  - {s}" for s in sources)
                + "\nPrioritize results from these sources above all others."
            )
    if scout.exclude_sources:
        sources = [s.strip() for s in scout.exclude_sources.splitlines() if s.strip()]
        if sources:
            parts.append(
                "IMPORTANT — Do NOT include results from these sources/domains:\n"
                + "\n".join(f"  - {s}" for s in sources)
                + "\nExclude any results originating from these domains."
            )
    return "\n\n".join(parts)


def _extract_json(text: str) -> dict | None:
    for attempt in [
        lambda: json.loads(text.strip()),
        lambda: json.loads(re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL).group(1).strip()),
        lambda: json.loads(text[text.find("{"):text.rfind("}") + 1]),
    ]:
        try:
            return attempt()
        except Exception:
            continue
    return None


def _extract_citations(response) -> list[dict]:
    citations, seen = [], set()
    for item in response.output:
        if hasattr(item, "content"):
            for block in item.content:
                if hasattr(block, "annotations"):
                    for ann in block.annotations:
                        if ann.type == "url_citation":
                            url = getattr(ann, "url", "")
                            if url and url not in seen:
                                seen.add(url)
                                citations.append({"title": getattr(ann, "title", ""), "url": url})
    return citations


def _citations_from_rows(columns: list[dict], rows: list[dict]) -> list[dict]:
    citations, seen = [], set()
    link_keys = [c["key"] for c in columns if c.get("type") == "link"]
    for row in rows:
        for lk in link_keys:
            url = row.get(lk)
            if url and url.startswith("http") and url not in seen:
                seen.add(url)
                name = row.get("name") or row.get("product") or row.get("company") or ""
                citations.append({"title": name, "url": url})
    return citations


def _is_error_response(text: str) -> bool:
    parsed = _extract_json(text)
    if parsed and isinstance(parsed.get("rows"), list) and len(parsed["rows"]) > 0:
        return False
    lower = text.lower().strip()
    if any(sig in lower for sig in _ERROR_SIGNALS):
        return True
    if parsed and isinstance(parsed.get("rows"), list) and len(parsed["rows"]) == 0:
        return True
    return False


def _parse_report_sections(text: str) -> dict:
    title, summary = "Web Research Report", ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("**Title:**"):
            title = s.replace("**Title:**", "").strip()
        elif s.startswith("**Summary:**"):
            summary = s.replace("**Summary:**", "").strip()
            for j in range(i + 1, len(lines)):
                nxt = lines[j].strip()
                if nxt.startswith("**"):
                    break
                if nxt:
                    summary += " " + nxt
    return {"title": title, "summary": summary.strip(), "full_text": text}


# ────────────────────────────────────────────────────────
# Strategy Detection
# ────────────────────────────────────────────────────────

def _detect_search_strategy(scout: Scout) -> str:
    if not get_settings().serpapi_key:
        return "openai"
    text = _scout_text(scout)
    if any(p in text for p in _ECOMMERCE_PLATFORMS):
        return "serpapi_google_shopping"
    if sum(1 for s in _PRODUCT_SIGNALS if s in text) >= 2:
        return "serpapi_google_shopping"
    return "openai"


def _wants_charts(scout: Scout) -> bool:
    """Check if the user's query explicitly asks for charts/plots/visualization."""
    text = _scout_text(scout)
    return bool(_CHART_SIGNALS.search(text))


def _detect_requested_formats(scout: Scout) -> list[str]:
    """Detect output formats the user requested in their query.
    Only returns explicitly mentioned formats; returns empty list when none specified.
    """
    text = _scout_text(scout)
    formats = []
    for pattern, fmt in _FORMAT_PATTERNS:
        if pattern.search(text):
            formats.append(fmt)
    return formats


def _detect_sort(text: str) -> str | None:
    text = text.lower()
    if any(kw in text for kw in ["descending", "high to low", "expensive first", "highest price"]):
        return "price-desc-rank"
    if any(kw in text for kw in ["ascending", "low to high", "cheapest", "lowest price"]):
        return "price-asc-rank"
    if any(kw in text for kw in ["best rated", "top rated", "highest rating"]):
        return "review-rank"
    if any(kw in text for kw in ["newest", "latest", "most recent"]):
        return "date-desc-rank"
    return None


def _detect_country_code(text: str) -> str:
    mapping = [
        (["india", ".in"], "in"), (["uk", "britain"], "uk"), (["germany"], "de"),
        (["france"], "fr"), (["japan"], "jp"), (["canada"], "ca"),
        (["australia"], "au"), (["uae", "dubai", "emirates"], "ae"),
    ]
    text = text.lower()
    for kws, code in mapping:
        if any(kw in text for kw in kws):
            return code
    return "us"


def _detect_currency(text: str) -> str:
    mapping = [
        (["india", "amazon.in", "inr"], "\u20b9"),
        (["uk", "gbp"], "\u00a3"), (["europe", "eur"], "\u20ac"),
        (["japan", "jpy"], "\u00a5"), (["uae", "aed", "dirham"], "AED "),
    ]
    text = text.lower()
    for kws, sym in mapping:
        if any(kw in text for kw in kws):
            return sym
    return "$"


# ────────────────────────────────────────────────────────
# SerpAPI Search
# ────────────────────────────────────────────────────────

def _search_serpapi_google_shopping(scout: Scout) -> list[dict]:
    from serpapi import GoogleSearch

    text = _scout_text(scout)
    gl = _detect_country_code(text)
    query = scout.keywords or scout.topic
    for platform in _ECOMMERCE_PLATFORMS:
        if platform in text and platform not in query.lower():
            query = f"{query} {platform}"
            break

    # Apply source filters to search query
    if scout.include_sources:
        sites = [s.strip() for s in scout.include_sources.splitlines() if s.strip()]
        if sites:
            query += " " + " OR ".join(f"site:{s}" for s in sites[:5])
    if scout.exclude_sources:
        sites = [s.strip() for s in scout.exclude_sources.splitlines() if s.strip()]
        for s in sites[:5]:
            query += f" -site:{s}"

    params = {
        "engine": "google_shopping", "q": query, "gl": gl, "hl": "en",
        "api_key": get_settings().serpapi_key,
    }

    logger.info(f"SerpAPI Google Shopping: q='{query}', gl={gl}")
    results = GoogleSearch(params).get_dict()

    products = []
    for item in results.get("shopping_results", []):
        price_str = str(item.get("price", "N/A"))
        price_val = 0.0
        if item.get("extracted_price"):
            price_val = float(item["extracted_price"])
        else:
            try:
                price_val = float(re.sub(r"[^\d.]", "", price_str))
            except ValueError:
                pass

        products.append({
            "name": item.get("title", "Unknown Product"),
            "price": price_str, "price_value": price_val,
            "rating": item.get("rating"), "reviews": item.get("reviews"),
            "source": item.get("source", ""),
            "link": item.get("link") or item.get("product_link", ""),
        })

    sort_pref = _detect_sort(text)
    if sort_pref and products:
        reverse = sort_pref in ("price-desc-rank", "date-desc-rank")
        if "price" in sort_pref:
            products.sort(key=lambda p: p.get("price_value", 0), reverse=reverse)
        elif "review" in sort_pref:
            products.sort(key=lambda p: float(p.get("rating") or 0), reverse=True)

    logger.info(f"SerpAPI Google Shopping: {len(products)} products, sort={sort_pref}")
    return products


# ────────────────────────────────────────────────────────
# Build Product Report from SerpAPI Data
# ────────────────────────────────────────────────────────

def _build_product_report(scout: Scout, products: list[dict], engine: str) -> tuple[str, str, dict]:
    text = _scout_text(scout)
    currency = _detect_currency(text)

    columns = [
        {"key": "name", "label": "Product", "type": "text"},
        {"key": "price", "label": "Price", "type": "text"},
    ]
    if any(p.get("rating") for p in products):
        columns.append({"key": "rating_display", "label": "Rating", "type": "text"})
    if any(p.get("source") for p in products):
        columns.append({"key": "source", "label": "Source", "type": "badge"})
    columns.append({"key": "link", "label": "Link", "type": "link"})

    rows = []
    for p in products:
        row = {"name": p["name"], "price": p["price"], "link": p.get("link", ""), "link_label": "Buy"}
        if any(p2.get("rating") for p2 in products):
            r = p.get("rating")
            rv = p.get("reviews")
            if r:
                stars = "\u2605" * int(float(r)) + "\u2606" * (5 - int(float(r)))
                row["rating_display"] = f"{stars} {r}" + (f" ({rv:,})" if rv else "")
            else:
                row["rating_display"] = "\u2014"
        if p.get("source"):
            row["source"] = p["source"]
        rows.append(row)

    prices = [p["price_value"] for p in products if p.get("price_value")]
    ratings = [float(p["rating"]) for p in products if p.get("rating")]
    sources = set(p.get("source", "") for p in products if p.get("source"))

    stats = [{"label": "Products Found", "value": str(len(products)), "color": "default"}]
    if prices:
        stats.append({"label": "Highest Price", "value": f"{currency}{max(prices):,.0f}", "color": "blue"})
        stats.append({"label": "Lowest Price", "value": f"{currency}{min(prices):,.0f}", "color": "green"})
    if ratings:
        stats.append({"label": "Avg Rating", "value": f"{sum(ratings) / len(ratings):.1f} / 5", "color": "default"})

    filter_columns = ["source"] if any(p.get("source") for p in products) else []

    insights = []
    if prices:
        avg = sum(prices) / len(prices)
        insights.append(f"Prices range from {currency}{min(prices):,.0f} to {currency}{max(prices):,.0f}, average {currency}{avg:,.0f}.")
    if ratings:
        best = max(products, key=lambda p: float(p.get("rating") or 0))
        insights.append(f"Highest rated: \"{best['name'][:70]}\" at {best['rating']} stars.")
    if sources:
        top_sources = sorted(sources, key=lambda s: sum(1 for p in products if p.get("source") == s), reverse=True)[:3]
        insights.append(f"Top sources: {', '.join(top_sources)}.")

    title = f"{scout.topic} \u2014 Product Search Results"
    summary = f"Found {len(products)} products across {len(sources)} sources."
    if prices:
        summary += f" Prices range from {currency}{min(prices):,.0f} to {currency}{max(prices):,.0f}."

    structured_data = {
        "title": title, "summary": summary, "stats": stats,
        "columns": columns, "rows": rows,
        "insights": insights, "filter_columns": filter_columns,
    }

    citations = [{"title": p["name"], "url": p["link"]}
                 for p in products if p.get("link")]

    findings = {
        "citations": citations, "format": "structured",
        "structured_data": structured_data,
        "full_text": json.dumps(structured_data, indent=2),
    }
    return title, summary, findings


def _enrich_with_gpt(scout: Scout, products: list[dict], findings: dict) -> dict:
    try:
        client = _get_openai_client()
        compact = [{"name": p["name"][:80], "price": p["price"], "rating": p.get("rating")}
                    for p in products[:20]]
        resp = client.responses.create(
            model="gpt-5.4-mini-2026-03-17",
            input=f"""Given these product search results for "{scout.topic}":
{json.dumps(compact)}

Generate JSON with:
- "summary": 2-3 sentence buyer-oriented analysis
- "insights": array of 3-5 specific, actionable buying insights

Return ONLY the JSON object.""",
        )
        enrichment = _extract_json(resp.output_text)
        if enrichment:
            sd = findings["structured_data"]
            if enrichment.get("summary"):
                sd["summary"] = enrichment["summary"]
            if enrichment.get("insights"):
                sd["insights"] = enrichment["insights"]
            logger.info("GPT enrichment applied")
    except Exception as e:
        logger.warning(f"GPT enrichment failed (non-fatal): {e}")
    return findings


# ────────────────────────────────────────────────────────
# Parallel Subagent Orchestration
# ────────────────────────────────────────────────────────

def _generate_subtasks(scout: Scout) -> list[str]:
    """Use GPT-4o-mini to break the scout topic into parallel search subtasks."""
    client = _get_openai_client()
    kw = f", keywords: {scout.keywords}" if scout.keywords else ""
    desc = f", context: {scout.description}" if scout.description else ""
    source_instr = _build_source_instructions(scout)
    source_block = f"\n\n{source_instr}" if source_instr else ""

    resp = client.responses.create(
        model="gpt-5.4-mini-2026-03-17",
        input=f"""Break this research query into 3-5 parallel search subtasks that different agents can execute simultaneously.

Topic: {scout.topic}{kw}{desc}{source_block}

Each subtask should search from a DIFFERENT angle or source type:
- Different source categories (news sites, company pages, academic, forums, job boards, government, etc.)
- Different aspects of the topic (facets, time periods, geographic regions)
- Each subtask should be a complete, self-contained search instruction

Return a JSON array of strings, each being a specific search instruction.
Return ONLY the JSON array.""",
    )

    try:
        subtasks = json.loads(resp.output_text.strip())
        if isinstance(subtasks, list) and len(subtasks) >= 2:
            logger.info(f"Generated {len(subtasks)} subtasks")
            return subtasks[:5]
    except Exception:
        pass

    topic = scout.topic
    return [
        f"Search for the latest news and updates about: {topic}",
        f"Search for in-depth analysis, reports, and expert opinions on: {topic}",
        f"Search for practical resources, tools, and direct links related to: {topic}",
    ]


def _run_single_subagent(subtask: str, scout: Scout) -> dict:
    """Execute a single search subagent (runs in thread pool)."""
    client = _get_openai_client()
    source_instr = _build_source_instructions(scout)
    source_block = f"\n\n{source_instr}" if source_instr else ""

    prompt = f"""{subtask}{source_block}

Compile your findings as a JSON data table. Focus on RECALL — find as many relevant items as possible.
Include real URLs as links.

Return ONLY valid JSON:
{_JSON_SCHEMA}

{_COLUMN_GUIDE}"""

    try:
        response = client.responses.create(
            model="gpt-5.4-mini-2026-03-17",
            tools=[{"type": "web_search"}],
            input=prompt,
        )

        output_text = response.output_text
        citations = _extract_citations(response)

        parsed = _extract_json(output_text)
        if parsed and isinstance(parsed.get("rows"), list):
            if not citations:
                citations = _citations_from_rows(parsed.get("columns", []), parsed["rows"])
            return {
                "success": True, "structured": parsed,
                "citations": citations, "raw": output_text, "subtask": subtask,
            }
        else:
            return {
                "success": True, "structured": None,
                "citations": citations, "raw": output_text, "subtask": subtask,
            }
    except Exception as e:
        logger.error(f"Subagent failed: {e}")
        return {"success": False, "structured": None, "citations": [], "raw": str(e), "subtask": subtask}


def _merge_subagent_results(scout: Scout, results: list[dict]) -> tuple[str, str, dict]:
    """Merge results from multiple subagents into a unified structured report."""
    all_rows, all_citations, all_columns, all_insights, all_raw = [], [], [], [], []
    seen_urls, column_set = set(), set()

    for r in results:
        if not r.get("success"):
            continue

        for c in r.get("citations", []):
            if c["url"] not in seen_urls:
                seen_urls.add(c["url"])
                all_citations.append(c)

        sd = r.get("structured")
        if sd:
            for col in sd.get("columns", []):
                if col["key"] not in column_set:
                    column_set.add(col["key"])
                    all_columns.append(col)
            all_rows.extend(sd.get("rows", []))
            all_insights.extend(sd.get("insights", []))

        if r.get("raw"):
            all_raw.append(r["raw"])

    if not all_rows:
        combined_text = "\n\n---\n\n".join(all_raw)
        sections = _parse_report_sections(combined_text)
        return sections["title"], sections["summary"] or combined_text[:500], {
            "citations": all_citations, "format": "text",
            "structured_data": None, "full_text": combined_text,
        }

    # Deduplicate rows by name
    seen_names = set()
    unique_rows = []
    name_key = next((c["key"] for c in all_columns if c.get("type") == "text"), "name")
    for row in all_rows:
        name = str(row.get(name_key, "")).strip().lower()
        if name and name not in seen_names:
            seen_names.add(name)
            unique_rows.append(row)
        elif not name:
            unique_rows.append(row)

    unique_insights = list(dict.fromkeys(all_insights))[:6]

    # Synthesize title and summary
    client = _get_openai_client()
    try:
        synth = client.responses.create(
            model="gpt-5.4-mini-2026-03-17",
            input=f"""Given {len(unique_rows)} research results about "{scout.topic}" from {len(results)} parallel searches:

Sample items: {json.dumps([{name_key: r.get(name_key, '')} for r in unique_rows[:10]])}

Generate JSON with:
- "title": concise descriptive title (include topic)
- "summary": 2-3 sentence executive summary

Return ONLY JSON.""",
        )
        meta = _extract_json(synth.output_text)
        if not isinstance(meta, dict):
            meta = None
    except Exception:
        meta = None

    title = (meta or {}).get("title", f"{scout.topic} \u2014 Research Report")
    summary = (meta or {}).get("summary", f"Found {len(unique_rows)} results across {len(results)} search agents.")

    # Generate comprehensive analysis for export artifacts
    analysis = ""
    try:
        # Build a data sample for the analysis prompt (limit to avoid token overflow)
        sample_rows = unique_rows[:15]
        col_keys = [c["key"] for c in all_columns if c.get("type") != "link"]
        data_preview = []
        for row in sample_rows:
            item = {k: str(row.get(k, ""))[:200] for k in col_keys if row.get(k)}
            data_preview.append(item)

        insights_text = "\n".join(f"- {ins}" for ins in unique_insights) if unique_insights else "None available"

        analysis_resp = client.responses.create(
            model="gpt-5.4-mini-2026-03-17",
            input=f"""You are a research analyst. Write a detailed analysis report based on these research findings about "{scout.topic}".

Data ({len(unique_rows)} items found):
{json.dumps(data_preview, indent=2)[:3000]}

Key insights from research agents:
{insights_text}

Write 4-6 paragraphs covering:
1. **Overview**: What the research found and its significance
2. **Key Findings**: The most important patterns, trends, or standout items in the data
3. **Detailed Analysis**: Compare items, highlight notable differences, price ranges, ratings, or other quantitative patterns
4. **Market/Context Insights**: What this data tells us about the broader landscape
5. **Recommendations**: Actionable takeaways for someone researching this topic

Write in clear, professional prose. Reference specific items from the data by name where relevant. Do NOT use markdown headers — just write flowing paragraphs. Be specific and insightful, not generic.""",
        )
        analysis = analysis_resp.output_text.strip()
        if len(analysis) < 50:
            analysis = ""
        logger.info(f"Generated analysis: {len(analysis)} chars")
    except Exception as e:
        logger.warning(f"Analysis generation failed (non-fatal): {e}")
        analysis = ""

    # Fallback: build analysis from insights if GPT call failed
    if not analysis and unique_insights:
        analysis = f"Research on \"{scout.topic}\" revealed {len(unique_rows)} results across {len(results)} search agents.\n\n"
        analysis += "Key findings:\n" + "\n".join(f"- {ins}" for ins in unique_insights)

    stats = [
        {"label": "Results Found", "value": str(len(unique_rows)), "color": "default"},
        {"label": "Agents Used", "value": str(len(results)), "color": "blue"},
        {"label": "Sources", "value": str(len(all_citations)), "color": "green"},
    ]

    filter_columns = [c["key"] for c in all_columns if c.get("type") == "badge"]

    structured_data = {
        "title": title, "summary": summary, "analysis": analysis, "stats": stats,
        "columns": all_columns, "rows": unique_rows,
        "insights": unique_insights, "filter_columns": filter_columns,
    }

    findings = {
        "citations": all_citations, "format": "structured",
        "structured_data": structured_data,
        "full_text": "\n\n".join(all_raw),
    }

    logger.info(f"Merged: {len(unique_rows)} unique rows from {len(results)} subagents")
    return title, summary, findings


async def _orchestrate_parallel_search(scout: Scout, on_progress=None) -> tuple[str, str, dict, int]:
    """Orchestrate parallel subagent search.
    Returns: (title, summary, findings, agents_used)
    """
    subtasks = await asyncio.to_thread(_generate_subtasks, scout)
    logger.info(f"Orchestrating {len(subtasks)} parallel subagents for '{scout.name}'")
    await _emit(on_progress, "subtasks", f"{len(subtasks)} research tasks created", count=len(subtasks))

    await _emit(on_progress, "agents_running", f"{len(subtasks)} AI agents researching in parallel...", count=len(subtasks))
    tasks = [asyncio.to_thread(_run_single_subagent, st, scout) for st in subtasks]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_results = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Subagent exception: {r}")
        elif isinstance(r, dict):
            valid_results.append(r)

    agents_used = 1 + len(subtasks) + 1  # orchestrator + subagents + merger
    await _emit(on_progress, "agents_done", "Research complete — merging results", agents=len(valid_results))

    await _emit(on_progress, "analysis", "Writing analysis & key findings...")
    title, summary, findings = _merge_subagent_results(scout, valid_results)

    row_count = 0
    sd = findings.get("structured_data")
    if sd and sd.get("rows"):
        row_count = len(sd["rows"])
    await _emit(on_progress, "merged", f"Compiled {row_count} data points", rows=row_count)

    return title, summary, findings, agents_used


# ────────────────────────────────────────────────────────
# Sandbox Analysis
# ────────────────────────────────────────────────────────

def _get_export_description(export_type: str, row_count: int) -> str:
    """Generate a human-readable description for an export artifact."""
    if row_count > 0:
        descriptions = {
            "pdf":   f"Formatted PDF report with {row_count} items in a styled table layout",
            "excel": f"Excel spreadsheet with {row_count} rows of structured data",
            "html":  f"Interactive HTML page with styled data table and summary",
            "csv":   f"Raw CSV data export with {row_count} records",
            "txt":   f"Plain text report with {row_count} items",
            "pptx":  f"PowerPoint presentation with data table slides",
        }
    else:
        descriptions = {
            "pdf":   "Formatted PDF research report",
            "excel": "Research data in Excel format",
            "html":  "Interactive HTML research report with formatted content",
            "csv":   "Research content in CSV format",
            "txt":   "Plain text research report",
            "pptx":  "Research findings as a PowerPoint presentation",
        }
    return descriptions.get(export_type, f"Generated {export_type.upper()} file")


def _run_sandbox_analysis(scout: Scout, findings: dict) -> dict:
    """Generate charts and export artifacts.
    Works with both structured data (rows/columns) AND text-only findings.
    Uses E2B sandbox when available, falls back to pure-Python for HTML/CSV/TXT.
    """
    from app.services import sandbox_runner
    from app.services.sandbox_runner import _slugify, LOCAL_EXPORTABLE, generate_local_export

    # ── Phase 1: Diagnostic logging ──
    sd = findings.get("structured_data")
    full_text = findings.get("full_text")
    data_format = findings.get("format", "unknown")
    has_rows = bool(sd and sd.get("rows"))

    e2b_available = sandbox_runner.is_available()
    if not e2b_available:
        e2b_pkg = sandbox_runner._E2B_AVAILABLE
        e2b_key = bool(get_settings().e2b_api_key)
        logger.warning(
            f"E2B sandbox not available: package_installed={e2b_pkg}, api_key_set={e2b_key}"
        )
    else:
        logger.info("E2B sandbox is available")

    if not has_rows:
        logger.warning(
            f"No structured data with rows (format={data_format}, "
            f"structured_data={'present but no rows' if sd else 'None'}, "
            f"full_text={'yes, ' + str(len(full_text or '')) + ' chars' if full_text else 'None'})"
        )
    else:
        logger.info(f"Structured data available: {len(sd['rows'])} rows, {len(sd.get('columns', []))} columns")

    # ── Phase 2: Chart generation (requires E2B + structured data) ──
    if has_rows and e2b_available:
        force_charts = _wants_charts(scout)
        if force_charts:
            logger.info("User explicitly requested charts/plots — forcing chart generation")
        try:
            result = sandbox_runner.run_analysis(
                sd["rows"], scout.topic,
                columns=sd.get("columns"),
                force_charts=force_charts,
            )
            if result.get("charts"):
                findings["charts"] = result["charts"]
                logger.info(f"Sandbox generated {len(result['charts'])} charts")
            if result.get("analysis_text"):
                findings["sandbox_analysis"] = result["analysis_text"]
        except Exception as e:
            logger.warning(f"Sandbox chart generation failed (non-fatal): {e}")
    elif not has_rows:
        logger.info("Skipping chart generation: no structured row data")
    elif not e2b_available:
        logger.info("Skipping chart generation: E2B not available")

    # ── Phase 3: Export generation with fallback ──
    requested_formats = _detect_requested_formats(scout)
    logger.info(f"Requested export formats: {requested_formats}")

    title = (sd.get("title") if sd else None) or scout.topic
    summary = (sd.get("summary") if sd else None) or (full_text or "")[:500]
    analysis = (sd.get("analysis") if sd else None) or ""
    slug = _slugify(scout.topic)
    columns = sd.get("columns", []) if sd else None
    rows = sd.get("rows", []) if sd else None
    row_count = len(rows) if rows else 0
    now_iso = datetime.utcnow().isoformat()

    if analysis:
        logger.info(f"Analysis text available: {len(analysis)} chars")
    else:
        logger.info("No analysis text available for exports")

    exports = {}
    for export_type in requested_formats:
        export_result = None

        # Strategy A: E2B sandbox (all formats, needs rows + E2B)
        if has_rows and e2b_available:
            try:
                export_result = sandbox_runner.generate_export(
                    data=rows,
                    columns=columns or [],
                    title=title,
                    summary=summary,
                    topic=title,
                    export_type=export_type,
                    slug=slug,
                    analysis=analysis,
                )
                if not export_result.get("data"):
                    logger.warning(
                        f"E2B {export_type} returned no data: {export_result.get('error')}; trying local fallback"
                    )
                    export_result = None
            except Exception as e:
                logger.warning(f"E2B {export_type} failed: {e}; trying local fallback")
                export_result = None

        # Strategy B: Pure-Python local generation (html, csv, txt)
        if export_result is None and export_type in LOCAL_EXPORTABLE:
            logger.info(f"Using local fallback for {export_type} export")
            export_result = generate_local_export(
                export_type=export_type,
                title=title,
                summary=summary,
                columns=columns,
                rows=rows,
                full_text=full_text,
                slug=slug,
                analysis=analysis,
            )

        # Store result
        if export_result and export_result.get("data"):
            exports[export_type] = {
                "data": export_result["data"],
                "filename": export_result["filename"],
                "mime_type": export_result["mime_type"],
                "created_at": now_iso,
                "size_bytes": math.ceil(len(export_result["data"]) * 3 / 4),
                "description": _get_export_description(export_type, row_count),
            }
            logger.info(f"Generated {export_type} export: {export_result['filename']}")
        else:
            err = export_result.get("error") if export_result else "no generation strategy available"
            logger.warning(f"Could not generate {export_type} export: {err}")

    # Add chart PNGs as downloadable artifacts
    if findings.get("charts"):
        for i, chart_b64 in enumerate(findings["charts"], 1):
            exports[f"chart_{i}"] = {
                "data": chart_b64,
                "filename": f"{slug}-chart-{i}.png",
                "mime_type": "image/png",
                "created_at": now_iso,
                "size_bytes": math.ceil(len(chart_b64) * 3 / 4),
                "description": f"Data visualization chart {i}",
            }

    if exports:
        findings["exports"] = exports
        logger.info(f"Total exports generated: {len(exports)} ({', '.join(exports.keys())})")
    else:
        logger.warning("No exports were generated for this report")

    return findings


# ────────────────────────────────────────────────────────
# Semantic Change Detection
# ────────────────────────────────────────────────────────

def _detect_changes_with_ai(topic: str, current_sd: dict, prev_sd: dict) -> dict | None:
    """Compare current report with previous using AI to find real changes.
    Returns a changes dict or None if detection fails."""
    client = _get_openai_client()

    curr_rows = current_sd.get("rows", [])
    prev_rows = prev_sd.get("rows", [])
    curr_cols = current_sd.get("columns", [])

    if not curr_rows or not prev_rows:
        return None

    # Build compact item summaries for comparison
    text_key = next((c["key"] for c in curr_cols if c.get("type") == "text"), "name")
    non_link_keys = [c["key"] for c in curr_cols if c.get("type") != "link"]

    def compact(rows, keys, limit=25):
        out = []
        for r in rows[:limit]:
            item = {k: str(r.get(k, ""))[:150] for k in keys if r.get(k)}
            out.append(item)
        return out

    prev_compact = compact(prev_rows, non_link_keys)
    curr_compact = compact(curr_rows, non_link_keys)

    prompt = f"""You are comparing two consecutive research snapshots about "{topic}".

PREVIOUS REPORT ({len(prev_rows)} items):
{json.dumps(prev_compact, indent=1)[:2500]}

CURRENT REPORT ({len(curr_rows)} items):
{json.dumps(curr_compact, indent=1)[:2500]}

IMPORTANT CONTEXT: These reports come from web searches that may use DIFFERENT sources each time.
Items that appear in one list but not the other are usually just from different search results — NOT real changes.

Identify ONLY genuinely meaningful changes:
- Items that are truly NEW to the landscape (not just found by a different search)
- Items that have genuinely CHANGED (real data updates like price changes, status changes, new versions)
- Items that are genuinely GONE (discontinued, removed, no longer relevant)

Be VERY CONSERVATIVE. If most items differ between runs, it's different sources — report NO changes.
Only flag something as a change if you're confident it represents a real-world change.

Return JSON:
{{
  "has_real_changes": true/false,
  "summary": "1 sentence describing what actually changed, or 'No significant changes detected'",
  "new_items": ["item name 1"],
  "removed_items": ["item name 1"],
  "updated_items": [{{"name": "item", "change": "what changed"}}]
}}

Return ONLY the JSON object."""

    try:
        resp = client.responses.create(
            model="gpt-5.4-mini-2026-03-17",
            input=prompt,
        )
        result = _extract_json(resp.output_text)
        if isinstance(result, dict):
            logger.info(f"AI change detection: has_real_changes={result.get('has_real_changes')}")
            return result
    except Exception as e:
        logger.warning(f"AI change detection failed (non-fatal): {e}")
    return None


# ────────────────────────────────────────────────────────
# Main Entry Point
# ────────────────────────────────────────────────────────

async def _emit(on_progress, event: str, message: str, **data):
    """Emit a progress event if callback is provided."""
    if on_progress:
        await on_progress(event, {"message": message, **data})


async def run_scout(scout: Scout, db: AsyncSession, on_progress=None) -> Report:
    logger.info(f"Running scout: {scout.name} (id={scout.id})")
    await _emit(on_progress, "started", "Analyzing your query...")

    strategy = _detect_search_strategy(scout)
    logger.info(f"Search strategy: {strategy}")
    agents_used = 1

    strategy_label = "Product Search" if strategy == "serpapi_google_shopping" else "Multi-Agent Research"
    await _emit(on_progress, "strategy", f"Strategy: {strategy_label}", strategy=strategy)

    if strategy == "serpapi_google_shopping":
        try:
            await _emit(on_progress, "agents_running", "Searching product databases...", count=1)
            products = await asyncio.to_thread(_search_serpapi_google_shopping, scout)
            if not products:
                logger.warning("SerpAPI returned 0 products, falling back to parallel search")
                title, summary, findings, agents_used = await _orchestrate_parallel_search(scout, on_progress)
            else:
                title, summary, findings = _build_product_report(scout, products, strategy)
                findings = await asyncio.to_thread(_enrich_with_gpt, scout, products, findings)
                title = findings["structured_data"]["title"]
                summary = findings["structured_data"]["summary"]
                agents_used = 2
                await _emit(on_progress, "agents_done", f"Found {len(products)} products", rows=len(products))
        except Exception as e:
            logger.error(f"SerpAPI failed: {e}, falling back to parallel search")
            title, summary, findings, agents_used = await _orchestrate_parallel_search(scout, on_progress)
    else:
        title, summary, findings, agents_used = await _orchestrate_parallel_search(scout, on_progress)

    # Sandbox Analysis
    await _emit(on_progress, "charts", "Generating data visualizations...")
    findings = await asyncio.to_thread(_run_sandbox_analysis, scout, findings)
    if findings.get("charts"):
        agents_used += 1

    formats = _detect_requested_formats(scout)
    await _emit(on_progress, "exports", f"Creating downloadable reports...", formats=formats)

    # AI-powered change detection against previous report
    current_sd = findings.get("structured_data")
    if current_sd and current_sd.get("rows"):
        try:
            prev_result = await db.execute(
                select(Report)
                .where(Report.scout_id == scout.id)
                .order_by(Report.created_at.desc())
                .limit(1)
            )
            prev_report = prev_result.scalar_one_or_none()
            if prev_report and prev_report.findings:
                prev_sd = prev_report.findings.get("structured_data")
                if prev_sd and prev_sd.get("rows"):
                    await _emit(on_progress, "changes", "Detecting changes from previous run...")
                    changes = await asyncio.to_thread(
                        _detect_changes_with_ai, scout.topic, current_sd, prev_sd
                    )
                    if changes:
                        findings["structured_data"]["changes"] = changes
        except Exception as e:
            logger.warning(f"Change detection step failed (non-fatal): {e}")

    # Store metadata
    findings["meta"] = {
        "agents_used": agents_used,
        "strategy": strategy,
        "requested_formats": formats,
        "timestamp": datetime.utcnow().isoformat(),
    }

    await _emit(on_progress, "saving", "Saving report...")

    report = Report(
        scout_id=scout.id, title=title, summary=summary,
        findings=findings, raw_response=findings.get("full_text", ""),
    )
    db.add(report)

    scout.last_run_at = datetime.utcnow()
    scout.next_run_at = datetime.utcnow() + timedelta(minutes=scout.schedule_minutes)
    await db.commit()
    await db.refresh(report)

    # Send email if enabled
    if scout.email_report:
        try:
            from app.services.email_service import send_report_email
            # Load user email via relationship
            await db.refresh(scout, ["user"])
            if scout.user and scout.user.email:
                await _emit(on_progress, "email", "Sending report to your email...")
                await asyncio.to_thread(send_report_email, scout.user.email, report, scout.topic)
        except Exception as e:
            logger.warning(f"Email send failed (non-fatal): {e}")

    logger.info(f"Scout '{scout.name}' done. Report id={report.id}, strategy={strategy}, agents={agents_used}")
    await _emit(on_progress, "complete", "Done!", report_id=report.id)
    return report
