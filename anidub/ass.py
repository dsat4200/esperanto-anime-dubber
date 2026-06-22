def merge_duplicate_lines(lines: list[str], min_repeat: int = 4, interactive: bool = True) -> list[str]:
    def _clean(line: str) -> str:
        parts = line.split(",", 9)
        if len(parts) < 10:
            return ""
        return strip_override_tags(parts[9]).strip().lower()

    def _end(line: str) -> str:
        return line.split(",", 9)[2]

    def _build_merged(first: str, last: str) -> str:
        fp = first.split(",", 9)
        merged = ",".join(fp[:2] + [_end(last)] + fp[3:])
        return merged + "\n" if not merged.endswith("\n") else merged

    def _ask_merge(run_len: int, text: str, first: str, last: str) -> bool:
        start_ts = first.split(",", 9)[1]
        end_ts = _end(last)
        ans = input(
            f"  Merge {run_len} copies of '{text[:70]}' "
            f"(0:{start_ts} -> 0:{end_ts})? [Y/n] "
        ).strip().lower()
        return ans in ("", "y", "yes")

    def _check_substring(out: list, cur_text: str, prev_line: str) -> bool:
        if not out:
            return False
        prev = out[-1]
        if not prev.startswith("Dialogue:"):
            return False
        pt = _clean(prev)
        if not pt or pt == cur_text:
            return False
        shorter = pt if len(pt) < len(cur_text) else cur_text
        longer = cur_text if len(pt) < len(cur_text) else pt
        if len(shorter) >= 2 and len(shorter) >= len(longer) / 2:
            if longer.startswith(shorter):
                ans = input(
                    f"  Substring: '{pt[:50]}' -> '{cur_text[:50]}'. Merge? [Y/n] "
                ).strip().lower()
                return ans in ("", "y", "yes")
        return False

    # ------------------------------------------------------------------
    # Pass 0: non-consecutive cyclic duplicates (run FIRST, one prompt)
    # ------------------------------------------------------------------
    raw_text_to_indices: dict[str, list[int]] = {}
    for idx, line in enumerate(lines):
        if line.startswith("Dialogue:"):
            t = _clean(line)
            if t:
                raw_text_to_indices.setdefault(t, []).append(idx)

    merge_groups = [(inds, t) for t, inds in raw_text_to_indices.items() if len(inds) >= 2]
    if merge_groups and interactive:
        total_lines = sum(len(inds) for inds, _ in merge_groups)
        msg = (
            f"  Found {len(merge_groups)} non-consecutive duplicate group(s) "
            f"({total_lines} lines across the file). Merge? [Y/n] "
        )
        if input(msg).strip().lower() in ("", "y", "yes"):
            keep = set()
            for inds, t in merge_groups:
                keep.add(inds[0])
            drop = set()
            for inds, t in merge_groups:
                drop.update(inds[1:])
            new_lines = []
            for idx, line in enumerate(lines):
                if idx in drop:
                    continue
                if idx in keep:
                    inds = next(i for i, _ in merge_groups if i[0] == idx)
                    last_idx = inds[-1]
                    new_lines.append(_build_merged(line, lines[last_idx]))
                else:
                    new_lines.append(line)
            lines = new_lines

    # ------------------------------------------------------------------
    # Pass 1: consecutive identical runs
    # ------------------------------------------------------------------
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("Dialogue:"):
            result.append(line)
            i += 1
            continue

        cur_text = _clean(line)
        if not cur_text:
            result.append(line)
            i += 1
            continue

        j = i + 1
        while j < len(lines) and lines[j].startswith("Dialogue:"):
            if _clean(lines[j]) != cur_text:
                break
            j += 1

        run_len = j - i
        should_merge = run_len >= min_repeat
        if not should_merge and run_len >= 2 and interactive:
            should_merge = _ask_merge(run_len, cur_text, line, lines[j - 1])

        if should_merge:
            merged = _build_merged(line, lines[j - 1])

            if interactive and _check_substring(result, cur_text, lines[j - 1]):
                prev_line = result.pop()
                prev_text = _clean(prev_line)
                longer_line = line if len(cur_text) >= len(prev_text) else prev_line
                merged = _build_merged(prev_line, lines[j - 1])
                mp = merged.split(",", 9)
                lp = longer_line.split(",", 9)
                if len(mp) >= 10 and len(lp) >= 10:
                    mp[9] = lp[9]
                    merged = ",".join(mp)

            result.append(merged)
        else:
            result.extend(lines[i:j])

        i = j

    if not interactive:
        return result

    # ------------------------------------------------------------------
    # Pass 2: adjacent substring merges (progressive karaoke)
    # ------------------------------------------------------------------
    i = 0
    while i < len(result) - 1:
        a = result[i]
        b = result[i + 1]
        if not a.startswith("Dialogue:") or not b.startswith("Dialogue:"):
            i += 1
            continue
        ta = _clean(a)
        tb = _clean(b)
        if ta and tb and ta != tb:
            shorter = ta if len(ta) < len(tb) else tb
            longer = tb if len(ta) < len(tb) else ta
            if len(shorter) >= 2 and len(shorter) >= len(longer) / 2:
                if longer.startswith(shorter):
                    ans = input(
                        f"  Substring: '{ta[:50]}' -> '{tb[:50]}'. Merge? [Y/n] "
                    ).strip().lower()
                    if ans in ("", "y", "yes"):
                        result[i] = _build_merged(a, b)
                        result.pop(i + 1)
                        continue
        i += 1

    return result