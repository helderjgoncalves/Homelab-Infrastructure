"""Release notes for a question: the releases in between, the parts about breaking changes, and what in them names this setup."""

from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

from releases import USER_AGENT, Release, newer

# A body this short goes as it is. A longer one keeps its sections about breaking
# changes, and from the rest only the lines that mention one.
WHOLE = 4000
HEADING = re.compile(r"(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*")
BOLD = re.compile(r"\*\*([^*]{3,100})\*\*:?")
SECTION = re.compile(
    r"breaking|backward[- ]?incompatib|incompatib|deprecat|migrat|upgrad|remov|farewell|important|\bnotes?\b"
    r"|before (?:you )?updat|action required|heads.up|known issues|^changed:?$|config(?:uration)? changes?",
    re.I,
)
LINE = re.compile(
    r"breaking|incompatib|deprecat|migrat|remov|renam|no longer|dropp|drop support|end of life|\brequires?\b|\bmust\b"
    r"|\bmanual(?:ly)?\b|now defaults?|default (?:is|was) changed|changed the default",
    re.I,
)
LINK = re.compile(r"\[([^\]]+)\]\((https://[^)\s]+)\)")
BARE = re.compile(r"https://[^\s<>()\[\]\"'`]+")
LINKED = re.compile(r"release notes|migration guide|upgrade guide|upgrading|breaking", re.I)
# What a rollback can't take back. Each match is shown with its whole sentence.
UNDONE = re.compile(
    r"\b(?:irreversibl\w*|(?:can ?not|can't) be (?:undone|reverted|rolled back)|(?:can ?not|can't) (?:be )?downgrad\w*"
    r"|downgrad\w* (?:is|are|will be) not (?:supported|possible)|no (?:way|path) back)\b",
    re.I,
)
SENTENCE_END = re.compile(r"[.\n]")
ENV_KEY = re.compile(r"^[ \t]*-?[ \t]*[\"']?([A-Z][A-Z0-9_]+)[\"']?[ \t]*[=:]", re.M)
FLAG = re.compile(r"(?<![\w-])--([A-Za-z][\w.-]*\w)")
ANCHOR = re.compile(r" \{#[^}]*\}$")
FENCE = re.compile(r"[ \t]*(?:```|~~~)")
PAGE_LIMIT = 4 * 1024 * 1024
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class NotesError(ValueError):
    """The notes can't support a verdict, and asking again will not change that."""


def between(releases: list[Release], current: tuple[int, ...], target: tuple[int, ...]) -> list[Release]:
    """The stable releases after `current` up to `target`, each after its betas, oldest first. Raises when the list may be missing some."""
    stable = [release for release in releases if release.version and not release.prerelease and not release.draft]
    chosen = sorted((release for release in stable if newer(release.version, current) and not newer(release.version, target)), key=lambda release: _pad(release.version))
    if not any(_pad(release.version) == _pad(target) for release in chosen):
        raise NotesError(f"There are no release notes for {_text(target)}.")
    if not any(not newer(release.version, current) for release in stable):
        raise NotesError(f"The releases listed stop after {_text(current)}, so notes in between may be missing.")
    # The betas of a release shown: its own notes often list only what changed since the last of them.
    shown = {_pad(release.version) for release in chosen}
    early = [release for release in releases if release.prerelease and not release.draft and release.version and _pad(release.version) in shown]
    return sorted(chosen + early, key=lambda release: (_pad(release.version), not release.prerelease, release.published or EPOCH))


def relevant(text: str) -> str:
    """The parts of release notes about breaking changes, deprecations, removals and migrations, and a count of the lines left out."""
    text = text.strip()
    if len(text) <= WHOLE:
        return text
    kept: list[str] = []
    level = 0
    left_out = 0
    # The heading of the section being left out, kept above the first line of it that is kept.
    above = ""
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        # A `#` line in a code block is a comment, not a heading.
        depth, title = (0, "") if fenced or FENCE.match(line) else heading(line)
        if depth:
            if level and depth <= level:
                level = 0
            if not level and SECTION.search(title.strip()):
                level = depth
            above = "" if level else line
        if level:
            kept.append(line)
        elif not depth and LINE.search(line):
            if above:
                kept.append(above)
                left_out -= 1
                above = ""
            kept.append(line)
        elif line.strip():
            left_out += 1
    if left_out:
        kept.append(f"[{left_out} other lines left out: none mention a breaking change, deprecation, removal or migration]")
    return "\n".join(kept).strip()


def heading(line: str) -> tuple[int, str]:
    """(level, title) of a Markdown heading, or of a line that is all bold, which ranks below every heading. (0, "") otherwise."""
    text = line.strip()
    match = HEADING.fullmatch(text)
    if match:
        return len(match.group(1)), match.group(2)
    match = BOLD.fullmatch(text)
    if match:
        return 7, match.group(1)
    return 0, ""


def links(body: str) -> list[str]:
    """Pages the notes send the reader to for the rest: a link named release notes, migration or upgrade guide, or breaking changes.

    A bare URL counts when it is the whole body, or its line says one of those.
    """
    found = [url for label, url in LINK.findall(body) if LINKED.search(label)]
    plain = LINK.sub(" ", body)
    alone = plain.strip().rstrip(".!,;:")
    if BARE.fullmatch(alone):
        found.append(alone)
    for line in plain.splitlines():
        if LINKED.search(BARE.sub(" ", line)):
            found += [url.rstrip(".!,;:") for url in BARE.findall(line)]
    return list(dict.fromkeys(found))


def fetch_page(url: str) -> str:
    """A linked page as text, or only its section when the link names one (`#v380`)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html, text/markdown, text/plain"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(PAGE_LIMIT + 1)
        kind = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
    if len(data) > PAGE_LIMIT:
        raise NotesError(f"{url} is too large to read.")
    text = data.decode(charset, errors="replace")
    fragment = url.partition("#")[2]
    if kind == "text/html":
        return page_text(text, fragment)
    if kind.startswith("text/"):
        return text
    raise NotesError(f"{url} is {kind}, not a page.")


def page_text(html: str, fragment: str = "") -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = parser.text().splitlines()
    if fragment:
        lines = _section(lines, fragment)
    return "\n".join(ANCHOR.sub("", line) for line in lines).strip()


def named(text: str, terms, pattern: str = "{}") -> list[str]:
    """The terms the text names. `pattern` is how it names one, `{}` the term: `/integrations/{}/`. The default is the term as a whole word."""
    found = []
    for term in sorted(set(terms)):
        if pattern == "{}":
            hit = re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text) is not None
        else:
            hit = pattern.replace("{}", term) in text
        if hit:
            found.append(term)
    return found


def marked(text: str, marker: str, pattern: str, terms) -> list[str]:
    """The lines carrying `marker` that name one of `terms`, or name nothing: a change to the app itself.

    `pattern` is how a line names something, `{}` the name: `[{} docs]`. With no pattern, every marked line counts.
    """
    names = re.compile(re.escape(pattern).replace(r"\{\}", r"([\w.-]+)")) if pattern else None
    found = []
    for line in text.splitlines():
        if marker not in line:
            continue
        named = set(names.findall(line)) if names else set()
        if not named or named & set(terms):
            found.append(" ".join(line.split()).lstrip("-* ")[:200])
    return found


def compose_terms(block: str) -> set[str]:
    """What a compose service sets that notes would name: its environment keys and command-line flags."""
    terms = set(ENV_KEY.findall(block))
    for flag in FLAG.findall(block):
        terms.add(f"--{flag}")
        if "." in flag:
            terms.add(flag)
    return terms


def undone(text: str) -> list[str]:
    """Sentences saying a change can't be undone."""
    found = []
    for match in UNDONE.finditer(text):
        start = max(text.rfind(".", 0, match.start()), text.rfind("\n", 0, match.start())) + 1
        end = SENTENCE_END.search(text, match.end())
        # A long sentence is cut around what was found.
        cut = match.start() - 100 > start
        start, stop = max(start, match.start() - 100), min(end.start() if end else len(text), match.end() + 100)
        sentence = " ".join(text[start:stop].split()[cut:])
        if sentence not in found:
            found.append(sentence)
    return found


def _section(lines: list[str], fragment: str) -> list[str]:
    for index, line in enumerate(lines):
        depth, title = heading(line)
        if depth and title.endswith(f"{{#{fragment}}}"):
            end = next((later for later in range(index + 1, len(lines)) if 0 < heading(lines[later])[0] <= depth), len(lines))
            return lines[index:end]
    return lines


def _pad(version: tuple[int, ...]) -> tuple[int, ...]:
    return version + (0,) * (6 - len(version))


def _text(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


class _Text(HTMLParser):
    """HTML as Markdown-ish text: headings as `#` lines with their id, list items, link targets in brackets."""

    SKIP = {"script", "style", "svg", "noscript", "nav", "header", "footer", "aside", "template", "button", "form", "iconify-icon"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "article", "main", "ul", "ol", "table", "tr", "pre", "blockquote", "dd", "dt", "details", "hr", "br"}
    HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # Inside a skipped element: its tag, and how deep in that tag. Only that tag counts, since HTML may leave </li> or
        # </p> out.
        self.skip_tag = ""
        self.skipping = 0
        self.links: list[str] = []
        self.ids: list[str] = []
        self.pre = 0
        # A summary is one bold line, whatever blocks it holds.
        self.summary = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if self.skipping:
            self.skipping += tag == self.skip_tag
            return
        if (tag in self.SKIP or "tooltip" in (values.get("class") or "")) and tag not in self.VOID:
            self.skip_tag, self.skipping = tag, 1
            return
        if tag in self.HEADINGS:
            self.parts.append(f"\n{'#' * self.HEADINGS[tag]} ")
            self.ids.append(values.get("id") or "")
        elif tag == "summary":
            self.summary += 1
            self.parts.append("\n**")
        elif self.summary:
            pass
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "pre":
            self.pre += 1
        elif tag == "code" and not self.pre:
            self.parts.append("`")
        elif tag == "a":
            self.links.append(values.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if self.skipping:
            self.skipping -= tag == self.skip_tag
            return
        if tag in self.HEADINGS:
            anchor = self.ids.pop() if self.ids else ""
            self.parts.append(f" {{#{anchor}}}\n" if anchor else "\n")
        elif tag == "summary":
            self.summary = max(0, self.summary - 1)
            self.parts.append("**\n")
        elif self.summary:
            pass
        elif tag in self.BLOCK or tag == "li":
            self.parts.append("\n")
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        elif tag == "code" and not self.pre:
            self.parts.append("`")
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self.parts.append(f" ({href})")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        lines = []
        for raw in "".join(self.parts).splitlines():
            line = re.sub(r"[ \t]+", " ", raw).strip()
            line = re.sub(r"\*\* ?(.*?) ?\*\*", r"**\1**", line)
            if line or (lines and lines[-1]):
                lines.append(line)
        return "\n".join(lines)
