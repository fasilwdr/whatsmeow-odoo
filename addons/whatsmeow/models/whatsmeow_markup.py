"""WhatsApp's text markers, rendered to HTML.

WhatsApp's styles are a client-side *text* convention: the gateway carries the
body verbatim and the recipient's phone parses `*bold*`, `_italic_`, `~strike~`
and ```monospace```. Stored bodies therefore keep the markers — which is right,
because the markup is the message — but a chatter entry showing `*Hello*` makes
the reader do the phone's job.

This is the server-side twin of `renderPreview()` in
`whatsmeow_template/static/src/whatsmeow_markup.js`: the two must agree, since
an author composes against the preview and then reads the result in the log.
The grammar lives in both places for the same reason `SESSION_CODE_RE` and
`sessionNameRe` do — one runs where there is no server, the other where there is
no browser. Keep them in step; the tests on both sides cover the same cases.

Display-only and one-way: nothing is ever read back out of the HTML, so where
this disagrees with WhatsApp's own parser the phone wins and a chatter entry is
a little wrong, which costs nothing.

`linkify` is the one thing the twin does not do, and is off unless asked for:
a phone makes a URL tappable, and an inbound location is little more than its
map link, so the posts that show a *received* message turn it on. The composer
preview has no use for it — nobody clicks a link in their own draft.
"""
import re

from markupsafe import Markup, escape

# The inline marks, one line at a time and never containing their own marker —
# the same grammar `applyMark` emits, read back. `(?=\S)` / `\S` before the
# closing marker is WhatsApp's own adjacency rule: `*text *` is literal
# asterisks on a phone, so it must stay literal here too.
INLINE_RE = re.compile(r"([*_~])(?=\S)((?:(?!\1)[^\n])*?\S)\1")
INLINE_TAGS = {"*": "strong", "_": "em", "~": "s"}

# A URL in already-escaped text. It stops at whitespace and at an escaped quote
# or angle bracket, so the match can be dropped into an href as it stands: by
# the time this runs, `escape` has left nothing in it that could close the
# attribute. Trailing punctuation belongs to the sentence, not the address.
URL_RE = re.compile(r"https?://(?:(?!&(?:lt|gt|#34|#39);)\S)+")
URL_TRAILING = ".,;:!?)"

# Monospace is block-ish: it spans newlines and suppresses the inline marks
# inside it, so the fences are split off before anything else is looked for.
FENCE = "```"


def _render_inline(text):
    """Wrap every inline mark in `text`, nesting `*bold _and italic_*`.

    Iterative over the tail and recursive only into a match's own content, so
    the recursion is bounded by how deeply the marks nest rather than by how
    many of them a message contains.
    """
    parts = []
    while True:
        match = INLINE_RE.search(text)
        if not match:
            parts.append(text)
            return "".join(parts)
        tag = INLINE_TAGS[match.group(1)]
        parts.append(text[:match.start()])
        parts.append(f"<{tag}>{_render_inline(match.group(2))}</{tag}>")
        text = text[match.end():]


def _render_chunk(text, linkify):
    """Inline marks, and — when asked — URLs as links.

    A URL is cut out before the marks are looked for: the `_` in
    `/some_path_here` is part of an address, not an italic.
    """
    if not linkify:
        return _render_inline(text)
    parts, pos = [], 0
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(URL_TRAILING)
        if url.endswith("://"):
            continue  # a bare scheme is not a link
        parts.append(_render_inline(text[pos:match.start()]))
        parts.append(
            f'<a href="{url}" target="_blank" rel="noreferrer noopener">{url}</a>')
        pos = match.start() + len(url)
    parts.append(_render_inline(text[pos:]))
    return "".join(parts)


def _line_breaks(text):
    return text.replace("\n", "<br/>")


def render_markup(text, linkify=False):
    """`text` as a phone would draw it, as escaped HTML.

    The body is escaped first and the tags are a closed set, so an inbound
    message can never smuggle markup into a chatter entry — the same guarantee
    `Markup(...) % value` gives, kept while still emitting real formatting.
    """
    if not text:
        return Markup("")
    chunks = str(escape(text)).split(FENCE)
    html = []
    for index, chunk in enumerate(chunks):
        if index % 2 == 1 and index < len(chunks) - 1:
            html.append(f"<code>{_line_breaks(chunk)}</code>")
        else:
            # An odd chunk that is also the last one had an opening fence with
            # nothing to close it. `split` ate that fence, so it goes back as
            # the literal text the phone would show.
            opener = FENCE if index % 2 == 1 else ""
            html.append(opener + _line_breaks(_render_chunk(chunk, linkify)))
    return Markup("".join(html))
