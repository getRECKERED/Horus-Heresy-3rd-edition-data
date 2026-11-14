#!/usr/bin/env python3
"""
bsp_parser.py
Production-Grade BSData (XML .gst/.cat & New Recruit .json) to LLM-Optimized Markdown Parser.
Resolves cross-file GUID links (entryLink, infoLink, infoGroup), strips point/modifier bloat,
and outputs structured Markdown files for NotebookLM and RAG pipelines.
"""

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Configure stderr logging for broken targetIds or parser diagnostics
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("bsp_parser")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class RuleItem:
    id: str
    name: str
    description: str
    source_file: str = ""


@dataclass
class ProfileItem:
    id: str
    name: str
    type_name: str
    characteristics: Dict[str, str] = field(default_factory=dict)
    source_file: str = ""


@dataclass
class ResolvedUnit:
    name: str
    category: str
    unit_profiles: List[ProfileItem] = field(default_factory=list)
    weapon_profiles: List[ProfileItem] = field(default_factory=list)
    ability_profiles: List[ProfileItem] = field(default_factory=list)
    rule_names: Set[str] = field(default_factory=set)
    inline_rules: List[RuleItem] = field(default_factory=list)
    wargear_names: Set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Unified XML / JSON Loader (Normalizes XML into BSData JSON Dict Format)
# ---------------------------------------------------------------------------

 CONTAINER_TAGS = {
    "rules": "rule",
    "sharedRules": "rule",
    "profiles": "profile",
    "sharedProfiles": "profile",
    "selectionEntries": "selectionEntry",
    "sharedSelectionEntries": "selectionEntry",
    "selectionEntryGroups": "selectionEntryGroup",
    "sharedSelectionEntryGroups": "selectionEntryGroup",
    "infoGroups": "infoGroup",
    "sharedInfoGroups": "infoGroup",
    "infoLinks": "infoLink",
    "entryLinks": "entryLink",
    "categoryLinks": "categoryLink",
    "characteristics": "characteristic",
    "modifiers": "modifier",
}

PRUNED_TAGS = {
    "costs", "cost", "constraints", "constraint",
    "conditions", "condition", "conditionGroups", "conditionGroup",
    "repeats", "repeat", "publications", "publication",
    "costTypes", "costType", "profileTypes", "profileType",
}


def strip_ns(tag: str) -> str:
    """Remove XML namespace prefix from tag name."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def xml_element_to_dict(elem: ET.Element) -> Dict[str, Any]:
    """Recursively convert a BattleScribe XML element into the standard BSData JSON dict structure."""
    node: Dict[str, Any] = dict(elem.attrib)

    for child in elem:
        ctag = strip_ns(child.tag)
        if ctag in PRUNED_TAGS:
            continue

        if ctag == "description":
            node["description"] = (child.text or "").strip()
        elif ctag == "comment":
            node["comment"] = (child.text or "").strip()
        elif ctag in CONTAINER_TAGS:
            expected_item_tag = CONTAINER_TAGS[ctag]
            items = []
            for sub in child:
                stag = strip_ns(sub.tag)
                if stag in PRUNED_TAGS:
                    continue
                if stag == "characteristic":
                    items.append({
                        "name": sub.attrib.get("name", ""),
                        "$text": (sub.text or "").strip(),
                    })
                elif stag == expected_item_tag or stag in (
                    "rule", "profile", "selectionEntry", "selectionEntryGroup",
                    "infoGroup", "infoLink", "entryLink", "categoryLink", "modifier"
                ):
                    items.append(xml_element_to_dict(sub))
            if items:
                node.setdefault(ctag, []).extend(items)

    return node


def load_bsdata_file(filepath: Path) -> Optional[Dict[str, Any]]:
    """Load either a .json, .cat, or .gst file into a normalized dictionary."""
    try:
        if filepath.suffix.lower() == ".json":
            with filepath.open("r", encoding="utf-8") as f:
                return json.load(f)
        elif filepath.suffix.lower() in (".cat", ".gst"):
            tree = ET.parse(filepath)
            root = tree.getroot()
            return xml_element_to_dict(root)
    except Exception as exc:
        logger.error(f"Failed to load {filepath.name}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Two-Pass Symbol Indexer & Link Resolver
# ---------------------------------------------------------------------------

class BSDataParser:
    def __init__(self, input_dir: Path, output_dir: Path, legion: str):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.legion = legion.strip()

        # Pass 1 Global Symbol Tables
        self.rules_by_id: Dict[str, RuleItem] = {}
        self.profiles_by_id: Dict[str, ProfileItem] = {}
        self.entries_by_id: Dict[str, Dict[str, Any]] = {}
        self.groups_by_id: Dict[str, Dict[str, Any]] = {}
        self.info_groups_by_id: Dict[str, Dict[str, Any]] = {}
        self.categories_by_id: Dict[str, str] = {}

        # Loaded documents grouped by role
        self.docs_by_file: Dict[str, Dict[str, Any]] = {}
        self.broken_links: Set[str] = set()

    def discover_files(self) -> List[Path]:
        extensions = {".json", ".cat", ".gst"}
        files = [
            p for p in sorted(self.input_dir.rglob("*"))
            if p.is_file() and p.suffix.lower() in extensions
            and not p.name.startswith(".")
            and "package" not in p.name.lower()
        ]
        return files

    @staticmethod
    def clean_text(text: str) -> str:
        """Normalize whitespace while preserving paragraph breaks and bullet lists."""
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def extract_characteristics(self, raw_chars: List[Dict[str, Any]]) -> Dict[str, str]:
        chars: Dict[str, str] = {}
        for c in raw_chars:
            cname = c.get("name", "").strip()
            val = c.get("$text") or c.get("value") or c.get("text") or ""
            chars[cname] = self.clean_text(str(val))
        return chars

    # -----------------------------------------------------------------------
    # PASS 1: Global Symbol Indexing
    # -----------------------------------------------------------------------
    def run_pass_1(self, files: List[Path]) -> None:
        for fpath in files:
            doc = load_bsdata_file(fpath)
            if not doc:
                continue
            self.docs_by_file[fpath.name] = doc
            self._index_node(doc, fpath.name)

    def _index_node(self, node: Dict[str, Any], filename: str) -> None:
        """Recursively index all identifiable elements into global lookup tables."""
        # Category definitions
        for cat in node.get("categoryEntries", []) + node.get("sharedCategoryEntries", []):
            cid = cat.get("id")
            if cid and cat.get("name"):
                self.categories_by_id[cid] = cat["name"].strip()

        # Rules
        for r in node.get("rules", []) + node.get("sharedRules", []):
            rid = r.get("id")
            rname = (r.get("name") or "").strip()
            rdesc = self.clean_text(r.get("description") or "")
            if rid and rname:
                # Prefer non-empty descriptions if already indexed
                if rid not in self.rules_by_id or (rdesc and not self.rules_by_id[rid].description):
                    self.rules_by_id[rid] = RuleItem(
                        id=rid, name=rname, description=rdesc, source_file=filename
                    )

        # Profiles
        for p in node.get("profiles", []) + node.get("sharedProfiles", []):
            pid = p.get("id")
            pname = (p.get("name") or "").strip()
            ptype = (p.get("typeName") or "").strip()
            chars = self.extract_characteristics(p.get("characteristics", []))
            if pid and pname:
                self.profiles_by_id[pid] = ProfileItem(
                    id=pid,
                    name=pname,
                    type_name=ptype,
                    characteristics=chars,
                    source_file=filename,
                )

        # InfoGroups
        for ig in node.get("infoGroups", []) + node.get("sharedInfoGroups", []):
            ig_id = ig.get("id")
            if ig_id:
                ig["_source_file"] = filename
                self.info_groups_by_id[ig_id] = ig
            self._index_node(ig, filename)

        # SelectionEntries
        for se in node.get("selectionEntries", []) + node.get("sharedSelectionEntries", []):
            se_id = se.get("id")
            if se_id:
                se["_source_file"] = filename
                self.entries_by_id[se_id] = se
            self._index_node(se, filename)

        # SelectionEntryGroups
        for seg in node.get("selectionEntryGroups", []) + node.get("sharedSelectionEntryGroups", []):
            seg_id = seg.get("id")
            if seg_id:
                seg["_source_file"] = filename
                self.groups_by_id[seg_id] = seg
            self._index_node(seg, filename)

    # -----------------------------------------------------------------------
    # PASS 2: Dereferencing & Extraction
    # -----------------------------------------------------------------------
    def resolve_parameterized_name(self, link_node: Dict[str, Any], target_name: str) -> str:
        """
        In HH 3.0 BSData, variable rules like 'Breaching (X)' often have their specific value
        stored in a name modifier, comment, or directly on the infoLink's name attribute.
        """
        link_name = (link_node.get("name") or "").strip()
        comment = (link_node.get("comment") or "").strip()

        # Check modifiers for a set/replace/append on the name field
        for mod in link_node.get("modifiers", []):
            if mod.get("field") == "name" and mod.get("value"):
                val = str(mod["value"]).strip()
                if mod.get("type") == "set":
                    return val
                elif mod.get("type") == "append":
                    return f"{link_name or target_name} {val}".strip()

        if link_name and link_name != target_name and "(X)" in target_name:
            return link_name
        if "(X)" in target_name and comment:
            return target_name.replace("(X)", f"({comment})")
        return link_name or target_name

    def collect_from_node(
        self,
        node: Dict[str, Any],
        unit: ResolvedUnit,
        visited: Set[str],
        depth: int = 0,
    ) -> None:
        """Recursively traverse a unit entry and dereference all nested links."""
        if depth > 14:
            return

        # 1. Local Profiles
        for p in node.get("profiles", []) + node.get("sharedProfiles", []):
            pid = p.get("id", "")
            p_item = self.profiles_by_id.get(pid) or ProfileItem(
                id=pid,
                name=(p.get("name") or "").strip(),
                type_name=(p.get("typeName") or "").strip(),
                characteristics=self.extract_characteristics(p.get("characteristics", [])),
            )
            self._assign_profile(p_item, unit)

        # 2. Local Rules
        for r in node.get("rules", []) + node.get("sharedRules", []):
            rid = r.get("id", "")
            rname = (r.get("name") or "").strip()
            rdesc = self.clean_text(r.get("description") or "")
            if rname:
                unit.rule_names.add(rname)
                if rdesc and rid not in self.rules_by_id:
                    unit.inline_rules.append(RuleItem(id=rid, name=rname, description=rdesc))

        # 3. InfoGroups
        for ig in node.get("infoGroups", []) + node.get("sharedInfoGroups", []):
            self.collect_from_node(ig, unit, visited, depth + 1)

        # 4. InfoLinks (Dereference to Rule, Profile, or InfoGroup)
        for il in node.get("infoLinks", []):
            tid = il.get("targetId")
            if not tid:
                continue
            ltype = il.get("type", "")

            if tid in self.rules_by_id:
                target_rule = self.rules_by_id[tid]
                resolved_name = self.resolve_parameterized_name(il, target_rule.name)
                unit.rule_names.add(resolved_name)
            elif tid in self.profiles_by_id:
                target_prof = self.profiles_by_id[tid]
                resolved_name = self.resolve_parameterized_name(il, target_prof.name)
                cloned = ProfileItem(
                    id=target_prof.id,
                    name=resolved_name,
                    type_name=target_prof.type_name,
                    characteristics=target_prof.characteristics,
                    source_file=target_prof.source_file,
                )
                self._assign_profile(cloned, unit)
            elif tid in self.info_groups_by_id and tid not in visited:
                visited.add(tid)
                self.collect_from_node(self.info_groups_by_id[tid], unit, visited, depth + 1)
            else:
                self.broken_links.add(f"infoLink ({ltype}) -> {tid}")

        # 5. Child SelectionEntries & SelectionEntryGroups
        for se in node.get("selectionEntries", []):
            se_name = (se.get("name") or "").strip()
            if se.get("type") == "upgrade" and se_name:
                unit.wargear_names.add(se_name)
            self.collect_from_node(se, unit, visited, depth + 1)

        for seg in node.get("selectionEntryGroups", []):
            self.collect_from_node(seg, unit, visited, depth + 1)

        # 6. EntryLinks (Dereference to sharedSelectionEntry or sharedSelectionEntryGroup)
        for el in node.get("entryLinks", []):
            tid = el.get("targetId")
            if not tid or tid in visited:
                continue
            # Skip generic "Prime Unit" boilerplate links unless they hold faction specifics
            el_name = (el.get("name") or "").strip()
            if el_name.lower() in ("prime unit", "logistical benefit"):
                continue

            if tid in self.entries_by_id:
                visited.add(tid)
                target_entry = self.entries_by_id[tid]
                t_name = el_name or (target_entry.get("name") or "").strip()
                if target_entry.get("type") == "upgrade" and t_name:
                    unit.wargear_names.add(t_name)
                self.collect_from_node(target_entry, unit, visited, depth + 1)
                visited.discard(tid)
            elif tid in self.groups_by_id:
                visited.add(tid)
                self.collect_from_node(self.groups_by_id[tid], unit, visited, depth + 1)
                visited.discard(tid)
            else:
                self.broken_links.add(f"entryLink -> {tid}")

    def _assign_profile(self, prof: ProfileItem, unit: ResolvedUnit) -> None:
        if not prof.name:
            return
        tn = prof.type_name.lower()
        if any(k in tn for k in ("unit", "model", "vehicle", "automata", "knight", "titan", "characteristic")):
            if not any(p.name == prof.name and p.characteristics == prof.characteristics for p in unit.unit_profiles):
                unit.unit_profiles.append(prof)
        elif "weapon" in tn or "armoury" in tn:
            if not any(p.name == prof.name and p.characteristics == prof.characteristics for p in unit.weapon_profiles):
                unit.weapon_profiles.append(prof)
        else:
            if not any(p.name == prof.name and p.characteristics == prof.characteristics for p in unit.ability_profiles):
                unit.ability_profiles.append(prof)

    def resolve_category(self, node: Dict[str, Any]) -> str:
        for cl in node.get("categoryLinks", []):
            cname = (cl.get("name") or "").strip()
            tid = cl.get("targetId", "")
            if not cname and tid in self.categories_by_id:
                cname = self.categories_by_id[tid]
            if cname and cname.lower() not in ("unit", "model", "legiones astartes", "faction"):
                if cl.get("primary") in (True, "true"):
                    return cname
        # Fallback to first non-generic category
        for cl in node.get("categoryLinks", []):
            cname = (cl.get("name") or "").strip() or self.categories_by_id.get(cl.get("targetId", ""), "")
            if cname:
                return cname
        return "Uncategorized"

    def extract_units_from_doc(self, doc: Dict[str, Any]) -> List[ResolvedUnit]:
        """Extract all root/shared unit entries from a catalogue document."""
        units: Dict[str, ResolvedUnit] = {}

        candidates: List[Dict[str, Any]] = []
        for se in doc.get("sharedSelectionEntries", []) + doc.get("selectionEntries", []):
            if se.get("type") in ("unit", "model") or self._has_unit_profile(se):
                candidates.append(se)

        # Also check root entryLinks pointing to units
        for el in doc.get("entryLinks", []):
            tid = el.get("targetId")
            if tid and tid in self.entries_by_id:
                target = self.entries_by_id[tid]
                if target.get("type") in ("unit", "model") or self._has_unit_profile(target):
                    merged = dict(target)
                    if el.get("name"):
                        merged["name"] = el["name"]
                    if el.get("categoryLinks"):
                        merged["categoryLinks"] = el["categoryLinks"]
                    candidates.append(merged)

        for cand in candidates:
            uname = (cand.get("name") or "").strip()
            if not uname or uname in units:
                continue
            cat = self.resolve_category(cand)
            ru = ResolvedUnit(name=uname, category=cat)
            visited: Set[str] = {cand.get("id", "")}
            self.collect_from_node(cand, ru, visited)
            if ru.unit_profiles or ru.ability_profiles or ru.rule_names:
                units[uname] = ru

        return sorted(units.values(), key=lambda u: u.name)

    def _has_unit_profile(self, node: Dict[str, Any]) -> bool:
        for p in node.get("profiles", []):
            tn = (p.get("typeName") or "").lower()
            if any(k in tn for k in ("unit", "model", "vehicle")):
                return True
        return False

    # -----------------------------------------------------------------------
    # File Role Classification
    # -----------------------------------------------------------------------
    def classify_files(self) -> Tuple[List[str], List[str], List[str]]:
        """
        Classify loaded files into:
        1. Core Engine files (.gst, Special Rules, Traits, Psychic)
        2. Generic Armory / Liber files (Liber Astartes/Hereticus, Weapons, Wargear, Legiones Astartes)
        3. Target Legion files (matching --legion)
        """
        core_files: List[str] = []
        armoury_files: List[str] = []
        legion_files: List[str] = []

        legion_lower = self.legion.lower()

        for fname in sorted(self.docs_by_file.keys()):
            fl = fname.lower()
            if legion_lower and legion_lower in fl:
                legion_files.append(fname)
            elif (
                fname.endswith(".gst")
                or "edition" in fl
                or "age of darkness" in fl
                or fl in ("special rules.json", "traits.json", "psychic.json")
            ):
                core_files.append(fname)
            elif any(
                k in fl
                for k in (
                    "liber ",
                    "legiones astartes",
                    "weapons",
                    "wargear",
                    "armoury",
                    "generic",
                )
            ):
                armoury_files.append(fname)

        return core_files, armoury_files, legion_files

    # -----------------------------------------------------------------------
    # Markdown Rendering Helpers
    # -----------------------------------------------------------------------
    @staticmethod
    def render_profile_table(profiles: List[ProfileItem]) -> str:
        if not profiles:
            return ""
        # Group profiles by identical characteristic column keys
        grouped: Dict[Tuple[str, ...], List[ProfileItem]] = defaultdict(list)
        for p in profiles:
            cols = tuple(p.characteristics.keys())
            if cols:
                grouped[cols].append(p)

        out: List[str] = []
        for cols, prof_list in grouped.items():
            headers = ["Name"] + list(cols)
            out.append("| " + " | ".join(headers) + " |")
            out.append("| :--- | " + " | ".join([":---:" if len(c) <= 4 else ":---" for c in cols]) + " |")
            for p in prof_list:
                row = [f"**{p.name}**"] + [
                    p.characteristics.get(c, "-").replace("\n", " ").replace("|", "\\|")
                    for c in cols
                ]
                out.append("| " + " | ".join(row) + " |")
            out.append("")
        return "\n".join(out)

    def render_unit_markdown(self, unit: ResolvedUnit, include_weapons: bool = True) -> str:
        lines: List[str] = [f"### {unit.name}"]
        if unit.category and unit.category != "Uncategorized":
            lines.append(f"* **Role / Slot:** {unit.category}")
        lines.append("")

        if unit.unit_profiles:
            lines.append(self.render_profile_table(unit.unit_profiles))

        if unit.rule_names:
            sorted_rules = ", ".join(sorted(unit.rule_names))
            lines.append(f"**Special Rules:** {sorted_rules}\n")

        if unit.wargear_names:
            sorted_wg = ", ".join(sorted(unit.wargear_names))
            lines.append(f"**Wargear & Upgrades:** {sorted_wg}\n")

        if unit.ability_profiles:
            lines.append("#### Unit-Specific Abilities & Arcana")
            lines.append(self.render_profile_table(unit.ability_profiles))

        if unit.inline_rules:
            for ir in unit.inline_rules:
                lines.append(f"* **{ir.name}:** {ir.description}")
            lines.append("")

        if include_weapons and unit.weapon_profiles:
            lines.append("#### Associated Weapons")
            lines.append(self.render_profile_table(unit.weapon_profiles))

        lines.append("---\n")
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Output Generation
    # -----------------------------------------------------------------------
    def write_outputs(self) -> Tuple[int, int, int]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        core_files, armoury_files, legion_files = self.classify_files()

        total_units = 0
        total_weapons = 0
        total_rules = 0

        # -------------------------------------------------------------------
        # 1. 01_Universal_Rules_Engine.md
        # -------------------------------------------------------------------
        engine_path = self.output_dir / "01_Universal_Rules_Engine.md"
        core_rules: Dict[str, RuleItem] = {}
        core_abilities: Dict[str, ProfileItem] = {}

        for r in self.rules_by_id.values():
            if r.source_file in core_files and r.description:
                core_rules[r.name] = r
        for p in self.profiles_by_id.values():
            if p.source_file in core_files and "weapon" not in p.type_name.lower():
                core_abilities[f"{p.type_name}:{p.name}"] = p

        with engine_path.open("w", encoding="utf-8") as f:
            f.write("# 01 - Universal Rules Engine, Reactions & Psychic Disciplines\n\n")
            f.write(f"*Compiled from: {', '.join(core_files) or 'Global Index'}*\n\n---\n\n")

            f.write("## 1. Universal Special Rules (USRs)\n\n")
            for rname in sorted(core_rules.keys()):
                rule = core_rules[rname]
                f.write(f"### {rule.name}\n{rule.description}\n\n")
                total_rules += 1

            # Group core non-weapon profiles by typeName (Reactions, Traits, Psychic Powers, etc.)
            by_type: Dict[str, List[ProfileItem]] = defaultdict(list)
            for p in core_abilities.values():
                by_type[p.type_name or "General Profiles"].append(p)

            f.write("## 2. Core Profiles, Reactions, Traits & Psychic Powers\n\n")
            for tname in sorted(by_type.keys()):
                f.write(f"### {tname}\n\n")
                profs = sorted(by_type[tname], key=lambda x: x.name)
                f.write(self.render_profile_table(profs))
                f.write("\n")

        # -------------------------------------------------------------------
        # 2. 02_Liber_Astartes_Armoury.md
        # -------------------------------------------------------------------
        armoury_path = self.output_dir / "02_Liber_Astartes_Armoury.md"
        armoury_weapons: Dict[str, ProfileItem] = {}
        armoury_wargear: Dict[str, ProfileItem] = {}
        armoury_rules: Dict[str, RuleItem] = {}
        armoury_units: List[ResolvedUnit] = []

        for p in self.profiles_by_id.values():
            if p.source_file in armoury_files or p.source_file in core_files:
                tn = p.type_name.lower()
                if "weapon" in tn:
                    armoury_weapons[p.name] = p
                elif not any(k in tn for k in ("unit", "model", "vehicle")) and p.source_file in armoury_files:
                    armoury_wargear[p.name] = p

        for r in self.rules_by_id.values():
            if r.source_file in armoury_files and r.description and r.name not in core_rules:
                armoury_rules[r.name] = r

        for fname in armoury_files:
            armoury_units.extend(self.extract_units_from_doc(self.docs_by_file[fname]))

        with armoury_path.open("w", encoding="utf-8") as f:
            f.write("# 02 - Legiones Astartes Universal Armoury & Standard Units\n\n")
            f.write(f"*Compiled from: {', '.join(armoury_files) or 'Global Armory Index'}*\n\n---\n\n")

            if armoury_rules:
                f.write("## 1. Legion & Armoury Special Rules\n\n")
                for rname in sorted(armoury_rules.keys()):
                    r = armoury_rules[rname]
                    f.write(f"### {r.name}\n{r.description}\n\n")
                    total_rules += 1

            if armoury_wargear:
                f.write("## 2. Universal Wargear, Detachments & Upgrades\n\n")
                f.write(self.render_profile_table(sorted(armoury_wargear.values(), key=lambda x: x.name)))

            if armoury_weapons:
                f.write("## 3. Master Weapons Reference Table\n\n")
                sorted_wpns = sorted(armoury_weapons.values(), key=lambda x: x.name)
                f.write(self.render_profile_table(sorted_wpns))
                total_weapons += len(sorted_wpns)

            if armoury_units:
                f.write("## 4. Standard Legiones Astartes Units\n\n")
                seen_unames: Set[str] = set()
                for u in sorted(armoury_units, key=lambda x: x.name):
                    if u.name in seen_unames:
                        continue
                    seen_unames.add(u.name)
                    f.write(self.render_unit_markdown(u, include_weapons=False))
                    total_units += 1

        # -------------------------------------------------------------------
        # 3. 03_<Legion>_Legion.md
        # -------------------------------------------------------------------
        legion_slug = re.sub(r"[^a-zA-Z0-9]+", "_", self.legion).strip("_") or "Target_Faction"
        legion_path = self.output_dir / f"03_{legion_slug}_Legion.md"

        legion_rules: Dict[str, RuleItem] = {}
        legion_profiles: List[ProfileItem] = []
        legion_weapons: Dict[str, ProfileItem] = {}
        legion_units: List[ResolvedUnit] = []

        for r in self.rules_by_id.values():
            if r.source_file in legion_files and r.description:
                legion_rules[r.name] = r

        for p in self.profiles_by_id.values():
            if p.source_file in legion_files:
                tn = p.type_name.lower()
                if "weapon" in tn:
                    legion_weapons[p.name] = p
                elif not any(k in tn for k in ("unit", "model", "vehicle")):
                    legion_profiles.append(p)

        for fname in legion_files:
            legion_units.extend(self.extract_units_from_doc(self.docs_by_file[fname]))

        with legion_path.open("w", encoding="utf-8") as f:
            f.write(f"# 03 - {self.legion} Complete Faction Compendium\n\n")
            f.write(f"*Compiled from: {', '.join(legion_files) or 'No matching legion file found'}*\n\n---\n\n")

            if not legion_files:
                f.write(
                    f"> **Note:** No file matching `--legion \"{self.legion}\"` was found in `{self.input_dir}`.\n"
                    f"> Available files: {', '.join(sorted(self.docs_by_file.keys()))}\n"
                )
            else:
                if legion_rules:
                    f.write(f"## 1. {self.legion} Faction Rules, Cults & Arcana\n\n")
                    for rname in sorted(legion_rules.keys()):
                        r = legion_rules[rname]
                        f.write(f"### {r.name}\n{r.description}\n\n")
                        total_rules += 1

                if legion_profiles:
                    f.write(f"## 2. {self.legion} Reactions, Rites of War, Psychic Powers & Wargear\n\n")
                    by_ltype: Dict[str, List[ProfileItem]] = defaultdict(list)
                    for lp in legion_profiles:
                        by_ltype[lp.type_name or "Faction Profiles"].append(lp)
                    for lt in sorted(by_ltype.keys()):
                        f.write(f"### {lt}\n\n")
                        f.write(self.render_profile_table(sorted(by_ltype[lt], key=lambda x: x.name)))

                if legion_weapons:
                    f.write(f"## 3. {self.legion} Unique Weapons (Achean Force & Legion Armoury)\n\n")
                    sorted_lwpns = sorted(legion_weapons.values(), key=lambda x: x.name)
                    f.write(self.render_profile_table(sorted_lwpns))
                    total_weapons += len(sorted_lwpns)

                if legion_units:
                    f.write(f"## 4. {self.legion} Units, Characters & Datasheets\n\n")
                    seen_lunames: Set[str] = set()
                    for u in sorted(legion_units, key=lambda x: x.name):
                        if u.name in seen_lunames:
                            continue
                        seen_lunames.add(u.name)
                        f.write(self.render_unit_markdown(u, include_weapons=True))
                        total_units += 1

        return total_units, total_weapons, total_rules


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    argp = argparse.ArgumentParser(
        description="Parse BSData .gst/.cat XML or .json repositories into LLM-optimized Markdown."
    )
    argp.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Path to cloned BSData repository directory.",
    )
    argp.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Output directory for generated Markdown files.",
    )
    argp.add_argument(
        "--legion",
        type=str,
        default="Thousand Sons",
        help="Target faction/legion name (e.g., 'Thousand Sons').",
    )

    args = argp.parse_args()

    if not args.input_dir.exists():
        logger.error(f"Input directory does not exist: {args.input_dir}")
        sys.exit(1)

    parser = BSDataParser(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        legion=args.legion,
    )

    files = parser.discover_files()
    if not files:
        logger.error(f"No .json, .cat, or .gst files found in {args.input_dir}")
        sys.exit(1)

    logger.info(f"Pass 1: Indexing {len(files)} BSData files from '{args.input_dir}'...")
    parser.run_pass_1(files)

    total_ids = (
        len(parser.rules_by_id)
        + len(parser.profiles_by_id)
        + len(parser.entries_by_id)
        + len(parser.groups_by_id)
        + len(parser.info_groups_by_id)
    )

    logger.info("Pass 2: Resolving cross-file GUID links and rendering Markdown...")
    units_cnt, wpns_cnt, rules_cnt = parser.write_outputs()

    if parser.broken_links:
        logger.warning(
            f"Encountered {len(parser.broken_links)} unresolved external targetId references "
            f"(logged silently; execution unaffected)."
        )

    print(
        f"\n[SUCCESS] BSData Extraction Complete:\n"
        f"  - Indexed IDs   : {total_ids:,}\n"
        f"  - Extracted     : {units_cnt:,} units, {wpns_cnt:,} weapons, {rules_cnt:,} special rules\n"
        f"  - Output Folder : {args.output_dir.resolve()}"
    )


if __name__ == "__main__":
    main()
