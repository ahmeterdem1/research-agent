import re

from config.settings import get_logger

logger = get_logger(__name__)

_SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "into", "is", "it", "of", "on", "or", "not", "paper", "papers",
    "that", "the", "their", "this", "to", "via", "with",
}


def _stem_word(word: str) -> str:
    """Lightweight suffix stripper so 'convergence' matches 'converging'/'converge', etc."""
    if len(word) < 5:
        return word
    for suffix in (
        "ational", "fulness", "ousness", "iveness", "izations", "ization",
        "ations", "ation", "ometries", "ometry", "ences", "ence", "ances", "ance",
        "ments", "ment", "ings", "ing", "ities", "ity", "nesses", "ness",
        "ives", "ive", "ally", "als", "al", "ers", "er", "ies", "es", "ed", "s",
    ):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _extract_word_stems(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stems = set(words)
    for w in words:
        if len(w) >= 5:
            stems.add(_stem_word(w))
    return stems


def _parse_lucene_query(query: str) -> dict:
    """
    Parse a natural-language or Lucene-style query into:
    - `phrases`: quoted phrases (`"platonic representation"`)
    - `required`: `+term` tokens
    - `excluded`: `-term` or `NOT term` tokens
    - `tokens`: content keywords
    - `is_or`: True if explicit `OR` operator is used
    """
    phrases = [p.strip().lower() for p in re.findall(r'"([^"]+)"', query) if p.strip()]
    remainder = re.sub(r'"[^"]*"', " ", query)
    raw_parts = remainder.split()

    required: list[str] = []
    excluded: list[str] = []
    tokens: list[str] = []
    all_cleaned: list[str] = []
    is_or = False

    skip_next = False
    for idx, part in enumerate(raw_parts):
        if skip_next:
            skip_next = False
            continue
        upper = part.upper()
        if upper == "OR":
            is_or = True
            continue
        if upper == "AND":
            continue
        if upper == "NOT" and idx + 1 < len(raw_parts):
            nxt = re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9*]+$", "", raw_parts[idx + 1]).lower().rstrip("*")
            if nxt:
                excluded.append(nxt)
            skip_next = True
            continue

        if part.startswith("-") and len(part) > 1:
            clean_ex = re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9*]+$", "", part[1:]).lower().rstrip("*")
            if clean_ex:
                excluded.append(clean_ex)
            continue

        is_req = part.startswith("+") and len(part) > 1
        raw_tok = part[1:] if is_req else part
        # Support field prefixes like title:platonic or summary:convergence by keeping the term
        if ":" in raw_tok and not raw_tok.lower().startswith(("10.", "http", "arxiv")):
            _, _, after_colon = raw_tok.partition(":")
            if after_colon:
                raw_tok = after_colon

        clean_tok = re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9.*_/-]+$", "", raw_tok).lower().rstrip("*")
        if not clean_tok:
            continue

        all_cleaned.append(clean_tok)
        if is_req:
            required.append(clean_tok)
            tokens.append(clean_tok)
        elif clean_tok not in _SEARCH_STOPWORDS:
            tokens.append(clean_tok)

    if not tokens and all_cleaned:
        tokens = all_cleaned

    return {
        "phrases": phrases,
        "required": required,
        "excluded": excluded,
        "tokens": tokens,
        "is_or": is_or,
    }


class VTreeManager:
    def __init__(self, vtree: dict):
        self.vtree = vtree

    def _get_node(self, vpath: str) -> dict | None:
        if vpath == "/" or vpath == "":
            return self.vtree.get("root")

        parts = [p for p in vpath.split("/") if p]
        curr = self.vtree.get("root")

        for part in parts:
            if not curr or curr.get("type") not in ("directory", "book"):
                return None
            found = False
            for child in curr.get("children", []):
                if child.get("name") == part:
                    curr = child
                    found = True
                    break
            if not found:
                return None
        return curr

    def ensure_dir(self, vpath: str) -> dict:
        """Ensure that a virtual directory path exists, creating intermediate directories as needed."""
        if vpath in ("/", ""):
            return self.vtree["root"]

        parts = [p for p in vpath.split("/") if p]
        curr = self.vtree["root"]
        built_parts: list[str] = []

        for part in parts:
            built_parts.append(part)
            curr_vpath = "/" + "/".join(built_parts)
            if "children" not in curr:
                curr["children"] = []
            existing = None
            for child in curr["children"]:
                if child.get("name") == part and child.get("type") == "directory":
                    existing = child
                    break
            if existing is None:
                existing = {
                    "type": "directory",
                    "name": part,
                    "virtual_path": curr_vpath,
                    "summary": "User created directory.",
                    "children": [],
                }
                curr["children"].append(existing)
                logger.info("Created virtual directory '%s'", curr_vpath)
            curr = existing
        return curr

    def add_document(self, target_dir_vpath: str, doc_node: dict) -> dict:
        """
        Insert or update a document node inside ``target_dir_vpath`` and mark
        ancestor directories for summary refresh.
        """
        dir_node = self.ensure_dir(target_dir_vpath)
        name = doc_node["name"]
        prefix = "" if target_dir_vpath.rstrip("/") == "" else target_dir_vpath.rstrip("/")
        full_vpath = f"{prefix}/{name}"
        doc_node = {**doc_node, "type": doc_node.get("type", "document"), "virtual_path": full_vpath}
        if "children" in doc_node and isinstance(doc_node["children"], list):
            doc_node["children"] = [
                {**c, "virtual_path": f"{full_vpath}/{c['name']}"} for c in doc_node["children"]
            ]

        if "children" not in dir_node:
            dir_node["children"] = []

        replaced = False
        for idx, child in enumerate(dir_node["children"]):
            if child.get("name") == name:
                dir_node["children"][idx] = doc_node
                replaced = True
                break
        if not replaced:
            dir_node["children"].append(doc_node)

        self.update_ancestor_summaries(full_vpath, doc_node.get("summary", ""))
        logger.info("%s document '%s' in VTree", "Updated" if replaced else "Added", full_vpath)
        return doc_node

    def ls(self, vpath: str) -> str:
        """List contents of a directory."""
        node = self._get_node(vpath)
        if not node:
            return f"Error: Path '{vpath}' not found."

        if node.get("type") not in ("directory", "book"):
            return f"Error: '{vpath}' is not a directory."

        children = node.get("children", [])
        if not children:
            return "Empty directory."

        result = []
        for c in children:
            t = c.get("type", "unknown")
            name = c.get("name", "unknown")
            summary = c.get("summary", "")
            result.append(f"[{t}] {name} - {summary[:100]}...")

        return "\n".join(result)

    def mkdir(self, vpath: str) -> str:
        """Create a new directory at the specified virtual path."""
        parts = [p for p in vpath.split("/") if p]
        if not parts:
            return "Error: Invalid path."

        parent_vpath = "/" + "/".join(parts[:-1])
        new_dir_name = parts[-1]

        parent_node = self._get_node(parent_vpath)
        if not parent_node:
            return f"Error: Parent path '{parent_vpath}' does not exist."

        if parent_node.get("type") != "directory":
            return f"Error: Parent '{parent_vpath}' is not a directory."

        for child in parent_node.get("children", []):
            if child.get("name") == new_dir_name:
                return f"Error: '{new_dir_name}' already exists in '{parent_vpath}'."

        new_dir = {
            "type": "directory",
            "name": new_dir_name,
            "virtual_path": "/" + "/".join(parts),
            "summary": "User created directory.",
            "children": [],
        }

        if "children" not in parent_node:
            parent_node["children"] = []
        parent_node["children"].append(new_dir)
        logger.info("mkdir created '%s'", vpath)
        return f"Successfully created directory '{vpath}'."

    def mv(self, src: str, dest: str) -> str:
        """Move a file or directory from src to dest."""
        if src == "/" or dest == "/":
            return "Error: Cannot move root directory."

        src_parts = [p for p in src.split("/") if p]
        dest_parts = [p for p in dest.split("/") if p]

        if not src_parts or not dest_parts:
            return "Error: Invalid paths."

        norm_src = "/" + "/".join(src_parts)
        norm_dest = "/" + "/".join(dest_parts)
        if norm_dest == norm_src or norm_dest.startswith(norm_src + "/"):
            return f"Error: Cannot move '{src}' into itself or its own subdirectory '{dest}'."

        src_parent_vpath = "/" + "/".join(src_parts[:-1])
        src_name = src_parts[-1]

        dest_parent_vpath = "/" + "/".join(dest_parts[:-1])
        dest_name = dest_parts[-1]

        src_parent = self._get_node(src_parent_vpath)
        if not src_parent:
            return f"Error: Source parent '{src_parent_vpath}' not found."

        dest_parent = self._get_node(dest_parent_vpath)
        if not dest_parent:
            return f"Error: Destination parent '{dest_parent_vpath}' not found."

        if dest_parent.get("type") != "directory":
            return f"Error: Destination parent '{dest_parent_vpath}' is not a directory."

        src_node = None
        src_idx = -1
        for i, child in enumerate(src_parent.get("children", [])):
            if child.get("name") == src_name:
                src_node = child
                src_idx = i
                break

        if not src_node:
            return f"Error: Source '{src}' not found."

        for child in dest_parent.get("children", []):
            if child.get("name") == dest_name:
                return f"Error: Destination '{dest}' already exists."

        src_parent["children"].pop(src_idx)

        def update_paths(node, new_parent_path, new_name):
            node["name"] = new_name
            node["virtual_path"] = f"{new_parent_path.rstrip('/')}/{new_name}"
            if "children" in node:
                for c in node["children"]:
                    update_paths(c, node["virtual_path"], c["name"])

        update_paths(src_node, dest_parent_vpath, dest_name)

        if "children" not in dest_parent:
            dest_parent["children"] = []
        dest_parent["children"].append(src_node)

        # Mark both the source parent chain and destination parent chain for summary refresh
        self.update_ancestor_summaries(norm_src, "")
        self.update_ancestor_summaries(norm_dest, src_node.get("summary", ""))
        logger.info("Moved VTree node '%s' -> '%s'", src, dest)
        return f"Successfully moved '{src}' to '{dest}'."

    def rm(self, vpath: str) -> str:
        """Remove a document, book, or directory at ``vpath`` and mark ancestors for summary refresh."""
        parts = [p for p in vpath.split("/") if p]
        if not parts:
            return "Error: Cannot remove root directory '/'."

        norm_vpath = "/" + "/".join(parts)
        parent_vpath = "/" + "/".join(parts[:-1])
        target_name = parts[-1]

        parent_node = self._get_node(parent_vpath)
        if not parent_node or "children" not in parent_node:
            return f"Error: Path '{vpath}' not found."

        target_idx = -1
        for i, child in enumerate(parent_node["children"]):
            if child.get("name") == target_name:
                target_idx = i
                break

        if target_idx == -1:
            return f"Error: Path '{vpath}' not found."

        removed = parent_node["children"].pop(target_idx)
        self.update_ancestor_summaries(norm_vpath, "")
        logger.info("Removed VTree node '%s' (type=%s)", norm_vpath, removed.get("type"))
        return f"Successfully removed [{removed.get('type', 'node')}] '{norm_vpath}'."

    def search_library(self, query: str) -> list[dict]:
        """
        Lucene / BM25-style ranked tokenized search across the VTree (`name`, `virtual_path`,
        `doi`, and `summary`).

        Supports:
        - Multi-keyword natural queries (e.g. `'platonic representation hypothesis convergence'`)
          without requiring all words to appear as a single contiguous substring.
        - Quoted phrases (`"platonic representation"`), required terms (`+platonic`),
          excluded terms (`-survey` or `NOT survey`), boolean `OR`, and prefix/stem matching
          (`convergence` matches `converging` / `converge`).
        - Field-weighted scoring (`doi` > `name` > `virtual_path` > `summary`), returning
          matching nodes sorted from highest to lowest relevance score.
        """
        q_raw = (query or "").strip()
        if not q_raw:
            return []

        q_lower = q_raw.lower()
        parsed = _parse_lucene_query(q_raw)
        phrases: list[str] = parsed["phrases"]
        required: list[str] = parsed["required"]
        excluded: list[str] = parsed["excluded"]
        tokens: list[str] = parsed["tokens"]
        is_or: bool = parsed["is_or"]

        primary_hits: list[tuple[float, int, int, int, dict]] = []
        fallback_hits: list[tuple[float, int, int, int, dict]] = []

        def _token_matches_field(tok: str, tok_stem: str, field_str: str, field_stems: set[str]) -> tuple[bool, bool]:
            """Return `(exact_match, stem_match)` for `tok` in `field_str` / `field_stems`."""
            if tok in field_str:
                return True, True
            if len(tok) >= 5 and (tok_stem in field_stems or tok_stem in field_str):
                return False, True
            return False, False

        def search(node: dict) -> None:
            if not isinstance(node, dict):
                return

            ntype = node.get("type")
            vpath = node.get("virtual_path", "")
            if ntype in ("document", "book", "directory") and not (
                vpath in ("/", "") and (node.get("name") or "").lower() == "root"
            ):
                name_l = (node.get("name") or "").lower()
                vpath_l = vpath.lower()
                doi_l = (node.get("doi") or "").lower()
                summary_l = (node.get("summary") or "").lower()
                combined_l = f"{name_l} {vpath_l} {doi_l} {summary_l}"

                # 1. Excluded terms (`-term` or `NOT term`) disqualify the node
                if any(ex in combined_l for ex in excluded):
                    pass
                # 2. Quoted phrases must appear in combined text
                elif any(ph not in combined_l for ph in phrases):
                    pass
                else:
                    name_stems = _extract_word_stems(name_l)
                    vpath_stems = _extract_word_stems(vpath_l)
                    summary_stems = _extract_word_stems(summary_l)
                    all_stems = name_stems | vpath_stems | summary_stems

                    # 3. Required terms (`+term`) must match at least by stem
                    req_ok = True
                    for req in required:
                        r_stem = _stem_word(req)
                        if req not in combined_l and r_stem not in all_stems:
                            req_ok = False
                            break

                    if req_ok:
                        score = 0.0
                        exact_substring_match = False

                        # Contiguous full-query substring boost
                        if doi_l and q_lower in doi_l:
                            score += 100.0
                            exact_substring_match = True
                        if q_lower in name_l:
                            score += 80.0
                            exact_substring_match = True
                        elif q_lower in vpath_l:
                            score += 40.0
                            exact_substring_match = True
                        if q_lower in summary_l:
                            score += 30.0
                            exact_substring_match = True

                        # Quoted phrase boosts
                        for ph in phrases:
                            if ph in name_l:
                                score += 25.0
                            elif ph in summary_l:
                                score += 12.0

                        matched_tokens = 0
                        title_matched_tokens = 0
                        for tok in tokens:
                            tok_stem = _stem_word(tok)
                            tok_score = 0.0
                            in_title = False

                            if doi_l and tok in doi_l:
                                tok_score += 8.0
                                in_title = True

                            ex_name, st_name = _token_matches_field(tok, tok_stem, name_l, name_stems)
                            if ex_name:
                                tok_score += 5.0
                                in_title = True
                            elif st_name:
                                tok_score += 3.5
                                in_title = True

                            ex_vp, st_vp = _token_matches_field(tok, tok_stem, vpath_l, vpath_stems)
                            if ex_vp:
                                tok_score += 2.5
                            elif st_vp:
                                tok_score += 1.5

                            ex_sum, st_sum = _token_matches_field(tok, tok_stem, summary_l, summary_stems)
                            if ex_sum:
                                tok_score += 2.0
                            elif st_sum:
                                tok_score += 1.2

                            if tok_score > 0:
                                matched_tokens += 1
                                if in_title:
                                    title_matched_tokens += 1
                                score += tok_score

                        n_tok = len(tokens)
                        type_priority = 1 if ntype in ("document", "book") else 0

                        is_primary = False
                        if exact_substring_match or (phrases and not tokens):
                            is_primary = True
                        elif n_tok > 0:
                            if is_or or n_tok == 1:
                                is_primary = matched_tokens >= 1
                            elif n_tok == 2:
                                is_primary = (matched_tokens == 2) or (title_matched_tokens >= 1)
                            else:
                                # For >= 3 tokens (e.g. 'platonic representation hypothesis convergence'),
                                # match if >= 50% of tokens hit across name+summary OR >= 2 tokens hit the title
                                min_needed = max(2, (n_tok + 1) // 2)
                                is_primary = (matched_tokens >= min_needed) or (title_matched_tokens >= 2)

                        if is_primary and score > 0:
                            primary_hits.append(
                                (score, matched_tokens, title_matched_tokens, type_priority, node)
                            )
                        elif score > 0 and (title_matched_tokens >= 1 or matched_tokens >= 2):
                            fallback_hits.append(
                                (score, matched_tokens, title_matched_tokens, type_priority, node)
                            )

            if "children" in node:
                for child in node["children"]:
                    search(child)

        search(self.vtree.get("root"))
        chosen = primary_hits if primary_hits else fallback_hits
        chosen.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
        return [item[4] for item in chosen]

    def update_ancestor_summaries(self, vpath: str, new_summary: str):
        """
        Recursively bubble up summary updates when new documents are added or changed.
        Tags ancestor directories with `_needs_summary_update = True`.
        """
        parts = [p for p in vpath.split("/") if p]

        ancestors = []
        curr = self.vtree.get("root")
        if curr:
            ancestors.append(curr)

        for part in parts[:-1]:
            found = False
            for child in curr.get("children", []):
                if child.get("name") == part:
                    curr = child
                    ancestors.append(curr)
                    found = True
                    break
            if not found:
                break

        for node in ancestors:
            node["_needs_summary_update"] = True
