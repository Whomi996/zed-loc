#!/usr/bin/env python3
"""Generate a safer, more complete l10n JSON for build-time string replacement.

This script is intentionally conservative:
- Only fills EMPTY translations (value == "" or None)
- Only for a whitelist of UI-ish source paths (file path keys)
- Skips high-risk strings (identifiers, URLs, file paths, debug format specs)
- Uses Google translate anonymous endpoint (no API key)

It writes a new JSON file and never mutates the input.

Usage:
  python3 tools/auto_fill_l10n.py --input zh.json --output l10n.generated.json --max 250 --require-uppercase-start
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Tuple


DEFAULT_PREFIX_WHITELIST = [
    "zed/crates/assistant/src/",
    "zed/crates/assistant2/src/",
    "zed/crates/collab_ui/src/",
    "zed/crates/workspace/src/",
    "zed/crates/project_panel/src/",
    "zed/crates/search/src/",
    "zed/crates/file_finder/src/",
    "zed/crates/diagnostics/src/",
    "zed/crates/tasks_ui/src/",
    "zed/crates/zed/src/",
]

# Safe single-word UI labels worth translating even without uppercase-start rule.
SAFE_SINGLE_WORDS = {
    "OK",
    "Ok",
    "Cancel",
    "Search",
    "Find",
    "Replace",
    "Preferences",
    "Help",
    "About",
    "Yes",
    "No",
}


_RE_IDENTIFIER = re.compile(r"^[a-z0-9_.:/\\-]+$")
_RE_FILELIKE = re.compile(r"[\\/].+\.[A-Za-z0-9]{1,6}$")
_RE_DEBUG_FMT = re.compile(r"\{[^}]*[:?!][^}]*\}")
_RE_CAMEL_NO_SPACE = re.compile(r"^[A-Za-z0-9_]+$")
_RE_PUNCT_ONLY = re.compile(r"^\W+$", re.UNICODE)
_RE_NUMBER_ONLY = re.compile(r"^\d+(?:\.\d+)?$")

# Placeholder patterns we want to preserve verbatim.
_RE_PLACEHOLDERS = re.compile(
    r"(\{[^}]*\}|%\d*\$?[a-zA-Z]|\$\{[^}]+\})"
)


@dataclass
class Stats:
    scanned_empty: int = 0
    eligible: int = 0
    filled: int = 0
    skipped_high_risk: int = 0
    skipped_not_whitelisted: int = 0
    skipped_not_uiish: int = 0
    skipped_translation_failed: int = 0
    skipped_no_chinese: int = 0


def _uppercase_start(s: str) -> bool:
    t = s.lstrip()
    return bool(t) and "A" <= t[0] <= "Z"


def _contains_letters(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]", s))


def _is_high_risk(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if len(t) > 180:
        return True
    if "://" in t or t.startswith("mailto:"):
        return True
    if _RE_FILELIKE.search(t):
        return True
    if _RE_IDENTIFIER.fullmatch(t):
        return True
    if any(x in t for x in ("::", "->", "=>")):
        return True
    if _RE_DEBUG_FMT.search(t):
        return True
    if _RE_PUNCT_ONLY.fullmatch(t) or _RE_NUMBER_ONLY.fullmatch(t):
        return True
    # CamelCase / PascalCase single token is often an identifier.
    if " " not in t and _RE_CAMEL_NO_SPACE.fullmatch(t) and re.search(r"[a-z]", t) and re.search(r"[A-Z]", t):
        return True
    return False


def _mask_placeholders(text: str) -> Tuple[str, List[str]]:
    placeholders: List[str] = []

    def repl(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        return f"__PH{len(placeholders)-1}__"

    masked = _RE_PLACEHOLDERS.sub(repl, text)
    return masked, placeholders


def _unmask_placeholders(text: str, placeholders: List[str]) -> Tuple[bool, str]:
    out = text
    for i, ph in enumerate(placeholders):
        token = f"__PH{i}__"
        if token not in out:
            return False, text
        out = out.replace(token, ph)
    # Ensure no placeholder tokens remain.
    if "__PH" in out:
        return False, out
    return True, out


def _google_translate(text: str, *, source: str = "en", target: str = "zh-CN", timeout: int = 20) -> str:
    # Anonymous Google endpoint. Response is JSON.
    q = urllib.parse.quote(text)
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl={urllib.parse.quote(source)}&tl={urllib.parse.quote(target)}&dt=t&q={q}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    # data[0] is list of segments: [[translated, original, ...], ...]
    segs = data[0] or []
    return "".join(seg[0] for seg in segs if seg and seg[0])


def _translate_with_retries(text: str, cache: Dict[str, str], *, sleep_s: float = 0.15) -> Tuple[bool, str]:
    if text in cache:
        return True, cache[text]

    # polite pacing to reduce rate limiting
    time.sleep(sleep_s)

    last_err = None
    for attempt in range(3):
        try:
            out = _google_translate(text)
            out = (out or "").strip()
            if out:
                cache[text] = out
                return True, out
            last_err = ValueError("empty translation")
        except Exception as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))

    return False, f"{last_err}"  # caller counts as failed


def _path_whitelisted(file_path: str, prefixes: List[str]) -> bool:
    return any(file_path.startswith(p) for p in prefixes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Input l10n JSON file path")
    ap.add_argument("--output", required=True, help="Output l10n JSON file path")
    ap.add_argument("--max", type=int, default=250, help="Max entries to fill")
    ap.add_argument(
        "--require-uppercase-start",
        action="store_true",
        help="Only translate strings starting with [A-Z] (more conservative)",
    )
    ap.add_argument(
        "--prefix",
        action="append",
        default=[],
        help="Whitelisted file path prefix (repeatable). If omitted, uses defaults.",
    )
    args = ap.parse_args()

    prefixes = args.prefix or list(DEFAULT_PREFIX_WHITELIST)

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    stats = Stats()
    cache: Dict[str, str] = {}

    filled = 0

    # We keep original ordering to avoid noisy diffs when input is committed.
    for file_path, mapping in data.items():
        if not isinstance(mapping, dict):
            continue

        if not _path_whitelisted(file_path, prefixes):
            # Still count empties but do not attempt translation.
            for _, v in mapping.items():
                if v is None or v == "":
                    stats.scanned_empty += 1
                    stats.skipped_not_whitelisted += 1
            continue

        for original, translated in list(mapping.items()):
            if translated not in (None, ""):
                continue

            stats.scanned_empty += 1

            src = original
            if not _contains_letters(src):
                stats.skipped_high_risk += 1
                continue

            if _is_high_risk(src):
                stats.skipped_high_risk += 1
                continue

            uiish = _uppercase_start(src) or (src.strip() in SAFE_SINGLE_WORDS)
            if args.require_uppercase_start and not uiish:
                stats.skipped_not_uiish += 1
                continue

            stats.eligible += 1

            masked, placeholders = _mask_placeholders(src)
            ok, out = _translate_with_retries(masked, cache)
            if not ok:
                stats.skipped_translation_failed += 1
                continue

            ok2, unmasked = _unmask_placeholders(out, placeholders)
            if not ok2:
                stats.skipped_translation_failed += 1
                continue

            unmasked = unmasked.strip()
            if not re.search(r"[\u4e00-\u9fff]", unmasked):
                stats.skipped_no_chinese += 1
                continue

            mapping[original] = unmasked
            filled += 1
            stats.filled += 1

            if filled >= args.max:
                break

        if filled >= args.max:
            break

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("auto_fill_l10n stats:")
    print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
    print(f"wrote: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
