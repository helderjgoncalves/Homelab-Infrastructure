"""Compose files as text: a service's block, and its image line changed with nothing else touched."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path

from releases import normalize_image
from runtime import split_ref

TOP = re.compile(r"services:[ \t]*(#.*)?")
KEY = re.compile(r"[\"']?([^\"'\s:#][^\"':]*?)[\"']?:[ \t]*(#.*)?")
IMAGE = re.compile(r"(?P<lead>[ \t]*image:[ \t]*)(?P<quote>[\"']?)(?P<ref>[^\s\"'#]+)(?P=quote)(?P<tail>[ \t]*(#.*)?)")
TAG = re.compile(r"(v?)(\d+(?:\.\d+)*)(-[A-Za-z][\w.-]*)?")


class EditError(ValueError):
    pass


@dataclass(frozen=True)
class Edit:
    path: str
    before: str
    after: str


def next_tag(tag: str, version: tuple[int, ...]) -> str | None:
    """The tag of the same shape for `version`: `2026.11` and 2026.12.1 make `2026.12`, `v3` and 4.0.2 make `v4`."""
    match = TAG.fullmatch(tag)
    if match is None:
        return None
    size = len(match.group(2).split("."))
    if len(version) < size:
        return None
    return match.group(1) + ".".join(str(part) for part in version[:size]) + (match.group(3) or "")


def retag(ref: str, tag: str) -> str:
    if "@" in ref:
        raise EditError(f"{ref} is pinned by digest")
    name, separator, old = ref.rpartition(":")
    if not separator or "/" in old:
        name = ref
    return f"{name}:{tag}"


def block(text: str, service: str) -> str:
    """The service's lines, its key included, or "" when the file does not define it."""
    lines = text.splitlines(keepends=True)
    span = _span(lines, service)
    return "".join(lines[span[0]:span[1]]).rstrip() + "\n" if span else ""


def plan(files: tuple[str, ...] | list[str], changes: list[tuple[str, str, str]]) -> list[Edit]:
    """The edits that move each (service, running image, new image) to its new image, read from every file of the project.

    Each service must set its image in exactly one file, on one plain `image:` line that names the running image.
    """
    texts = {path: Path(path).read_bytes().decode() for path in files}
    after = dict(texts)
    for service, old_ref, new_ref in changes:
        found = [(path, line) for path in files if (line := _image_line(after[path], service)) is not None]
        if not found:
            raise EditError(f"no file of the project sets the image of {service} on an image: line")
        if len(found) > 1:
            raise EditError(f"{service} sets its image in more than one file ({', '.join(Path(path).name for path, _line in found)})")
        path, index = found[0]
        lines = after[path].splitlines(keepends=True)
        line = lines[index]
        ending = line[len(line.rstrip("\r\n")):]
        match = IMAGE.fullmatch(line.rstrip("\r\n"))
        ref = match.group("ref") if match else ""
        if not match or "$" in ref or ref[0] in ">|&*!%{[":
            raise EditError(f"the image of {service} in {Path(path).name} is not a plain image:tag")
        if "@" in ref:
            raise EditError(f"the image of {service} in {Path(path).name} is pinned by digest")
        if _same(ref) != _same(old_ref):
            raise EditError(f"{Path(path).name} says {ref} for {service}, but it runs {old_ref}")
        # Only the tag changes; the file keeps its own spelling of the image (docker.io/library/…).
        written = retag(ref, split_ref(new_ref)[1]) if _same(split_ref(ref)[0]) == _same(split_ref(new_ref)[0]) else new_ref
        lines[index] = f"{match.group('lead')}{match.group('quote')}{written}{match.group('quote')}{match.group('tail')}{ending}"
        after[path] = "".join(lines)
    return [Edit(path, texts[path], after[path]) for path in files if after[path] != texts[path]]


def write(edits: list[Edit]) -> None:
    """In place, so each file keeps its owner and mode."""
    for edit in edits:
        Path(edit.path).write_bytes(edit.after.encode())


def revert(edits: list[Edit]) -> None:
    failed: OSError | None = None
    for edit in edits:
        try:
            Path(edit.path).write_bytes(edit.before.encode())
        except OSError as exc:
            failed = exc
    if failed is not None:
        raise failed


def only_images(before: dict, after: dict, images: dict[str, str]) -> bool:
    """Whether the resolved project after the edit differs from the one before only in these services' images."""
    expected = copy.deepcopy(before)
    try:
        for service, ref in images.items():
            got = after["services"][service]["image"]
            if _same(got) != _same(ref):
                return False
            expected["services"][service]["image"] = got
    except (KeyError, TypeError):
        return False
    return expected == after


def _same(ref: str) -> str:
    return normalize_image(ref).removeprefix("library/")


def _image_line(text: str, service: str) -> int | None:
    lines = text.splitlines(keepends=True)
    span = _span(lines, service)
    if span is None:
        return None
    start, end = span
    child = next((_depth(line) for line in lines[start + 1:end] if not _skip(line)), None)
    found = [index for index in range(start + 1, end) if not _skip(lines[index]) and _depth(lines[index]) == child and lines[index].lstrip().startswith("image:")]
    if len(found) > 1:
        raise EditError(f"{service} has more than one image: line")
    return found[0] if found else None


def _span(lines: list[str], service: str) -> tuple[int, int] | None:
    """[start, end) of the service's lines: its key under the top-level `services:` and everything indented under it."""
    top = next((index for index, line in enumerate(lines) if TOP.fullmatch(line.rstrip("\r\n"))), None)
    if top is None:
        return None
    indent = None
    for index in range(top + 1, len(lines)):
        line = lines[index]
        if _skip(line):
            continue
        depth = _depth(line)
        if depth == 0:
            return None
        if indent is None:
            indent = depth
        if depth != indent:
            continue
        match = KEY.fullmatch(line.strip())
        if match is None or match.group(1) != service:
            continue
        end = index + 1
        while end < len(lines) and (_skip(lines[end]) or _depth(lines[end]) > indent):
            end += 1
        while end > index + 1 and _skip(lines[end - 1]):
            end -= 1
        return index, end
    return None


def _skip(line: str) -> bool:
    text = line.strip()
    return not text or text.startswith("#")


def _depth(line: str) -> int:
    return len(line) - len(line.lstrip(" "))
