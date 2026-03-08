import json
import re


def format_default(value):
    """Return a properly quoted string for HTML attributes from any Python value."""
    # Convert to JSON then ensure it's properly quoted for HTML attributes
    json_value = json.dumps(value)
    # If the JSON value is already a string (starts and ends with quotes), return as-is
    # Otherwise, wrap it in quotes for HTML attribute safety
    if json_value.startswith('"') and json_value.endswith('"'):
        return json_value
    else:
        return f'"{json_value}"'


def escape_mdx_content(text):
    """Escape MDX-sensitive content like angle brackets that could be interpreted as HTML tags."""
    if not text or not isinstance(text, str):
        return text

    # List of known valid HTML/MDX tags that should not be escaped
    valid_html_tags = {
        "a",
        "abbr",
        "address",
        "area",
        "article",
        "aside",
        "audio",
        "b",
        "base",
        "bdi",
        "bdo",
        "blockquote",
        "body",
        "br",
        "button",
        "canvas",
        "caption",
        "cite",
        "code",
        "col",
        "colgroup",
        "data",
        "datalist",
        "dd",
        "del",
        "details",
        "dfn",
        "dialog",
        "div",
        "dl",
        "dt",
        "em",
        "embed",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "header",
        "hr",
        "html",
        "i",
        "iframe",
        "img",
        "input",
        "ins",
        "kbd",
        "label",
        "legend",
        "li",
        "link",
        "main",
        "map",
        "mark",
        "meta",
        "meter",
        "nav",
        "noscript",
        "object",
        "ol",
        "optgroup",
        "option",
        "output",
        "p",
        "param",
        "picture",
        "pre",
        "progress",
        "q",
        "rp",
        "rt",
        "ruby",
        "s",
        "samp",
        "script",
        "section",
        "select",
        "small",
        "source",
        "span",
        "strong",
        "style",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "template",
        "textarea",
        "tfoot",
        "th",
        "thead",
        "time",
        "title",
        "tr",
        "track",
        "u",
        "ul",
        "var",
        "video",
        "wbr",
        # Common MDX/JSX tags
        "ParamField",
        "RequestExample",
        "ResponseExample",
    }

    def escape_non_html_brackets(match):
        content = match.group(1).strip()
        # Extract just the tag name (before any whitespace or attributes)
        tag_name = content.split()[0] if content else ""

        # If it's a known valid HTML/MDX tag, leave it alone
        if tag_name.lower() in valid_html_tags:
            return match.group(0)
        # Otherwise, escape the brackets
        return f"&lt;{content}&gt;"

    # Replace <something> with &lt;something&gt; unless it's a valid HTML tag
    text = re.sub(r"<([^>]+)>", escape_non_html_brackets, text)

    return text
