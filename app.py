import streamlit as st
import requests
import re
from bs4 import BeautifulSoup, NavigableString, Tag
import html as html_mod
import json

# ─── CONFIG ───────────────────────────────────────────────────────────────────
COLLECTION_ID = "64ac3a242208dda62b6e6a90"
WEBFLOW_API_BASE = "https://api.webflow.com/v2"
EMBED_CHAR_LIMIT = 10000

# ─── EMBED DETECTION RULES ────────────────────────────────────────────────────
# Top-level CSS classes that mark an element as an EMBED block.
# If any of these classes appear on a tag, the ENTIRE tag (and its children)
# gets wrapped with <div data-rt-embed-type='true'>
EMBED_TOP_CLASSES = {
    # Key Takeaways box
    "takeaway", "key-takeaways",
    # Evaluation Criteria grid
    "criteria",
    # Comparison Table
    "table-scroll",
    # Infographic copy-to-clipboard
    "copy-div",
    # Company Profile card
    "co-card",
    # Testimonial / Expert Quote
    "testimonial",
    # FAQ section (on <section> tag)
    "faq",
    # CTA block
    "cta",
    # Stats grid (from Malaysia template)
    "nl-card",
    # Related reading
    "related-reading",
    # Author block
    "author-block",
    # Infographic placeholder
    "infographic-placeholder",
    # Expert quote (standalone, outside co-card)
    "expert-quote",
    # CTA block (aside variant)
    "cta-block",
    # Steps list
    "steps-list",
}

# Container tags to UNWRAP (strip the tag, process its children individually)
UNWRAP_TAGS = {"article", "main", "header", "nav"}

# Section/aside: unwrap ONLY if they don't have an embed class
# (e.g. <section class="faq"> is embed, but <section> without class is unwrap)

# Tags that are always plain rich text (when no embed class present)
PLAIN_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li",
              "a", "strong", "em", "b", "i", "blockquote", "figure",
              "figcaption", "br", "hr", "img"}


# ─── PREPROCESSING ────────────────────────────────────────────────────────────

def unescape_if_needed(html_content):
    if "&lt;div" in html_content or "&lt;table" in html_content or "&lt;style" in html_content:
        return html_mod.unescape(html_content)
    return html_content


def normalize_html(html_content):
    html_content = unescape_if_needed(html_content)
    # Use BeautifulSoup to extract <body> content reliably
    soup = BeautifulSoup(html_content, "html.parser")
    body = soup.find("body")
    if body:
        # Return the inner HTML of <body>
        return body.decode_contents().strip()
    # If no <body>, check for <article> directly
    article = soup.find("article")
    if article:
        return str(article)
    # Return as-is
    return html_content.strip()


# ─── BLOCK CLASSIFIER ────────────────────────────────────────────────────────

def get_classes(tag):
    """Get classes as a set."""
    classes = tag.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    return set(classes)


def is_embed_block(tag):
    """Check if a tag should be treated as an embed block."""
    if not isinstance(tag, Tag):
        return False

    classes = get_classes(tag)

    # Direct match: tag has a known embed class
    if classes & EMBED_TOP_CLASSES:
        return True

    # <div> with ANY class (likely a styled component)
    if tag.name == "div" and classes:
        return True

    # <section> with a class (like <section class="faq">)
    if tag.name == "section" and classes:
        return True

    # <aside> with a class (like <aside class="cta-block">)
    if tag.name == "aside" and classes:
        return True

    # <table> with a class
    if tag.name == "table" and classes:
        return True

    # <details> tags (FAQ items when not inside a faq section)
    if tag.name == "details":
        return True

    return False


def should_unwrap(tag):
    """Check if a container tag should be unwrapped (children processed individually)."""
    if not isinstance(tag, Tag):
        return False

    classes = get_classes(tag)

    # Always unwrap these tags
    if tag.name in UNWRAP_TAGS:
        return True

    # <section> without embed class → unwrap
    if tag.name == "section" and not (classes & EMBED_TOP_CLASSES):
        return True

    # <aside> without embed class → unwrap
    if tag.name == "aside" and not (classes & EMBED_TOP_CLASSES) and not classes:
        return True

    # <div> without ANY class → generic wrapper, unwrap
    if tag.name == "div" and not classes:
        return True

    return False


def is_noise(element):
    """Check if an element is noise (comments, section labels, etc.) to strip."""
    if isinstance(element, NavigableString):
        text = str(element).strip()
        if not text:
            return True
        # Strip section comment labels like "Section 1: Title"
        if re.match(r'^(Section \d+|Company \d+|Mid-Blog|Expert Quote|End)', text, re.IGNORECASE):
            return True
        # Strip bare text that looks like a comment
        if text.startswith("REPLACE:") or text.startswith("PLACEHOLDER:"):
            return True
    return False


def unwrap_containers(soup):
    """
    Unwrap the outermost generic container if there's only one.
    e.g. <article><...content...></article> → process content directly.
    """
    children = list(soup.children)
    real_children = [c for c in children if isinstance(c, Tag) or
                     (isinstance(c, NavigableString) and str(c).strip())]

    if len(real_children) == 1 and isinstance(real_children[0], Tag):
        child = real_children[0]
        if child.name in UNWRAP_TAGS:
            return unwrap_containers(child)
        if child.name == "div" and not get_classes(child):
            return unwrap_containers(child)

    return soup


def process_children(parent, blocks):
    """
    Recursively process children of a container element.
    - Embed blocks → collect as embed
    - Unwrappable containers → recurse into their children
    - Plain tags → collect as plain
    - Noise text → skip
    - <style> → skip (Webflow has its own CSS)
    """
    for element in parent.children:
        # Skip noise (empty text, section labels, placeholders)
        if is_noise(element):
            continue

        # Skip non-tag, non-string
        if not isinstance(element, (Tag, NavigableString)):
            continue

        # NavigableString that's not noise — skip loose text
        if isinstance(element, NavigableString):
            continue

        # <style> → skip entirely (Webflow uses its own stylesheets)
        if element.name == "style":
            continue

        # <script> → skip
        if element.name == "script":
            continue

        # <h1> → skip (handled by the Name field, not content)
        if element.name == "h1":
            continue

        # Is this an embed block?
        if is_embed_block(element):
            blocks.append(("embed", str(element)))
            continue

        # Should this container be unwrapped?
        if should_unwrap(element):
            process_children(element, blocks)
            continue

        # <p> that might contain embed children (parser quirk)
        if element.name == "p":
            has_inner_embeds = any(
                isinstance(child, Tag) and is_embed_block(child)
                for child in element.children
            )
            if has_inner_embeds:
                current_plain = []
                for child in element.children:
                    if isinstance(child, Tag) and is_embed_block(child):
                        if current_plain:
                            plain_html = "".join(str(c) for c in current_plain).strip()
                            if plain_html and plain_html not in ("<br/>", "<br>", ""):
                                blocks.append(("plain", f"<p>{plain_html}</p>"))
                            current_plain = []
                        blocks.append(("embed", str(child)))
                    else:
                        current_plain.append(child)
                if current_plain:
                    plain_html = "".join(str(c) for c in current_plain).strip()
                    if plain_html and plain_html not in ("<br/>", "<br>", ""):
                        blocks.append(("plain", f"<p>{plain_html}</p>"))
                continue

        # Plain rich text element
        el_html = str(element).strip()
        if el_html:
            blocks.append(("plain", el_html))


def split_into_blocks(html_content):
    html_content = normalize_html(html_content)
    soup = BeautifulSoup(html_content, "html.parser")

    # Unwrap outermost container (e.g. <article>)
    soup = unwrap_containers(soup)

    blocks = []
    process_children(soup, blocks)

    return blocks


# ─── CONVERSION LAYER ─────────────────────────────────────────────────────────
# Transforms HTML file format → Webflow template format before wrapping.

# CSS style block for co-card and related components (injected once)
CO_CARD_STYLE = '''<style>
  .co-card {
    background-color: #fff;
    border: 1px solid #e2e2dd;
    transition: box-shadow .3s;
    border-radius: 8px;
    padding: 30px;
    margin: 14px 0;
  }
  .co-card:hover { box-shadow: 0 8px 30px rgba(0,0,0,.06); }
  .co-hdr { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
  .co-hdr h3, .co-hdr p { margin: 0; }
  .co-logo { border-radius: 8px; padding: 4px 10px; border: 1px solid #264cbe; display: flex; align-items: center; justify-content: center; height:70px; }
  .co-logo img { width: 150px; height: 100%; object-fit: contain; }
  .meta-row { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
  .chip { font-size: 12px; padding: 4px 10px; border-radius: 5px; background: #f3f4f6; color: #1a1a2e; font-weight: 500; }
  .co-card p { margin-top: 0px; }
  .co-card p a { text-decoration: none; }
  .co-card ul { padding-left: 20px; margin-bottom: 12px; margin-top: 10px; }
  .co-card li { margin-bottom: 4px; }
  .insight { display: flex; gap: 16px; background: linear-gradient(135deg, #fefce8, #fef9c3); border: 1px solid #fde68a; border-radius: 10px; padding: 24px; margin: 16px 0; }
  .insight-icon { width: 40px; height: 40px; border-radius: 50%; background: #fef3c7; display: flex; align-items: center; justify-content: center; font-size: 20px; flex-shrink: 0; }
  .ename { font-weight: 600; font-size: 14px; margin-top: 8px; }
  .author-del { font-size: 13px; color: #6b7280; margin-top: 4px; }
</style>'''

# CSS style block for criteria grid
CRITERIA_STYLE = '''<style>
  .criteria {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    gap: 1rem;
    margin: 1.5rem 0 2rem;
  }
  .crit-item {
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 1.25rem;
    text-align: center;
  }
  .crit-icon { font-size: 1.8rem; margin-bottom: 0.5rem; }
  .crit-item p:first-of-type { font-weight: 700; font-size: 0.95rem; color: #1E40AF; margin-bottom: 0.4rem; }
  .crit-item p:last-of-type { font-size: 0.82rem; color: #6B7280; line-height: 1.5; }
  @media (max-width: 768px) { .criteria { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 480px) { .criteria { grid-template-columns: 1fr; } }
</style>'''


def convert_key_takeaways(soup_tag):
    """key-takeaways + <h3> → takeaway + <p>💡 KEY TAKEAWAYS</p>"""
    soup_tag["class"] = ["takeaway"]
    h3 = soup_tag.find("h3")
    if h3:
        h3.name = "p"
        h3.string = "💡 KEY TAKEAWAYS"
    return str(soup_tag)


def convert_eval_grid(soup_tag):
    """eval-grid/eval-card/icon/factor/desc → criteria/crit-item/crit-icon + <p> pairs"""
    soup_tag["class"] = ["criteria"]
    for card in soup_tag.find_all("div", class_="eval-card"):
        card["class"] = ["crit-item"]
        icon_div = card.find("div", class_="icon")
        if icon_div:
            icon_div["class"] = ["crit-icon"]
        # factor → first <p> (replace div with p)
        factor = card.find("div", class_="factor")
        if factor:
            factor.name = "p"
            del factor["class"]
        # desc → second <p> (replace div with p)
        desc = card.find("div", class_="desc")
        if desc:
            desc.name = "p"
            del desc["class"]
    return str(soup_tag)


def convert_table_wrap(soup_tag):
    """table-wrap → table-scroll, add comp-table class, add rank spans"""
    soup_tag["class"] = ["table-scroll"]
    table = soup_tag.find("table")
    if table:
        table["class"] = ["comp-table"]
        # Add <span class="rank"> around first <td> content in each row
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if tds and tds[0].string and tds[0].string.strip().isdigit():
                num = tds[0].string.strip()
                tds[0].clear()
                span = BeautifulSoup(f'<span class="rank">{num}</span>', "html.parser")
                tds[0].append(span)
    return str(soup_tag)


def convert_company_profile(soup_tag):
    """company-profile → co-card with co-hdr, co-logo, meta-row, chip structure"""
    # Change main class
    classes = soup_tag.get("class", [])
    new_classes = ["co-card"]
    if "featured" in classes:
        new_classes.append("featured")  # preserve featured flag if present
    soup_tag["class"] = new_classes

    # Find and restructure the header: h3 + tagline → co-hdr with co-logo
    h3 = soup_tag.find("h3")
    tagline = soup_tag.find("p", class_="tagline")

    if h3:
        # Build co-hdr structure
        co_hdr_html = '<div class="co-hdr"><div class="co-logo"></div><div>'
        co_hdr_html += str(h3)
        if tagline:
            # Remove tagline class
            tagline_copy = str(tagline).replace(' class="tagline"', '')
            co_hdr_html += tagline_copy
        co_hdr_html += '</div></div>'

        # Insert co-hdr before h3
        co_hdr = BeautifulSoup(co_hdr_html, "html.parser")
        h3.insert_before(co_hdr)
        h3.decompose()
        if tagline:
            tagline.decompose()

    # meta-badges → meta-row, badge → chip
    meta = soup_tag.find("div", class_="meta-badges")
    if meta:
        meta["class"] = ["meta-row"]
        for badge in meta.find_all("span", class_="badge"):
            badge["class"] = ["chip"]

    # offerings-title div → <b>Key Offerings:</b>
    off_title = soup_tag.find("div", class_="offerings-title")
    if off_title:
        off_title.replace_with(BeautifulSoup("<b>Key Offerings:</b>", "html.parser"))

    # highlights-title div → <b>Highlights:</b>
    hi_title = soup_tag.find("div", class_="highlights-title")
    if hi_title:
        hi_title.replace_with(BeautifulSoup("<b>Highlights:</b>", "html.parser"))

    # company-location div → <p><b>Location:</b> text</p>
    loc = soup_tag.find("div", class_="company-location")
    if loc:
        loc.name = "p"
        del loc["class"]

    # expert-quote inside card → insight block (simplified — keep as-is if present)
    # The insight block structure in the Webflow format is already inside co-card

    return str(soup_tag)


def convert_expert_quote_to_testimonial(soup_tag):
    """
    expert-quote (simple blockquote + attribution) → testimonial layout.
    Note: The input has placeholder data; the real testimonial data comes from
    the content writers. We convert the structure but keep the content.
    """
    soup_tag["class"] = ["testimonial"]

    # blockquote → plain <p>
    bq = soup_tag.find("blockquote")
    if bq:
        bq.name = "p"

    # attribution div → simplified author section
    attr = soup_tag.find("div", class_="attribution")
    if attr:
        attr["class"] = ["div-flex"]
        # The attribution has <strong>Name</strong><br/>Title<br/>Credentials
        # Convert to the author-name structure
        strong = attr.find("strong")
        name = strong.string if strong else ""
        # Get remaining text
        text_parts = [s.strip() for s in attr.stripped_strings if s.strip() != name]

        new_html = f'<div class="author-name"><div class="name-flex"><b>{name}</b></div>'
        if len(text_parts) >= 1:
            new_html += f'<p class="author-pos">{text_parts[0]}</p>'
        if len(text_parts) >= 2:
            new_html += f'<p class="author-del">{text_parts[1]}</p>'
        new_html += '</div>'

        attr.clear()
        attr.append(BeautifulSoup(new_html, "html.parser"))

    return str(soup_tag)


def convert_steps_to_paragraphs(soup_tag):
    """
    steps-list with step-items (h4 + p) → plain <p> paragraphs with bold lead.
    Returns a LIST of plain blocks, not a single embed.
    """
    paragraphs = []
    for step in soup_tag.find_all("div", class_="step-item"):
        h4 = step.find("h4")
        p = step.find("p")
        if h4 and p:
            # Combine: <p><strong>Step title.</strong> Step content...</p>
            step_html = f"<p><strong>{h4.get_text()}</strong> {p.decode_contents()}</p>"
            paragraphs.append(step_html)
    return paragraphs


def convert_faq_details(details_list):
    """
    <details><summary>Q</summary><div class="faq-answer"><p>A</p></div></details>
    → <section class="faq" itemscope itemtype="https://schema.org/FAQPage">
        <div class="faq-item"><div class="faq-question"><p>Q</p><span class="toggle-icon"></span></div>
        <div class="faq-answer"><p>A</p></div></div>
    """
    faq_html = '<section class="faq" itemscope="" itemtype="https://schema.org/FAQPage">\n'

    for i, detail in enumerate(details_list):
        summary = detail.find("summary")
        answer_div = detail.find("div", class_="faq-answer")

        q_text = summary.get_text() if summary else ""
        a_html = answer_div.decode_contents() if answer_div else ""

        active = ' active' if i == 0 else ''
        faq_html += f'''<div class="faq-item{active}" itemscope="" itemprop="mainEntity" itemtype="https://schema.org/Question">
<div class="faq-question" data-index="{i}">
<p itemprop="name">{q_text}</p>
<span class="toggle-icon"></span>
</div>
<div class="faq-answer" id="answer-{i}" itemprop="acceptedAnswer" itemscope="" itemtype="https://schema.org/Answer">
{a_html}
</div>
</div>\n'''

    faq_html += '</section>'
    return faq_html


def convert_cta_block(soup_tag, is_end_cta=False):
    """aside.cta-block → div.cta (mid) or div.cta.bg-green (end)"""
    new_div = BeautifulSoup(str(soup_tag), "html.parser").find()
    new_div.name = "div"

    if is_end_cta:
        new_div["class"] = ["cta", "bg-green"]
    else:
        new_div["class"] = ["cta"]

    # Add style to h3
    h3 = new_div.find("h3")
    if h3:
        h3["style"] = "color:white;margin-top:0px"

    return str(new_div)


def convert_quotes_to_single(html_str):
    """Convert all double-quoted attributes to single quotes for Webflow."""
    result = re.sub(r'(\w+)="([^"]*)"', r"\1='\2'", html_str)
    return result


def convert_block(block_type, block_html):
    """Apply the appropriate conversion based on the block's content."""
    soup = BeautifulSoup(block_html, "html.parser")
    first_tag = soup.find()
    if not first_tag:
        return block_html

    classes = set(first_tag.get("class", []))

    # Key Takeaways
    if "key-takeaways" in classes:
        return convert_key_takeaways(first_tag)

    # Eval Grid → Criteria
    if "eval-grid" in classes:
        return convert_eval_grid(first_tag)

    # Table Wrap → Table Scroll
    if "table-wrap" in classes:
        return convert_table_wrap(first_tag)

    # Company Profile → Co-Card
    if "company-profile" in classes:
        return convert_company_profile(first_tag)

    # Expert Quote → Testimonial
    if "expert-quote" in classes:
        return convert_expert_quote_to_testimonial(first_tag)

    # CTA Block (aside)
    if "cta-block" in classes:
        return convert_cta_block(first_tag, is_end_cta=False)

    return block_html


def classify_and_wrap(html_content):
    blocks = split_into_blocks(html_content)

    output_parts = []
    embed_count = 0
    plain_count = 0
    warnings = []
    style_injected = False
    criteria_style_injected = False
    cta_count = 0  # Track CTAs to detect end CTA

    # First pass: count CTAs to identify the last one
    total_ctas = sum(1 for bt, bh in blocks
                     if bt == "embed" and ("cta-block" in str(bh)[:200] or
                                            "cta" in str(BeautifulSoup(bh, "html.parser").find().get("class", [])) if BeautifulSoup(bh, "html.parser").find() else False))

    for block_type, block_html in blocks:
        if block_type == "embed":
            soup = BeautifulSoup(block_html, "html.parser")
            first_tag = soup.find()
            classes = set(first_tag.get("class", [])) if first_tag else set()

            # ── Special: steps-list → convert to plain paragraphs ──
            if "steps-list" in classes:
                paragraphs = convert_steps_to_paragraphs(first_tag)
                for p in paragraphs:
                    output_parts.append(p)
                    plain_count += 1
                continue

            # ── Special: FAQ details → convert to faq section ──
            if first_tag and first_tag.name == "details":
                # Collect all consecutive details blocks
                # (they come one by one from the parser)
                if not any("section" in p and "faq" in p for p in output_parts[-1:]):
                    # This is a standalone details — collect it
                    # We'll handle FAQ collection below
                    pass

            # ── Special: Inject criteria style before first criteria block ──
            if ("eval-grid" in classes or "criteria" in classes) and not criteria_style_injected:
                crit_style_wrapped = f'<div data-rt-embed-type="true">\n{CRITERIA_STYLE}\n</div>'
                output_parts.append(crit_style_wrapped)
                embed_count += 1
                criteria_style_injected = True

            # ── Special: Inject style block before first co-card ──
            if ("company-profile" in classes or "co-card" in classes) and not style_injected:
                style_wrapped = f'<div data-rt-embed-type="true">\n{CO_CARD_STYLE}\n</div>'
                output_parts.append(style_wrapped)
                embed_count += 1
                style_injected = True

            # ── Special: CTA detection (mid vs end) ──
            if "cta-block" in classes:
                cta_count += 1

            # Apply structural conversion
            block_html = convert_block(block_type, block_html)

            if len(block_html) > EMBED_CHAR_LIMIT:
                soup2 = BeautifulSoup(block_html, "html.parser")
                ft = soup2.find()
                cn = " ".join(ft.get("class", [])) if ft else "unknown"
                warnings.append({
                    "block": f"{ft.name if ft else '?'}.{cn}",
                    "chars": len(block_html),
                    "preview": block_html[:150] + "..."
                })

            wrapped = f'<div data-rt-embed-type="true">\n{block_html}\n</div>'
            output_parts.append(wrapped)
            embed_count += 1
        else:
            stripped = block_html.strip()
            if stripped and stripped not in ("<p></p>", "<p> </p>", "<br/>", "<br>"):
                output_parts.append(stripped)
                plain_count += 1

    # ── Post-processing: collect consecutive <details> into one FAQ section ──
    final_parts = []
    details_buffer = []
    for part in output_parts:
        # Check if this is a wrapped details block
        if 'data-rt-embed-type="true"' in part and "<details" in part:
            # Extract the details tag
            s = BeautifulSoup(part, "html.parser")
            wrapper = s.find("div", attrs={"data-rt-embed-type": True})
            detail = wrapper.find("details") if wrapper else None
            if detail:
                details_buffer.append(detail)
                continue
        else:
            # Flush any buffered details as FAQ
            if details_buffer:
                faq_html = convert_faq_details(details_buffer)
                faq_wrapped = f'<div data-rt-embed-type="true">\n{faq_html}\n</div>'
                final_parts.append(faq_wrapped)
                details_buffer = []

        final_parts.append(part)

    # Flush remaining details
    if details_buffer:
        faq_html = convert_faq_details(details_buffer)
        faq_wrapped = f'<div data-rt-embed-type="true">\n{faq_html}\n</div>'
        final_parts.append(faq_wrapped)

    processed_html = "\n".join(final_parts)

    # Process external links: add rel='nofollow' and target='_blank' for non-edstellar links
    link_soup = BeautifulSoup(processed_html, "html.parser")
    for a_tag in link_soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        # Skip internal links (edstellar.com, relative paths, mailto, #anchors)
        if (href.startswith("/") or
            href.startswith("#") or
            href.startswith("mailto:") or
            "edstellar.com" in href):
            # Edstellar links: ensure target='_blank' but NO nofollow
            if href.startswith("http") and "edstellar.com" in href:
                a_tag["target"] = "_blank"
            continue
        # External links: add nofollow + new tab
        if href.startswith("http"):
            a_tag["target"] = "_blank"
            a_tag["rel"] = "nofollow"
    processed_html = str(link_soup)

    # Global: convert double quotes to single quotes on all embed blocks
    processed_html = convert_quotes_to_single(processed_html)
    # Restore the data-rt-embed-type wrapper to double quotes
    processed_html = processed_html.replace("data-rt-embed-type='true'", 'data-rt-embed-type="true"')

    # Recalculate counts after FAQ merging
    final_soup = BeautifulSoup(processed_html, "html.parser")
    embed_count = 0
    plain_count = 0
    for el in final_soup.children:
        if isinstance(el, Tag):
            if el.get("data-rt-embed-type"):
                embed_count += 1
            else:
                plain_count += 1

    stats = {
        "total_blocks": embed_count + plain_count,
        "embed_blocks": embed_count,
        "plain_blocks": plain_count,
        "warnings": warnings,
        "total_chars": len(processed_html),
    }

    return processed_html, stats


# ─── WEBFLOW API ──────────────────────────────────────────────────────────────

def get_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "accept": "application/json",
    }


def test_api_connection(token):
    """Test API token by checking collection access and item count."""
    results = {}

    # 1. Test token + collection access — get collection info
    resp = requests.get(f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}",
                        headers=get_headers(token))
    if resp.status_code == 200:
        col = resp.json()
        results["collection"] = {
            "status": "✅ OK",
            "name": col.get("displayName", "?"),
            "slug": col.get("slug", "?"),
            "fields": len(col.get("fields", [])),
        }
    elif resp.status_code in (401, 403):
        results["collection"] = {"status": f"❌ Auth failed ({resp.status_code})", "error": resp.text}
        return results
    else:
        results["collection"] = {"status": f"❌ {resp.status_code}", "error": resp.text}
        return results

    # 2. Test items read — get first page count
    resp = requests.get(f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}/items",
                        headers=get_headers(token), params={"limit": 1})
    if resp.status_code == 200:
        data = resp.json()
        total = data.get("pagination", {}).get("total", 0)
        # Also grab the first item name as proof
        items = data.get("items", [])
        sample = items[0]["fieldData"].get("name", "?") if items else "—"
        results["items"] = {"status": "✅ OK", "total_items": total, "sample": sample}
    else:
        results["items"] = {"status": f"❌ {resp.status_code}", "error": resp.text}

    # 3. Test write scope — use token introspect
    resp = requests.get(f"{WEBFLOW_API_BASE}/token/introspect",
                        headers=get_headers(token))
    if resp.status_code == 200:
        info = resp.json()
        results["token"] = {
            "status": "✅ OK",
            "type": info.get("authorization", {}).get("type", "?"),
        }
    else:
        # Introspect might not work for site tokens — that's fine
        results["token"] = {"status": "ℹ️ Skipped (site token)", "note": "CMS access confirmed above"}

    return results


def search_item_by_slug(token, slug):
    url = f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}/items"
    headers = get_headers(token)
    offset = 0
    limit = 100

    while True:
        resp = requests.get(url, headers=headers, params={"offset": offset, "limit": limit})
        if resp.status_code != 200:
            return None, f"API Error {resp.status_code}: {resp.text}"

        data = resp.json()
        for item in data.get("items", []):
            if item.get("fieldData", {}).get("slug") == slug:
                return item, None

        total = data.get("pagination", {}).get("total", 0)
        if offset + limit >= total:
            break
        offset += limit

    return None, f"No item found with slug: '{slug}'"


def update_item_content(token, item_id, content_html, live=False):
    if live:
        url = f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}/items/live"
    else:
        url = f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}/items"
    headers = get_headers(token)

    payload = {
        "items": [{
            "id": item_id,
            "fieldData": {
                "content": content_html
            }
        }]
    }

    resp = requests.patch(url, headers=headers, json=payload)
    return resp


def create_new_item(token, name, slug, content_html, extra_fields=None):
    """Create a new blog post in the collection."""
    url = f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}/items"
    headers = get_headers(token)

    field_data = {
        "name": name,
        "slug": slug,
        "content": content_html,
    }

    # Add optional fields if provided
    if extra_fields:
        field_data.update(extra_fields)

    payload = {
        "items": [{
            "fieldData": field_data,
            "isDraft": True,
        }]
    }

    resp = requests.post(url, headers=headers, json=payload)
    return resp


# ─── STREAMLIT UI ─────────────────────────────────────────────────────────────

st.set_page_config(page_title="Edstellar Blog → Webflow", page_icon="🚀", layout="wide")

st.title("🚀 Edstellar Blog Content → Webflow CMS")
st.caption("Upload HTML → Preview processed blocks → Push to Webflow content field")

# Sidebar
with st.sidebar:
    st.header("⚙️ Settings")
    api_token = st.text_input("Webflow API Token", type="password",
                               help="Site API token with CMS edit+read scope")

    push_live = st.checkbox("Push to Live (not just Draft)", value=False,
                             help="If checked, updates go live immediately")

    # API Test button
    if api_token:
        if st.button("🧪 Test API Connection", use_container_width=True):
            with st.spinner("Testing..."):
                results = test_api_connection(api_token)

            # Collection
            col = results.get("collection", {})
            if col:
                if "name" in col:
                    st.success(f"**Collection:** {col['status']} — {col['name']} ({col['fields']} fields)")
                else:
                    st.error(f"**Collection:** {col['status']}")
                    st.code(col.get("error", ""), language="json")

            # Items
            items = results.get("items", {})
            if items:
                if "total_items" in items:
                    st.success(f"**Items:** {items['status']} — {items['total_items']} blog posts")
                    st.caption(f"Sample: {items.get('sample', '—')}")
                else:
                    st.error(f"**Items:** {items['status']}")

            # Token info
            tok = results.get("token", {})
            if tok:
                st.info(f"**Token:** {tok['status']}")
    else:
        st.caption("Enter token above, then test connection")

    st.divider()
    st.markdown("**Collection:** Blog Posts")
    st.code(COLLECTION_ID, language=None)

    st.divider()
    st.markdown("""
    **Workflow:**
    1. Enter blog slug
    2. Upload HTML file
    3. Auto-processes into blocks
    4. Preview & push

    **Content types:**
    - 🟢 Plain rich text → as-is
    - 🟡 Embed → wrapped with `data-rt-embed-type`
    """)

# Slug input
# Mode selector
mode = st.radio("📋 Mode", ["Update Existing Blog", "Create New Blog"], horizontal=True)

# Initialize variables for both modes
slug = ""
new_name = ""
new_slug = ""
new_meta_title = ""
new_meta_desc = ""
new_description = ""
new_canonical = ""
new_primary_keyword = ""
new_keyword_volume = 0
new_format_blog = True
new_faqs_section = True

if mode == "Update Existing Blog":
    slug = st.text_input("🔗 Blog Post Slug",
                          placeholder="corporate-training-companies-malaysia",
                          help="Slug of the existing blog post to update")

    if slug and api_token:
        if st.button("🔍 Find Blog Post"):
            with st.spinner("Searching..."):
                item, error = search_item_by_slug(api_token, slug)
            if error:
                st.error(error)
            else:
                st.session_state["found_item"] = item
                fd = item.get("fieldData", {})
                st.success(f"✅ **{fd.get('name')}** — ID: `{item['id']}`")
    elif slug and not api_token:
        st.info("Enter your API token in the sidebar to search.")

    # Show editable meta fields if item found
    found_item = st.session_state.get("found_item")
    if found_item:
        fd = found_item.get("fieldData", {})
        with st.expander("✏️ Edit Meta Fields (pre-filled from existing blog)", expanded=True):
            edit_name = st.text_input("Name (H1)", value=fd.get("name", ""), key="edit_name")
            edit_slug = st.text_input("Slug", value=fd.get("slug", ""), key="edit_slug")
            edit_meta_title = st.text_input("Meta Title", value=fd.get("meta-title", ""), key="edit_meta_title")
            edit_meta_desc = st.text_area("Meta Description", value=fd.get("meta-description", ""), key="edit_meta_desc", max_chars=300)
            edit_canonical = st.text_input("Canonical Links", value=fd.get("canonical-links", ""), key="edit_canonical")

else:
    # Create new mode
    new_name = st.text_input("📝 Blog Post Title (Name)*",
                              placeholder="11 Best Corporate Training Companies in Malaysia for 2026")
    new_slug = st.text_input("🔗 Slug*",
                              placeholder="corporate-training-companies-malaysia",
                              help="URL slug — lowercase, hyphens, no spaces")

    # Auto-generate slug from name
    if new_name and not new_slug:
        auto_slug = re.sub(r'[^a-z0-9]+', '-', new_name.lower()).strip('-')
        st.caption(f"Auto-slug: `{auto_slug}`")

    with st.expander("Optional Fields"):
        new_meta_title = st.text_input("Meta Title", placeholder="Same as title if blank")
        new_meta_desc = st.text_area("Meta Description", placeholder="Short description for SEO", max_chars=300)
        new_description = st.text_area("Description (excerpt)", placeholder="Short excerpt for listings", max_chars=500)
        new_canonical = st.text_input("Canonical URL", placeholder="https://www.edstellar.com/blog/your-slug")
        new_primary_keyword = st.text_input("Primary Keyword", placeholder="corporate training companies malaysia")
        new_keyword_volume = st.number_input("Keyword Search Volume", min_value=0, value=0)
        new_format_blog = st.checkbox("New Format Blog", value=True)
        new_faqs_section = st.checkbox("FAQS Section", value=True)

    slug = new_slug  # for file naming

st.divider()

# Upload - two options
upload_type = st.radio("📤 Upload Type", ["HTML File (auto-converts)", "CSV (pre-formatted Webflow content)"], horizontal=True)

if upload_type == "HTML File (auto-converts)":
    uploaded_file = st.file_uploader("📄 Upload Blog HTML", type=["html", "htm"])

    if uploaded_file:
        raw_html = uploaded_file.read().decode("utf-8")
        st.caption(f"Loaded **{uploaded_file.name}** — {len(raw_html):,} characters")

        with st.spinner("Processing HTML..."):
            processed_html, stats = classify_and_wrap(raw_html)

            # Parse into individual blocks for editing
            block_soup = BeautifulSoup(processed_html, "html.parser")
            blocks_list = []
            for element in block_soup.children:
                if isinstance(element, NavigableString):
                    continue
                if not isinstance(element, Tag):
                    continue
                is_embed = element.get("data-rt-embed-type") == "true"
                blocks_list.append({
                    "type": "embed" if is_embed else "plain",
                    "html": str(element),
                    "tag": element.name,
                    "preview": element.get_text()[:100].replace("\n", " ").strip(),
                    "chars": len(str(element)),
                })

            st.session_state["blocks"] = blocks_list
            st.session_state["stats"] = stats

else:
    # CSV upload — pre-formatted Webflow content
    uploaded_csv = st.file_uploader("📄 Upload Content CSV", type=["csv"])

    if uploaded_csv:
        import csv
        import io
        csv_text = uploaded_csv.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)

        # Concatenate all content rows
        content_parts = [row.get("content", "").strip() for row in rows if row.get("content", "").strip()]
        csv_content = "\n".join(content_parts)

        st.caption(f"Loaded **{uploaded_csv.name}** — {len(rows)} blocks, {len(csv_content):,} characters")

        # Parse into blocks for display
        block_soup = BeautifulSoup(csv_content, "html.parser")
        blocks_list = []
        for element in block_soup.children:
            if isinstance(element, NavigableString):
                continue
            if not isinstance(element, Tag):
                continue
            is_embed = element.get("data-rt-embed-type") == "true"
            blocks_list.append({
                "type": "embed" if is_embed else "plain",
                "html": str(element),
                "tag": element.name,
                "preview": element.get_text()[:100].replace("\n", " ").strip(),
                "chars": len(str(element)),
            })

        st.session_state["blocks"] = blocks_list
        st.session_state["processed_html"] = csv_content
        st.session_state["stats"] = {
            "total_blocks": len(blocks_list),
            "embed_blocks": sum(1 for b in blocks_list if b["type"] == "embed"),
            "plain_blocks": sum(1 for b in blocks_list if b["type"] == "plain"),
            "warnings": [],
            "total_chars": len(csv_content),
        }

if "blocks" in st.session_state:
    blocks_list = st.session_state["blocks"]

    # Rebuild stats from current blocks
    embed_count = sum(1 for b in blocks_list if b["type"] == "embed")
    plain_count = sum(1 for b in blocks_list if b["type"] == "plain")
    total_chars = sum(b["chars"] for b in blocks_list)

    # Stats
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Blocks", len(blocks_list))
    c2.metric("🟢 Plain", plain_count)
    c3.metric("🟡 Embeds", embed_count)
    c4.metric("Total Size", f"{total_chars:,} ch")

    # ── Group blocks by H2 sections ──
    sections = []
    current_section = {"title": "📌 Introduction", "blocks": [], "indices": []}

    for idx, block in enumerate(blocks_list):
        # Detect H2 headings to start new sections
        soup_check = BeautifulSoup(block["html"], "html.parser")
        h2 = soup_check.find("h2")
        if h2 and block["type"] == "plain":
            # Save previous section
            if current_section["blocks"] or current_section["title"] == "📌 Introduction":
                sections.append(current_section)
            # Start new section
            h2_text = h2.get_text()[:50].strip()
            current_section = {"title": h2_text, "blocks": [], "indices": []}

        current_section["blocks"].append(block)
        current_section["indices"].append(idx)

    # Append last section
    if current_section["blocks"]:
        sections.append(current_section)

    # Create tabs for each section
    section_tabs = st.tabs([s["title"] for s in sections])

    for sec_idx, (section, sec_tab) in enumerate(zip(sections, section_tabs)):
        with sec_tab:
            st.caption(f"{len(section['blocks'])} blocks | {sum(b['chars'] for b in section['blocks']):,} chars")

            for i, (block, global_idx) in enumerate(zip(section["blocks"], section["indices"])):
                is_embed = block["type"] == "embed"
                icon = "🟡" if is_embed else "🟢"
                type_label = "EMBED" if is_embed else "PLAIN"
                tag_info = "" if is_embed else f" <{block['tag']}>"
                preview = block["preview"][:60]

                with st.expander(f"{icon} **Block {global_idx+1}** — {type_label}{tag_info} ({block['chars']:,} ch) | {preview}"):
                    if is_embed:
                        inner_soup = BeautifulSoup(block["html"], "html.parser")
                        wrapper = inner_soup.find("div", attrs={"data-rt-embed-type": "true"})
                        inner_html = wrapper.decode_contents().strip() if wrapper else block["html"]
                        st.code(inner_html[:3000] + ("..." if len(inner_html) > 3000 else ""), language="html")
                        if block["chars"] > EMBED_CHAR_LIMIT:
                            st.error(f"⚠️ Exceeds {EMBED_CHAR_LIMIT:,} char limit!")
                    else:
                        st.markdown(block["html"], unsafe_allow_html=True)
                        st.code(block["html"][:1000], language="html")

    # Rebuild processed HTML from blocks
    processed_html = "\n".join(b["html"] for b in blocks_list)
    st.session_state["processed_html"] = processed_html

    # Source HTML & Download
    st.divider()
    dl_col1, dl_col2 = st.columns(2)
    with dl_col1:
        with st.expander("💻 Source HTML"):
            st.code(processed_html[:15000] + ("\n\n... [TRUNCATED]" if len(processed_html) > 15000 else ""),
                    language="html")
    with dl_col2:
        st.download_button(
            "📥 Download Webflow-Ready HTML",
            data=processed_html,
            file_name=f"webflow_ready_{slug or 'content'}.html",
            mime="text/html",
            use_container_width=True
        )

    # Push section
    st.divider()
    st.subheader("🚀 Push to Webflow CMS")

    if not api_token:
        st.warning("Enter your Webflow API token in the sidebar.")
    elif mode == "Update Existing Blog":
        found_item = st.session_state.get("found_item")
        if not found_item:
            st.warning("Search for the blog post first using the slug above.")
        else:
            item_name = found_item["fieldData"].get("name", "?")
            item_id = found_item["id"]
            target = "**LIVE**" if push_live else "**Draft (staged)**"

            # Build update payload with meta fields
            update_fields = {"content": processed_html}

            # Check if meta fields were edited
            if "edit_name" in st.session_state and st.session_state["edit_name"] != found_item["fieldData"].get("name", ""):
                update_fields["name"] = st.session_state["edit_name"]
            if "edit_slug" in st.session_state and st.session_state["edit_slug"] != found_item["fieldData"].get("slug", ""):
                update_fields["slug"] = st.session_state["edit_slug"]
            if "edit_meta_title" in st.session_state and st.session_state["edit_meta_title"] != found_item["fieldData"].get("meta-title", ""):
                update_fields["meta-title"] = st.session_state["edit_meta_title"]
            if "edit_meta_desc" in st.session_state and st.session_state["edit_meta_desc"] != found_item["fieldData"].get("meta-description", ""):
                update_fields["meta-description"] = st.session_state["edit_meta_desc"]
            if "edit_canonical" in st.session_state and st.session_state["edit_canonical"] != found_item["fieldData"].get("canonical-links", ""):
                update_fields["canonical-links"] = st.session_state["edit_canonical"]

            # Show what will be updated
            fields_updating = [k for k in update_fields.keys()]
            st.info(f"**Update:** {item_name} → {target}\n\nItem ID: `{item_id}` | Fields: {', '.join(fields_updating)}")

            confirm = st.checkbox(f"I confirm: update '{item_name}'")
            if confirm:
                if st.button("🚀 Push Content Now", type="primary", use_container_width=True):
                    with st.spinner("Pushing to Webflow..."):
                        # Build full payload
                        if push_live:
                            url = f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}/items/live"
                        else:
                            url = f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}/items"

                        payload = {
                            "items": [{
                                "id": item_id,
                                "fieldData": update_fields
                            }]
                        }
                        resp = requests.patch(url, headers=get_headers(api_token), json=payload)

                    if resp.status_code == 200:
                        st.success("✅ Updated successfully!")
                        st.balloons()
                        with st.expander("API Response"):
                            st.json(resp.json())
                    else:
                        st.error(f"❌ Failed — HTTP {resp.status_code}")
                        st.code(resp.text, language="json")

    else:  # Create New Blog
        if not new_name or not new_slug:
            st.warning("Title and Slug are required to create a new blog post.")
        else:
            # Build extra fields
            extra = {}
            if new_meta_title:
                extra["meta-title"] = new_meta_title
            if new_meta_desc:
                extra["meta-description"] = new_meta_desc
            if new_description:
                extra["description"] = new_description
            if new_canonical:
                extra["canonical-links"] = new_canonical
            elif new_slug:
                extra["canonical-links"] = f"https://www.edstellar.com/blog/{new_slug}"
            if new_primary_keyword:
                extra["primary-keyword"] = new_primary_keyword
            if new_keyword_volume:
                extra["keyword-search-volume"] = new_keyword_volume
            extra["new-format-blog"] = new_format_blog
            extra["faqs-section"] = new_faqs_section

            st.info(f"**Create:** {new_name}\n\nSlug: `{new_slug}` | Content: {stats['total_chars']:,} chars | Status: Draft")

            fields_summary = ", ".join(f"{k}" for k in extra.keys() if extra[k])
            st.caption(f"Extra fields: {fields_summary}")

            confirm = st.checkbox(f"I confirm: create new blog post '{new_name}'")
            if confirm:
                if st.button("🚀 Create Blog Post", type="primary", use_container_width=True):
                    with st.spinner("Creating in Webflow..."):
                        resp = create_new_item(api_token, new_name, new_slug, processed_html, extra)

                    if resp.status_code in (200, 201, 202):
                        st.success("✅ Blog post created as Draft!")
                        st.balloons()
                        with st.expander("API Response"):
                            st.json(resp.json())
                    else:
                        st.error(f"❌ Failed — HTTP {resp.status_code}")
                        st.code(resp.text, language="json")
