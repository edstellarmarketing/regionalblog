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

# CSS classes that indicate embed blocks
EMBED_CLASSES = {
    "takeaway",
    "criteria", "crit-item", "crit-icon", "crit-header",
    "table-scroll", "comp-table", "rank",
    "co-card", "co-hdr", "co-logo", "meta-row", "chip",
    "insight", "insight-icon", "ename", "author-del",
    "nl-card", "nl-grid", "nl-stat", "nl-num", "nl-source", "nl-p",
    "testimonial", "linkedin-topic", "div-flex", "testimonial-image",
    "linked-in", "author-name", "name-flex", "author-pos",
    "faq-item", "faq-question", "faq-answer", "toggle-icon",
    "cta", "bg-green", "cta-btn",
}

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
    body_match = re.search(r'<body[^>]*>(.*?)</body>', html_content, re.DOTALL)
    if body_match:
        html_content = body_match.group(1)
    return html_content.strip()


# ─── BLOCK CLASSIFIER ────────────────────────────────────────────────────────

def has_embed_class(tag):
    classes = tag.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    return bool(set(classes) & EMBED_CLASSES)


def is_embed_block(tag):
    if not isinstance(tag, Tag):
        return False
    if tag.name == "style":
        return True
    if tag.name == "div" and tag.get("class"):
        return True
    if tag.name == "table" and tag.get("class"):
        return True
    if has_embed_class(tag):
        return True
    if tag.name == "div":
        for desc in tag.descendants:
            if isinstance(desc, Tag) and has_embed_class(desc):
                return True
    return False


def unwrap_containers(soup):
    """
    Unwrap generic container tags (article, main, section, div without embed class)
    to get to the actual content blocks inside. Recursively unwraps nested containers.
    """
    # Tags that are generic wrappers (not embed content)
    WRAPPER_TAGS = {"article", "main", "section", "header", "footer", "aside", "nav"}

    children = list(soup.children)

    # If there's exactly one child and it's a wrapper tag, unwrap it
    real_children = [c for c in children if isinstance(c, Tag) or
                     (isinstance(c, NavigableString) and str(c).strip())]

    if len(real_children) == 1 and isinstance(real_children[0], Tag):
        child = real_children[0]
        # Unwrap known wrapper tags
        if child.name in WRAPPER_TAGS:
            return unwrap_containers(child)
        # Unwrap <div> WITHOUT embed classes (generic wrapper div)
        if child.name == "div" and not has_embed_class(child) and not child.get("class"):
            return unwrap_containers(child)

    return soup


def process_element(element, blocks, pending_style_ref):
    """Process a single element and append to blocks list."""
    pending_style = pending_style_ref[0]

    if isinstance(element, NavigableString):
        text = str(element).strip()
        if text and text not in ('\n', '\r\n'):
            if '<div' in text or '<table' in text or '<style' in text:
                sub_soup = BeautifulSoup(text, "html.parser")
                for sub_el in sub_soup.children:
                    if isinstance(sub_el, Tag):
                        if is_embed_block(sub_el):
                            blocks.append(("embed", str(sub_el)))
                        else:
                            blocks.append(("plain", str(sub_el)))
                    elif isinstance(sub_el, NavigableString) and str(sub_el).strip():
                        blocks.append(("plain", str(sub_el)))
        return

    if not isinstance(element, Tag):
        return

    el_html = str(element)

    # <style> → merge with next embed
    if element.name == "style":
        pending_style_ref[0] = el_html
        return

    # Top-level embed
    if is_embed_block(element):
        embed_html = el_html
        if pending_style_ref[0]:
            embed_html = pending_style_ref[0] + "\n" + embed_html
            pending_style_ref[0] = None
        blocks.append(("embed", embed_html))
        return

    # <p> containing embed children (parser absorbed divs into p)
    if element.name == "p":
        has_inner_embeds = False
        for child in element.children:
            if isinstance(child, Tag) and is_embed_block(child):
                has_inner_embeds = True
                break

        if has_inner_embeds:
            current_plain = []
            for child in element.children:
                if isinstance(child, Tag) and is_embed_block(child):
                    if current_plain:
                        plain_html = "".join(str(c) for c in current_plain).strip()
                        if plain_html and plain_html not in ("<br/>", "<br>", ""):
                            blocks.append(("plain", f"<p>{plain_html}</p>"))
                        current_plain = []
                    embed_html = str(child)
                    if pending_style_ref[0]:
                        embed_html = pending_style_ref[0] + "\n" + embed_html
                        pending_style_ref[0] = None
                    blocks.append(("embed", embed_html))
                else:
                    current_plain.append(child)
            if current_plain:
                plain_html = "".join(str(c) for c in current_plain).strip()
                if plain_html and plain_html not in ("<br/>", "<br>", ""):
                    blocks.append(("plain", f"<p>{plain_html}</p>"))
            return

    # Flush pending style
    if pending_style_ref[0]:
        blocks.append(("embed", pending_style_ref[0]))
        pending_style_ref[0] = None

    # Plain rich text
    blocks.append(("plain", el_html))


def split_into_blocks(html_content):
    html_content = normalize_html(html_content)
    soup = BeautifulSoup(html_content, "html.parser")

    # Unwrap generic containers (article, main, section, etc.)
    soup = unwrap_containers(soup)

    blocks = []
    pending_style_ref = [None]  # mutable ref for nested function

    for element in soup.children:
        process_element(element, blocks, pending_style_ref)

    if pending_style_ref[0]:
        blocks.append(("embed", pending_style_ref[0]))

    return blocks


def classify_and_wrap(html_content):
    blocks = split_into_blocks(html_content)

    output_parts = []
    embed_count = 0
    plain_count = 0
    warnings = []

    for block_type, block_html in blocks:
        if block_type == "embed":
            if len(block_html) > EMBED_CHAR_LIMIT:
                soup = BeautifulSoup(block_html, "html.parser")
                first_tag = soup.find()
                class_name = " ".join(first_tag.get("class", [])) if first_tag else "unknown"
                warnings.append({
                    "block": f"{first_tag.name if first_tag else '?'}.{class_name}",
                    "chars": len(block_html),
                    "preview": block_html[:150] + "..."
                })

            wrapped = f'<div data-rt-embed-type="html">\n{block_html}\n</div>'
            output_parts.append(wrapped)
            embed_count += 1
        else:
            stripped = block_html.strip()
            if stripped and stripped not in ("<p></p>", "<p> </p>", "<br/>", "<br>"):
                output_parts.append(stripped)
                plain_count += 1

    processed_html = "\n".join(output_parts)

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

# ── Test Push Section ──
with st.expander("🧪 Test Push (verify embed format)"):
    st.markdown("Push a sample embed block to a test blog post to verify `data-rt-embed-type` works correctly.")

    # Debug: Show collection field slugs
    if api_token:
        if st.button("🔎 Show Collection Field Slugs", key="show_fields_btn"):
            with st.spinner("Fetching collection schema..."):
                resp = requests.get(f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}",
                                    headers=get_headers(api_token))
            if resp.status_code == 200:
                col_data = resp.json()
                fields = col_data.get("fields", [])
                st.markdown("**All fields in Blog Posts collection:**")
                for f in fields:
                    ftype = f.get("type", "?")
                    slug = f.get("slug", "?")
                    name = f.get("displayName", "?")
                    marker = "👈 **THIS ONE**" if ftype == "RichText" else ""
                    st.markdown(f"- `{slug}` → {name} ({ftype}) {marker}")

                # Store the rich text field slug
                rt_fields = [f["slug"] for f in fields if f.get("type") == "RichText"]
                if rt_fields:
                    st.session_state["content_field_slug"] = rt_fields[0]
                    st.success(f"Rich text field slug: **`{rt_fields[0]}`**")
                else:
                    st.warning("No RichText field found!")
            else:
                st.error(f"Failed: {resp.status_code}")
                st.code(resp.text)

    st.divider()

    test_slug = st.text_input("Test blog post slug", value="test-2", key="test_slug",
                               help="Slug of the blog post to use for testing")

    # Use discovered field slug or default
    content_field = st.session_state.get("content_field_slug", "content")
    st.caption(f"Using field slug: `{content_field}` (click 'Show Field Slugs' above to verify)")

    test_content_option = st.radio("Test content:", [
        "Embed only (takeaway block)",
        "Plain + Embed mix",
        "Plain only (simple paragraph)",
    ], key="test_option", horizontal=True)

    if test_content_option == "Embed only (takeaway block)":
        test_html = '''<div data-rt-embed-type="html">
<div class='takeaway'>  <p>💡 KEY TAKEAWAYS</p>  <ul>    <li>      Edstellar is the best corporate training company in New Zealand with 2,000+ corporate training courses in NZ and 5,000+ trainers across technical, leadership, and behavioural domains.    </li>    <li>      Lumify Work is New Zealand's largest corporate IT training provider and Microsoft NZ's most strategic Learning Partner, training 5,000+ students per year.    </li>    <li>      Skillset NZ stands out for its exclusively B2B model serving large and medium organisations for 30+ years, with verified clients including WorkSafe NZ.    </li>    <li>      Companies were evaluated on trainer quality, NZQA and regulatory alignment, SME and geographic reach beyond Auckland, and post-training support.    </li>  </ul></div>
</div>'''
    elif test_content_option == "Plain + Embed mix":
        test_html = '''<h2>Test Heading — Plain Rich Text</h2>
<p>This is a plain paragraph with <strong>bold text</strong> and a <a href="https://www.edstellar.com">link to Edstellar</a>. This should appear as normal rich text in Webflow.</p>
<div data-rt-embed-type="html">
<div class='takeaway'>  <p>💡 KEY TAKEAWAYS</p>  <ul>    <li>      Edstellar is the best corporate training company in New Zealand with 2,000+ corporate training courses.    </li>    <li>      This block should appear as a Code Embed in Webflow editor.    </li>  </ul></div>
</div>
<p>This is another plain paragraph after the embed. It should appear as normal rich text.</p>'''
    else:
        test_html = '''<h2>Test Heading</h2>
<p>This is a simple test paragraph. If you can see this in the Webflow editor, the field slug is correct.</p>
<p>Second paragraph with <strong>bold</strong> and <a href="https://www.edstellar.com">a link</a>.</p>'''

    st.code(test_html[:500] + ("..." if len(test_html) > 500 else ""), language="html")
    st.caption(f"Content size: {len(test_html):,} chars")

    if api_token and test_slug:
        if st.button("🧪 Push Test Content", key="test_push_btn"):
            with st.spinner(f"Finding '{test_slug}' and pushing test content..."):
                item, error = search_item_by_slug(api_token, test_slug)
            if error:
                st.error(error)
            else:
                item_id = item["id"]
                item_name = item["fieldData"].get("name", "?")
                st.caption(f"Found: {item_name} (ID: {item_id})")

                # Use the correct field slug
                payload = {
                    "items": [{
                        "id": item_id,
                        "fieldData": {
                            content_field: test_html
                        }
                    }]
                }

                with st.spinner("Pushing..."):
                    resp = requests.patch(
                        f"{WEBFLOW_API_BASE}/collections/{COLLECTION_ID}/items",
                        headers=get_headers(api_token),
                        json=payload
                    )

                if resp.status_code == 200:
                    st.success(f"✅ Test content pushed to '{item_name}' using field `{content_field}`!")
                    with st.expander("API Response"):
                        st.json(resp.json())
                else:
                    st.error(f"❌ Failed — HTTP {resp.status_code}")
                    st.code(resp.text, language="json")
    elif not api_token:
        st.warning("Enter API token in sidebar first.")

st.divider()

# Upload
uploaded_file = st.file_uploader("📄 Upload Blog HTML", type=["html", "htm"])

if uploaded_file:
    raw_html = uploaded_file.read().decode("utf-8")
    st.caption(f"Loaded **{uploaded_file.name}** — {len(raw_html):,} characters")

    with st.spinner("Processing HTML..."):
        processed_html, stats = classify_and_wrap(raw_html)
        st.session_state["processed_html"] = processed_html
        st.session_state["stats"] = stats

if "stats" in st.session_state and "processed_html" in st.session_state:
    stats = st.session_state["stats"]
    processed_html = st.session_state["processed_html"]

    # Stats
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Blocks", stats["total_blocks"])
    c2.metric("🟢 Plain", stats["plain_blocks"])
    c3.metric("🟡 Embeds", stats["embed_blocks"])
    c4.metric("Total Size", f"{stats['total_chars']:,} ch")

    if stats["warnings"]:
        st.warning(f"⚠️ {len(stats['warnings'])} embed(s) exceed {EMBED_CHAR_LIMIT:,} char limit!")
        for w in stats["warnings"]:
            st.error(f"**{w['block']}** — {w['chars']:,} chars (limit: {EMBED_CHAR_LIMIT:,})")

    # Tabs
    tab_blocks, tab_source, tab_download = st.tabs(["📊 Block Analysis", "💻 Source HTML", "📥 Download"])

    with tab_blocks:
        block_soup = BeautifulSoup(processed_html, "html.parser")
        idx = 0
        for element in block_soup.children:
            if isinstance(element, NavigableString):
                continue
            if not isinstance(element, Tag):
                continue
            idx += 1
            is_embed = element.get("data-rt-embed-type") == "html"
            char_count = len(str(element))
            preview = element.get_text()[:100].replace("\n", " ").strip()

            if is_embed:
                inner = element.decode_contents().strip()
                with st.expander(f"🟡 **Block {idx}** — EMBED ({char_count:,} chars) | {preview[:60]}..."):
                    st.code(inner[:3000] + ("..." if len(inner) > 3000 else ""), language="html")
                    if char_count > EMBED_CHAR_LIMIT:
                        st.error(f"⚠️ Exceeds {EMBED_CHAR_LIMIT:,} char limit!")
            else:
                with st.expander(f"🟢 **Block {idx}** — PLAIN `<{element.name}>` ({char_count:,} chars) | {preview[:60]}"):
                    st.markdown(str(element), unsafe_allow_html=True)
                    st.code(str(element)[:1000], language="html")

    with tab_source:
        st.code(processed_html[:15000] + ("\n\n... [TRUNCATED]" if len(processed_html) > 15000 else ""),
                language="html")

    with tab_download:
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

            st.info(f"**Update:** {item_name} → {target}\n\nItem ID: `{item_id}` | Content: {stats['total_chars']:,} chars")

            confirm = st.checkbox(f"I confirm: update '{item_name}' content field")
            if confirm:
                if st.button("🚀 Push Content Now", type="primary", use_container_width=True):
                    with st.spinner("Pushing to Webflow..."):
                        resp = update_item_content(api_token, item_id, processed_html, live=push_live)

                    if resp.status_code == 200:
                        st.success("✅ Content updated successfully!")
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
