#!/usr/bin/env python3
"""
bsp_parser.py (v4.1 - True Force Org Roles, Strict Base Points & Compact JSON Catalog)
Compiles Horus Heresy 3rd Edition BSData into:
  1. roster_catalog.json (Compact normalized schema for Hobby Tracker UI & Gemini Co-pilot)
  2. 01-04 Markdown compendiums (For NotebookLM tactical cheat sheets & rules lookup)
"""

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("bsp_parser")

OTHER_LEGIONS = {
    "dark angels", "emperor's children", "iron warriors", "white scars",
    "space wolves", "imperial fists", "night lords", "blood angels",
    "iron hands", "world eaters", "ultramarines", "death guard",
    "sons of horus", "word bearers", "salamanders", "raven guard", "alpha legion"
}

# Full Horus Heresy 3rd Edition Battlefield Role hierarchy
BATTLEFIELD_ROLE_PRIORITY = [
    "High Command",
    "Command",
    "Retinue",
    "Troops",
    "Elites",
    "Heavy Assault",
    "Support",
    "Recon",
    "Fast Attack",
    "Armour",
    "Heavy Support",
    "War-engine",
    "Dedicated Transport",
    "Heavy Transport",
    "Transport",
    "Lord of War",
    "Lords of War",
    "Warlord",
    "Fortification",
]

NON_ROLE_CATEGORIES = {
    "unit", "model", "legiones astartes", "faction", "traitor", "loyalist",
    "unique", "infantry", "vehicle", "dreadnought", "cavalry", "automata",
    "psyker", "character", "sergeant", "independent character", "master of the legion",
    "free power weapon", "heavy", "line", "bulky", "malefic", "skimmer",
    "antigrav", "walker", "transport", "super-heavy", "titan", "knight",
}

GLOBAL_META_UPGRADES = {
    "prime unit", "logistical benefit", "allegiance", "legion", "faction",
    "rite of war", "warlord trait", "army configuration",
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


@dataclass
class RuleItem:
    id: str
    name: str
    description: str
    source_file: str = ""
    source_ref: str = ""


@dataclass
class ProfileItem:
    id: str
    name: str
    type_name: str
    characteristics: Dict[str, str] = field(default_factory=dict)
    source_file: str = ""
    source_ref: str = ""


@dataclass
class AdditionalModelOption:
    id: str
    name: str
    cost_pts: float
    min_added: int
    max_added: Optional[int]
    base_count: int


@dataclass
class UpgradeOption:
    id: str
    name: str
    cost_pts: float = 0.0
    min_qty: Optional[int] = None
    max_qty: Optional[int] = None
    group_name: str = ""
    is_default: bool = False
    grants: List[str] = field(default_factory=list)


@dataclass
class ForceSlot:
    name: str
    min_slots: int = 0
    max_slots: Optional[int] = None


@dataclass
class DetachmentItem:
    id: str
    name: str
    detachment_type: str
    source_file: str
    source_ref: str = ""
    description: str = ""
    slots: List[ForceSlot] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)


@dataclass
class ResolvedUnit:
    id: str
    name: str
    category: str = "Uncategorized"
    catalog_scope: str = "Armoury"
    source_ref: str = ""
    root_points: float = 0.0
    base_points: float = 0.0
    base_model_count: int = 1
    model_points_breakdown: List[str] = field(default_factory=list)
    unit_composition: List[str] = field(default_factory=list)
    additional_models: List[AdditionalModelOption] = field(default_factory=list)
    unit_profiles: List[ProfileItem] = field(default_factory=list)
    weapon_profiles: List[ProfileItem] = field(default_factory=list)
    ability_profiles: List[ProfileItem] = field(default_factory=list)
    rule_names: Set[str] = field(default_factory=set)
    inline_rules: List[RuleItem] = field(default_factory=list)
    default_wargear: Set[str] = field(default_factory=set)
    upgrades: List[UpgradeOption] = field(default_factory=list)


CONTAINER_TAGS = {
    "publications": "publication",
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
    "categoryEntries": "categoryEntry",
    "sharedCategoryEntries": "categoryEntry",
    "forceEntries": "forceEntry",
    "sharedForceEntries": "forceEntry",
    "characteristics": "characteristic",
    "modifiers": "modifier",
    "costs": "cost",
    "constraints": "constraint",
}

PRUNED_TAGS = {
    "conditions", "condition", "conditionGroups", "conditionGroup",
    "repeats", "repeat", "costTypes", "costType", "profileTypes", "profileType",
}

BSDATA_KEYS = {
    "sharedSelectionEntries", "selectionEntries",
    "sharedRules", "rules",
    "sharedProfiles", "profiles",
    "entryLinks", "infoLinks",
    "categoryEntries", "sharedCategoryEntries",
    "sharedSelectionEntryGroups", "selectionEntryGroups",
    "sharedInfoGroups", "infoGroups",
    "forceEntries", "sharedForceEntries",
}


def get_dict_list(node: Any, *keys: str) -> List[Dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    result: List[Dict[str, Any]] = []
    for k in keys:
        val = node.get(k)
        if not val:
            continue
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    result.append(item)
                elif isinstance(item, list):
                    result.extend(x for x in item if isinstance(x, dict))
        elif isinstance(val, dict):
            inner_lists = [v for v in val.values() if isinstance(v, list)]
            if inner_lists and "id" not in val and "name" not in val:
                for il in inner_lists:
                    result.extend(x for x in il if isinstance(x, dict))
            else:
                result.append(val)
    return result


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def xml_element_to_dict(elem: ET.Element) -> Dict[str, Any]:
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
                elif stag == expected_item_tag or stag in CONTAINER_TAGS.values():
                    items.append(xml_element_to_dict(sub))
            if items:
                node.setdefault(ctag, []).extend(items)
    return node


def load_bsdata_file(filepath: Path) -> Optional[Dict[str, Any]]:
    try:
        if filepath.suffix.lower() == ".json":
            with filepath.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return None
            if isinstance(raw.get("catalogue"), dict):
                raw = raw["catalogue"]
            elif isinstance(raw.get("gameSystem"), dict):
                raw = raw["gameSystem"]
            if not any(k in raw for k in BSDATA_KEYS):
                return None
            return raw
        elif filepath.suffix.lower() in (".cat", ".gst"):
            tree = ET.parse(filepath)
            root = tree.getroot()
            return xml_element_to_dict(root)
    except Exception as exc:
        logger.error(f"Failed to load {filepath.name}: {exc}")
    return None


class BSDataParser:
    def __init__(self, input_dir: Path, output_dir: Path, legion: str, patch_file: Optional[Path] = None):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.legion = legion.strip()
        self.excluded_legions = OTHER_LEGIONS - {self.legion.lower()}
        self.patch_file = patch_file or (input_dir / "errata_patches.json")
        self.patches: Dict[str, Any] = {}
        self.applied_patches_log: List[str] = []

        self.publications_by_id: Dict[str, str] = {}
        self.rules_by_id: Dict[str, RuleItem] = {}
        self.profiles_by_id: Dict[str, ProfileItem] = {}
        self.entries_by_id: Dict[str, Dict[str, Any]] = {}
        self.groups_by_id: Dict[str, Dict[str, Any]] = {}
        self.info_groups_by_id: Dict[str, Dict[str, Any]] = {}
        self.categories_by_id: Dict[str, str] = {}
        self.detachment_slot_roles: Set[str] = set()
        self.detachments: List[DetachmentItem] = []

        self.docs_by_file: Dict[str, Dict[str, Any]] = {}
        self.broken_links: Set[str] = set()

        self._load_patches()

    def _load_patches(self) -> None:
        if self.patch_file and self.patch_file.exists():
            try:
                self.patches = json.loads(self.patch_file.read_text(encoding="utf-8"))
                logger.info(f"Loaded errata patch file: {self.patch_file.name}")
            except Exception as exc:
                logger.error(f"Failed to parse {self.patch_file}: {exc}")

    def discover_files(self) -> List[Path]:
        extensions = {".json", ".cat", ".gst"}
        files = []
        for p in sorted(self.input_dir.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in extensions:
                continue
            if p.name in ("errata_patches.json", "roster_catalog.json") or "package" in p.name.lower():
                continue
            rel_parts = p.relative_to(self.input_dir).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            files.append(p)
        return files

    @staticmethod
    def clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def is_other_legion_item(self, name: str) -> bool:
        nl = name.lower()
        for ol in self.excluded_legions:
            if ol in nl:
                return True
        return False

    def format_source_ref(self, node: Dict[str, Any]) -> str:
        if not isinstance(node, dict):
            return ""
        pub_id = str(node.get("publicationId") or "").strip()
        page = str(node.get("page") or "").strip()
        pub_name = self.publications_by_id.get(pub_id, "")
        if pub_name and page:
            return f"*(Source: {pub_name}, p. {page})*"
        elif pub_name:
            return f"*(Source: {pub_name})*"
        elif page:
            return f"*(p. {page})*"
        return ""

    def extract_pts(self, node: Dict[str, Any]) -> float:
        for c in get_dict_list(node, "costs"):
            cname = str(c.get("name") or "").lower()
            if "pt" in cname or "point" in cname:
                try:
                    return float(c.get("value", 0))
                except (ValueError, TypeError):
                    return 0.0
        return 0.0

    def extract_constraints(self, node: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
        min_v, max_v = None, None
        for c in get_dict_list(node, "constraints"):
            ctype = str(c.get("type") or "").lower()
            field_name = str(c.get("field") or "selections").lower()
            if "pt" in field_name or "point" in field_name:
                continue
            try:
                val = int(float(c.get("value", 0)))
                if ctype == "min":
                    min_v = val if min_v is None else max(min_v, val)
                elif ctype == "max" and val >= 0:
                    max_v = val if max_v is None else min(max_v, val)
            except (ValueError, TypeError):
                continue
        return min_v, max_v

    def extract_characteristics(self, prof_node: Dict[str, Any]) -> Dict[str, str]:
        chars: Dict[str, str] = {}
        for c in get_dict_list(prof_node, "characteristics"):
            cname = str(c.get("name") or "").strip()
            val = c.get("$text")
            if val is None:
                val = c.get("value")
            if val is None:
                val = c.get("text") or ""
            chars[cname] = self.clean_text(str(val))
        return chars

    # -----------------------------------------------------------------------
    # PASS 1: Global Indexing
    # -----------------------------------------------------------------------
    def run_pass_1(self, files: List[Path]) -> None:
        raw_docs: List[Tuple[str, Dict[str, Any]]] = []
        for fpath in files:
            doc = load_bsdata_file(fpath)
            if not isinstance(doc, dict):
                continue
            self.docs_by_file[fpath.name] = doc
            raw_docs.append((fpath.name, doc))
            for pub in get_dict_list(doc, "publications"):
                pid = str(pub.get("id") or "").strip()
                pname = str(pub.get("name") or "").strip()
                if pid and pname:
                    self.publications_by_id[pid] = pname
            # Index categories globally first so forceEntries and units resolve all category IDs
            self._index_categories_only(doc)

        for fname, doc in raw_docs:
            self._index_node(doc, fname)

        self._apply_weapon_patches()

    def _index_categories_only(self, node: Any) -> None:
        if not isinstance(node, dict):
            return
        for cat in get_dict_list(node, "categoryEntries", "sharedCategoryEntries"):
            cid = cat.get("id")
            if cid and cat.get("name"):
                self.categories_by_id[str(cid)] = str(cat["name"]).strip()
        for k in ("selectionEntries", "sharedSelectionEntries", "forceEntries", "sharedForceEntries"):
            for child in get_dict_list(node, k):
                self._index_categories_only(child)

    def _apply_weapon_patches(self) -> None:
        wpn_patches = self.patches.get("weapons", {})
        if not wpn_patches:
            return
        for prof in self.profiles_by_id.values():
            if prof.name in wpn_patches:
                patch_data = wpn_patches[prof.name]
                overrides = patch_data.get("characteristic_overrides", {})
                for k, v in overrides.items():
                    prof.characteristics[k] = str(v)
                if patch_data.get("source_ref"):
                    prof.source_ref = f"*(Source: {patch_data['source_ref']})*"
                msg = f"Weapon Patch: '{prof.name}' -> {overrides}"
                if msg not in self.applied_patches_log:
                    self.applied_patches_log.append(msg)

    def _index_node(self, node: Any, filename: str) -> None:
        if not isinstance(node, dict):
            return

        for r in get_dict_list(node, "rules", "sharedRules"):
            rid = r.get("id")
            rname = str(r.get("name") or "").strip()
            rdesc = self.clean_text(str(r.get("description") or ""))
            sref = self.format_source_ref(r)
            if rid and rname:
                rid_str = str(rid)
                if rid_str not in self.rules_by_id or (rdesc and not self.rules_by_id[rid_str].description):
                    self.rules_by_id[rid_str] = RuleItem(
                        id=rid_str, name=rname, description=rdesc, source_file=filename, source_ref=sref
                    )

        for p in get_dict_list(node, "profiles", "sharedProfiles"):
            pid = p.get("id")
            pname = str(p.get("name") or "").strip()
            ptype = str(p.get("typeName") or "").strip()
            chars = self.extract_characteristics(p)
            sref = self.format_source_ref(p)
            if pid and pname:
                pid_str = str(pid)
                self.profiles_by_id[pid_str] = ProfileItem(
                    id=pid_str,
                    name=pname,
                    type_name=ptype,
                    characteristics=chars,
                    source_file=filename,
                    source_ref=sref,
                )

        for fe in get_dict_list(node, "forceEntries", "sharedForceEntries"):
            self._extract_detachment(fe, filename)
            self._index_node(fe, filename)

        for ig in get_dict_list(node, "infoGroups", "sharedInfoGroups"):
            ig_id = ig.get("id")
            if ig_id:
                ig["_source_file"] = filename
                self.info_groups_by_id[str(ig_id)] = ig
            self._index_node(ig, filename)

        for se in get_dict_list(node, "selectionEntries", "sharedSelectionEntries"):
            se_id = se.get("id")
            if se_id:
                se["_source_file"] = filename
                self.entries_by_id[str(se_id)] = se
            self._index_node(se, filename)

        for seg in get_dict_list(node, "selectionEntryGroups", "sharedSelectionEntryGroups"):
            seg_id = seg.get("id")
            if seg_id:
                seg["_source_file"] = filename
                self.groups_by_id[str(seg_id)] = seg
            self._index_node(seg, filename)

    def _classify_detachment_type(self, name: str) -> str:
        nl = name.lower()
        if "primary" in nl or "crusade" in nl:
            return "Primary"
        if "apex" in nl or "leviathan" in nl:
            return "Apex"
        if "allied" in nl:
            return "Allied"
        if "auxiliary" in nl or "convocation" in nl or "cohort" in nl or "delegation" in nl:
            return "Auxiliary"
        return "Special"

    def _extract_detachment(self, fe: Dict[str, Any], filename: str) -> None:
        fe_id = str(fe.get("id") or "")
        fe_name = str(fe.get("name") or "").strip()
        if not fe_name or self.is_other_legion_item(fe_name):
            return
        desc = self.clean_text(str(fe.get("description") or ""))
        sref = self.format_source_ref(fe)
        slots: List[ForceSlot] = []
        for cl in get_dict_list(fe, "categoryLinks"):
            sname = str(cl.get("name") or "").strip()
            tid = str(cl.get("targetId") or "")
            if tid in self.categories_by_id:
                sname = self.categories_by_id[tid]
            if not sname or sname.lower() in ("allegiance", "rite of war", "army configuration"):
                continue
            self.detachment_slot_roles.add(sname)
            min_s, max_s = self.extract_constraints(cl)
            slots.append(ForceSlot(name=sname, min_slots=min_s or 0, max_slots=max_s))

        rules_list: List[str] = []
        for r in get_dict_list(fe, "rules"):
            rname = str(r.get("name") or "").strip()
            rdesc = self.clean_text(str(r.get("description") or ""))
            if rname and rdesc:
                rules_list.append(f"{rname}: {rdesc}")

        det = DetachmentItem(
            id=slugify(fe_name) or fe_id,
            name=fe_name,
            detachment_type=self._classify_detachment_type(fe_name),
            source_file=filename,
            source_ref=sref,
            description=desc,
            slots=slots,
            rules=rules_list,
        )
        self.detachments.append(det)

    # -----------------------------------------------------------------------
    # PASS 2: Dereferencing & Unit Resolution
    # -----------------------------------------------------------------------
    def resolve_parameterized_name(self, link_node: Dict[str, Any], target_name: str) -> str:
        if not isinstance(link_node, dict):
            return target_name
        link_name = str(link_node.get("name") or "").strip()
        comment = str(link_node.get("comment") or "").strip()

        for mod in get_dict_list(link_node, "modifiers"):
            if mod.get("field") == "name" and mod.get("value") is not None:
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

    def extract_grants_from_upgrade(self, node: Dict[str, Any], visited: Set[str], depth: int = 0) -> List[str]:
        if not isinstance(node, dict) or depth > 2:
            return []
        grants: List[str] = []
        for r in get_dict_list(node, "rules", "sharedRules"):
            rname = str(r.get("name") or "").strip()
            rdesc = self.clean_text(str(r.get("description") or ""))
            if rname and not self.is_other_legion_item(rname):
                if rdesc and len(rdesc) < 120:
                    grants.append(f"{rname} ({rdesc})")
                else:
                    grants.append(rname)

        for p in get_dict_list(node, "profiles", "sharedProfiles"):
            pname = str(p.get("name") or "").strip()
            if pname and not self.is_other_legion_item(pname):
                grants.append(pname)

        for il in get_dict_list(node, "infoLinks"):
            tid = str(il.get("targetId") or "")
            if tid in self.rules_by_id:
                rname = self.resolve_parameterized_name(il, self.rules_by_id[tid].name)
                if not self.is_other_legion_item(rname):
                    grants.append(rname)
            elif tid in self.profiles_by_id:
                pname = self.resolve_parameterized_name(il, self.profiles_by_id[tid].name)
                if not self.is_other_legion_item(pname):
                    grants.append(pname)

        for se in get_dict_list(node, "selectionEntries"):
            sname = str(se.get("name") or "").strip()
            if sname and not self.is_other_legion_item(sname):
                grants.append(sname)

        for el in get_dict_list(node, "entryLinks"):
            tid = str(el.get("targetId") or "")
            if tid in self.entries_by_id and tid not in visited:
                t_entry = self.entries_by_id[tid]
                ename = str(el.get("name") or t_entry.get("name") or "").strip()
                if ename and not self.is_other_legion_item(ename):
                    grants.append(ename)
                grants.extend(self.extract_grants_from_upgrade(t_entry, visited | {tid}, depth + 1))

        seen: Set[str] = set()
        clean_grants: List[str] = []
        node_name = str(node.get("name") or "").strip().lower()
        for g in grants:
            gl = g.lower()
            if gl != node_name and gl not in seen:
                seen.add(gl)
                clean_grants.append(g)
        return clean_grants[:4]

    def collect_weapons_only(self, node: Dict[str, Any], unit: ResolvedUnit, visited: Set[str], depth: int = 0) -> None:
        if not isinstance(node, dict) or depth > 5:
            return
        for p in get_dict_list(node, "profiles", "sharedProfiles"):
            pid = str(p.get("id") or "")
            p_item = self.profiles_by_id.get(pid) or ProfileItem(
                id=pid,
                name=str(p.get("name") or "").strip(),
                type_name=str(p.get("typeName") or "").strip(),
                characteristics=self.extract_characteristics(p),
                source_ref=self.format_source_ref(p),
            )
            if "weapon" in p_item.type_name.lower() and not self.is_other_legion_item(p_item.name):
                self._assign_profile(p_item, unit)

        for il in get_dict_list(node, "infoLinks"):
            tid = str(il.get("targetId") or "")
            if tid in self.profiles_by_id:
                target_prof = self.profiles_by_id[tid]
                if "weapon" in target_prof.type_name.lower() and not self.is_other_legion_item(target_prof.name):
                    resolved_name = self.resolve_parameterized_name(il, target_prof.name)
                    cloned = ProfileItem(
                        id=target_prof.id,
                        name=resolved_name,
                        type_name=target_prof.type_name,
                        characteristics=target_prof.characteristics,
                        source_file=target_prof.source_file,
                        source_ref=target_prof.source_ref,
                    )
                    self._assign_profile(cloned, unit)
            elif tid in self.info_groups_by_id and tid not in visited:
                self.collect_weapons_only(self.info_groups_by_id[tid], unit, visited | {tid}, depth + 1)

        for se in get_dict_list(node, "selectionEntries"):
            if not self.is_other_legion_item(str(se.get("name") or "")):
                self.collect_weapons_only(se, unit, visited, depth + 1)

        for el in get_dict_list(node, "entryLinks"):
            tid = str(el.get("targetId") or "")
            if tid in self.entries_by_id and tid not in visited:
                t_entry = self.entries_by_id[tid]
                if not self.is_other_legion_item(str(t_entry.get("name") or "")):
                    self.collect_weapons_only(t_entry, unit, visited | {tid}, depth + 1)

    def collect_from_node(
        self,
        node: Any,
        unit: ResolvedUnit,
        visited: Set[str],
        depth: int = 0,
        is_mandatory_context: bool = True,
        group_label: str = "",
        default_entry_id: str = "",
    ) -> float:
        if not isinstance(node, dict) or depth > 10:
            return 0.0

        mandatory_model_pts = 0.0

        if is_mandatory_context:
            for p in get_dict_list(node, "profiles", "sharedProfiles"):
                pid = str(p.get("id") or "")
                p_item = self.profiles_by_id.get(pid) or ProfileItem(
                    id=pid,
                    name=str(p.get("name") or "").strip(),
                    type_name=str(p.get("typeName") or "").strip(),
                    characteristics=self.extract_characteristics(p),
                    source_ref=self.format_source_ref(p),
                )
                if not self.is_other_legion_item(p_item.name):
                    self._assign_profile(p_item, unit)

            for r in get_dict_list(node, "rules", "sharedRules"):
                rid = str(r.get("id") or "")
                rname = str(r.get("name") or "").strip()
                rdesc = self.clean_text(str(r.get("description") or ""))
                sref = self.format_source_ref(r)
                if rname and not self.is_other_legion_item(rname):
                    unit.rule_names.add(rname)
                    if rdesc and rid not in self.rules_by_id:
                        unit.inline_rules.append(RuleItem(id=rid, name=rname, description=rdesc, source_ref=sref))

            for ig in get_dict_list(node, "infoGroups", "sharedInfoGroups"):
                if not self.is_other_legion_item(str(ig.get("name") or "")):
                    self.collect_from_node(ig, unit, visited, depth + 1, is_mandatory_context=True)

            for il in get_dict_list(node, "infoLinks"):
                tid = str(il.get("targetId") or "")
                if not tid:
                    continue
                if tid in self.rules_by_id:
                    target_rule = self.rules_by_id[tid]
                    resolved_name = self.resolve_parameterized_name(il, target_rule.name)
                    if not self.is_other_legion_item(resolved_name):
                        unit.rule_names.add(resolved_name)
                elif tid in self.profiles_by_id:
                    target_prof = self.profiles_by_id[tid]
                    resolved_name = self.resolve_parameterized_name(il, target_prof.name)
                    if not self.is_other_legion_item(resolved_name):
                        cloned = ProfileItem(
                            id=target_prof.id,
                            name=resolved_name,
                            type_name=target_prof.type_name,
                            characteristics=target_prof.characteristics,
                            source_file=target_prof.source_file,
                            source_ref=target_prof.source_ref,
                        )
                        self._assign_profile(cloned, unit)
                elif tid in self.info_groups_by_id and tid not in visited:
                    ig_target = self.info_groups_by_id[tid]
                    if not self.is_other_legion_item(str(ig_target.get("name") or "")):
                        visited.add(tid)
                        self.collect_from_node(ig_target, unit, visited, depth + 1, is_mandatory_context=True)

        # Child SelectionEntries
        for se in get_dict_list(node, "selectionEntries"):
            se_id = str(se.get("id") or "")
            se_name = str(se.get("name") or "").strip()
            if not se_name or self.is_other_legion_item(se_name) or se_name.lower() in GLOBAL_META_UPGRADES:
                continue
            se_type = str(se.get("type") or "").lower()
            se_pts = self.extract_pts(se)
            min_c, max_c = self.extract_constraints(se)
            is_def = (se_id != "" and se_id == default_entry_id)

            if se_type == "model":
                # Only treat a child model as mandatory if min_c >= 1 (never assume min=None is mandatory!)
                eff_min = min_c if (min_c is not None and min_c >= 1) else 0
                if eff_min >= 1:
                    pts_note = f" ({int(se_pts)} pts each)" if se_pts > 0 else ""
                    unit.unit_composition.append(f"{eff_min}x {se_name}{pts_note}")
                    if se_pts > 0:
                        mandatory_model_pts += eff_min * se_pts
                        unit.model_points_breakdown.append(f"{eff_min}x {se_name} = {int(eff_min * se_pts)} pts")
                if max_c is None or max_c > eff_min:
                    if se_pts > 0:
                        max_add = (max_c - eff_min) if max_c is not None else None
                        unit.additional_models.append(
                            AdditionalModelOption(
                                id=slugify(f"add_{se_name}"),
                                name=se_name,
                                cost_pts=se_pts,
                                min_added=0,
                                max_added=max_add,
                                base_count=eff_min,
                            )
                        )
                        unit.upgrades.append(
                            UpgradeOption(
                                id=slugify(f"add_{se_name}"),
                                name=f"Additional {se_name}",
                                cost_pts=se_pts,
                                min_qty=eff_min,
                                max_qty=max_c,
                                group_name="Unit Size / Additional Models",
                            )
                        )
                if eff_min >= 1:
                    mandatory_model_pts += self.collect_from_node(
                        se, unit, visited, depth + 1, is_mandatory_context=True
                    )
                else:
                    self.collect_from_node(
                        se, unit, visited, depth + 1, is_mandatory_context=False, group_label=se_name
                    )
            else:
                is_mand_upgrade = is_mandatory_context and (min_c is not None and min_c >= 1) and se_pts == 0
                if is_mand_upgrade or (is_def and se_pts == 0):
                    unit.default_wargear.add(se_name)
                    self.collect_from_node(se, unit, visited, depth + 1, is_mandatory_context=True)
                else:
                    grants = self.extract_grants_from_upgrade(se, visited)
                    unit.upgrades.append(
                        UpgradeOption(
                            id=slugify(f"{group_label}_{se_name}" if group_label else se_name),
                            name=se_name,
                            cost_pts=se_pts,
                            min_qty=min_c,
                            max_qty=max_c,
                            group_name=group_label,
                            is_default=is_def,
                            grants=grants,
                        )
                    )
                    self.collect_weapons_only(se, unit, visited, depth + 1)

        # Child SelectionEntryGroups
        for seg in get_dict_list(node, "selectionEntryGroups"):
            seg_name = str(seg.get("name") or "").strip()
            if self.is_other_legion_item(seg_name) or seg_name.lower() in GLOBAL_META_UPGRADES:
                continue
            def_id = str(seg.get("defaultSelectionEntryId") or "")
            self.collect_from_node(
                seg,
                unit,
                visited,
                depth + 1,
                is_mandatory_context=False,
                group_label=seg_name or group_label,
                default_entry_id=def_id,
            )

        # Child EntryLinks
        for el in get_dict_list(node, "entryLinks"):
            tid = str(el.get("targetId") or "")
            el_id = str(el.get("id") or "")
            if not tid or tid in visited:
                continue
            el_name = str(el.get("name") or "").strip()
            if el_name.lower() in GLOBAL_META_UPGRADES or self.is_other_legion_item(el_name):
                continue

            min_c, max_c = self.extract_constraints(el)
            is_def = (el_id != "" and el_id == default_entry_id) or (tid == default_entry_id)

            if tid in self.entries_by_id:
                target_entry = self.entries_by_id[tid]
                t_name = el_name or str(target_entry.get("name") or "").strip()
                if not t_name or self.is_other_legion_item(t_name) or t_name.lower() in GLOBAL_META_UPGRADES:
                    continue
                t_type = str(target_entry.get("type") or "").lower()
                t_pts = self.extract_pts(el)
                if t_pts == 0:
                    t_pts = self.extract_pts(target_entry)
                if min_c is None and max_c is None:
                    min_c, max_c = self.extract_constraints(target_entry)

                visited.add(tid)
                if t_type == "model":
                    eff_min = min_c if (min_c is not None and min_c >= 1) else 0
                    if eff_min >= 1:
                        pts_note = f" ({int(t_pts)} pts each)" if t_pts > 0 else ""
                        unit.unit_composition.append(f"{eff_min}x {t_name}{pts_note}")
                        if t_pts > 0:
                            mandatory_model_pts += eff_min * t_pts
                            unit.model_points_breakdown.append(f"{eff_min}x {t_name} = {int(eff_min * t_pts)} pts")
                    if max_c is None or max_c > eff_min:
                        if t_pts > 0:
                            max_add = (max_c - eff_min) if max_c is not None else None
                            unit.additional_models.append(
                                AdditionalModelOption(
                                    id=slugify(f"add_{t_name}"),
                                    name=t_name,
                                    cost_pts=t_pts,
                                    min_added=0,
                                    max_added=max_add,
                                    base_count=eff_min,
                                )
                            )
                            unit.upgrades.append(
                                UpgradeOption(
                                    id=slugify(f"add_{t_name}"),
                                    name=f"Additional {t_name}",
                                    cost_pts=t_pts,
                                    min_qty=eff_min,
                                    max_qty=max_c,
                                    group_name="Unit Size / Additional Models",
                                )
                            )
                    if eff_min >= 1:
                        mandatory_model_pts += self.collect_from_node(
                            target_entry, unit, visited, depth + 1, is_mandatory_context=True
                        )
                    else:
                        self.collect_from_node(
                            target_entry, unit, visited, depth + 1, is_mandatory_context=False, group_label=t_name
                        )
                else:
                    is_mand_upgrade = is_mandatory_context and (min_c is not None and min_c >= 1) and t_pts == 0
                    if is_mand_upgrade or (is_def and t_pts == 0):
                        unit.default_wargear.add(t_name)
                        self.collect_from_node(
                            target_entry, unit, visited, depth + 1, is_mandatory_context=True
                        )
                    else:
                        grants = self.extract_grants_from_upgrade(target_entry, visited)
                        unit.upgrades.append(
                            UpgradeOption(
                                id=slugify(f"{group_label}_{t_name}" if group_label else t_name),
                                name=t_name,
                                cost_pts=t_pts,
                                min_qty=min_c,
                                max_qty=max_c,
                                group_name=group_label,
                                is_default=is_def,
                                grants=grants,
                            )
                        )
                        self.collect_weapons_only(target_entry, unit, visited, depth + 1)
                visited.discard(tid)

            elif tid in self.groups_by_id:
                target_group = self.groups_by_id[tid]
                g_name = el_name or str(target_group.get("name") or "").strip()
                if self.is_other_legion_item(g_name) or g_name.lower() in GLOBAL_META_UPGRADES:
                    continue
                if any(k in g_name.lower() for k in ("legion trait", "legiones astartes trait", "faction trait")):
                    continue
                def_id = str(target_group.get("defaultSelectionEntryId") or "")
                visited.add(tid)
                self.collect_from_node(
                    target_group,
                    unit,
                    visited,
                    depth + 1,
                    is_mandatory_context=False,
                    group_label=g_name or group_label,
                    default_entry_id=def_id,
                )
                visited.discard(tid)
            else:
                self.broken_links.add(f"entryLink -> {tid}")

        return mandatory_model_pts

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
        cat_names: List[str] = []
        primary_cat = ""
        for cl in get_dict_list(node, "categoryLinks"):
            tid = str(cl.get("targetId") or "")
            cname = self.categories_by_id.get(tid) or str(cl.get("name") or "").strip()
            if not cname:
                continue
            # Strip trailing explanations like "High Command: ..."
            clean_c = cname.split(":", 1)[0].split(" - ", 1)[0].strip()
            cat_names.append(clean_c)
            if cl.get("primary") in (True, "true") and clean_c.lower() not in NON_ROLE_CATEGORIES:
                primary_cat = clean_c

        if primary_cat:
            return primary_cat

        # Check explicit Battlefield Role priority list
        for role in BATTLEFIELD_ROLE_PRIORITY:
            for c in cat_names:
                if c.lower() == role.lower():
                    return role

        # Check any category name used as a Detachment Force Org slot
        for c in cat_names:
            if c in self.detachment_slot_roles and c.lower() not in NON_ROLE_CATEGORIES:
                return c

        for c in cat_names:
            if c.lower() not in NON_ROLE_CATEGORIES:
                return c
        return "Uncategorized"

    def _compute_base_model_count(self, comp_list: List[str]) -> int:
        total = 0
        for item in comp_list:
            m = re.match(r"^(\d+)x\s+", item)
            if m:
                total += int(m.group(1))
        return total if total > 0 else 1

    def _apply_unit_patches(self, unit: ResolvedUnit) -> None:
        unit_patches = self.patches.get("units", {})
        if unit.name not in unit_patches:
            return
        pdata = unit_patches[unit.name]
        if "base_points" in pdata:
            old_pts = unit.base_points
            unit.base_points = float(pdata["base_points"])
            self.applied_patches_log.append(
                f"Unit Patch: '{unit.name}' base_points {old_pts} -> {unit.base_points}"
            )
        if "source_ref" in pdata:
            unit.source_ref = f"*(Source: {pdata['source_ref']})*"
        if "upgrades" in pdata and isinstance(pdata["upgrades"], dict):
            for upg_name, upg_override in pdata["upgrades"].items():
                for upg in unit.upgrades:
                    if upg.name.lower() == upg_name.lower():
                        if "cost_pts" in upg_override:
                            upg.cost_pts = float(upg_override["cost_pts"])
                            self.applied_patches_log.append(
                                f"Upgrade Patch: '{unit.name}' -> '{upg.name}' cost_pts = {upg.cost_pts}"
                            )

    def extract_units_from_doc(self, doc: Dict[str, Any], scope_label: str = "Armoury") -> List[ResolvedUnit]:
        units: Dict[str, ResolvedUnit] = {}
        candidates_by_name: Dict[str, Dict[str, Any]] = {}

        # 1. Load sharedSelectionEntries and selectionEntries first as base templates
        for se in get_dict_list(doc, "sharedSelectionEntries", "selectionEntries"):
            if se.get("type") in ("unit", "model") or self._has_unit_profile(se):
                uname = str(se.get("name") or "").strip()
                if uname:
                    candidates_by_name[uname] = dict(se)

        # 2. Overlay entryLinks on top so Force Org categoryLinks & root costs are preserved!
        for el in get_dict_list(doc, "entryLinks"):
            tid = str(el.get("targetId") or "")
            if tid and tid in self.entries_by_id:
                target = self.entries_by_id[tid]
                if target.get("type") in ("unit", "model") or self._has_unit_profile(target):
                    uname = str(el.get("name") or target.get("name") or "").strip()
                    if not uname:
                        continue
                    merged = dict(candidates_by_name.get(uname, target))
                    merged["name"] = uname
                    # Combine categoryLinks with entryLink taking priority
                    merged["categoryLinks"] = get_dict_list(el, "categoryLinks") + get_dict_list(target, "categoryLinks")
                    if el.get("costs"):
                        merged["costs"] = el["costs"]
                    candidates_by_name[uname] = merged

        for uname, cand in candidates_by_name.items():
            if not uname or self.is_other_legion_item(uname):
                continue
            cat = self.resolve_category(cand)
            root_pts = self.extract_pts(cand)
            sref = self.format_source_ref(cand)

            ru = ResolvedUnit(
                id=slugify(uname),
                name=uname,
                category=cat,
                catalog_scope=scope_label,
                source_ref=sref,
                root_points=root_pts,
                base_points=root_pts,
            )
            visited: Set[str] = {str(cand.get("id") or "")}
            inherited_model_pts = self.collect_from_node(cand, ru, visited, is_mandatory_context=True)

            if ru.base_points == 0 and inherited_model_pts > 0:
                ru.base_points = inherited_model_pts
            elif ru.base_points > 0 and inherited_model_pts > 0:
                ru.base_points += inherited_model_pts

            ru.base_model_count = self._compute_base_model_count(ru.unit_composition)
            self._apply_unit_patches(ru)

            if ru.unit_profiles or ru.ability_profiles or ru.rule_names or ru.base_points > 0:
                units[uname] = ru

        return sorted(units.values(), key=lambda u: u.name)

    def _has_unit_profile(self, node: Dict[str, Any]) -> bool:
        for p in get_dict_list(node, "profiles", "sharedProfiles"):
            tn = str(p.get("typeName") or "").lower()
            if any(k in tn for k in ("unit", "model", "vehicle")):
                return True
        return False

    def classify_files(self) -> Tuple[List[str], List[str], List[str]]:
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

    @staticmethod
    def render_profile_table(profiles: List[ProfileItem]) -> str:
        if not profiles:
            return ""
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
                name_cell = f"**{p.name}**"
                if p.source_ref:
                    name_cell += f" {p.source_ref}"
                row = [name_cell] + [
                    p.characteristics.get(c, "-").replace("\n", " ").replace("|", "\\|")
                    for c in cols
                ]
                out.append("| " + " | ".join(row) + " |")
            out.append("")
        return "\n".join(out)

    def render_unit_markdown(self, unit: ResolvedUnit, include_weapons: bool = True) -> str:
        header = f"### {unit.name}"
        if unit.source_ref:
            header += f" {unit.source_ref}"
        lines: List[str] = [header]

        role_pts = []
        if unit.category and unit.category != "Uncategorized":
            role_pts.append(f"**Battlefield Role:** {unit.category}")
        if unit.base_points > 0:
            pts_str = f"**Base Points:** {int(unit.base_points)} pts"
            if unit.model_points_breakdown:
                pts_str += f" ({', '.join(unit.model_points_breakdown)})"
            role_pts.append(pts_str)
        if unit.unit_composition:
            role_pts.append(f"**Base Composition:** {', '.join(unit.unit_composition)}")

        if role_pts:
            lines.append("* " + " | ".join(role_pts))
        lines.append("")

        if unit.unit_profiles:
            lines.append(self.render_profile_table(unit.unit_profiles))

        if unit.rule_names:
            sorted_rules = ", ".join(sorted(unit.rule_names))
            lines.append(f"**Base Special Rules:** {sorted_rules}\n")

        if unit.default_wargear:
            sorted_wg = ", ".join(sorted(unit.default_wargear))
            lines.append(f"**Default Wargear (Included in Base Cost):** {sorted_wg}\n")

        if unit.upgrades:
            lines.append("#### Equipment Options & Paid Upgrades")
            seen_upg: Set[str] = set()
            for upg in sorted(unit.upgrades, key=lambda u: (u.group_name, u.cost_pts, u.name)):
                if upg.is_default and upg.cost_pts == 0:
                    cost_str = "0 pts (Default Equipment)"
                elif upg.cost_pts > 0:
                    cost_str = f"+{int(upg.cost_pts)} pts"
                else:
                    cost_str = "0 pts"

                limit_str = f" (max {upg.max_qty})" if upg.max_qty is not None else ""
                grp_prefix = f"[{upg.group_name}] " if upg.group_name else ""
                grants_str = f" — *Grants: {', '.join(upg.grants)}*" if upg.grants else ""
                upg_line = f"* {grp_prefix}**{upg.name}**: {cost_str}{limit_str}{grants_str}"
                if upg_line not in seen_upg:
                    seen_upg.add(upg_line)
                    lines.append(upg_line)
            lines.append("")

        if unit.ability_profiles:
            lines.append("#### Unit-Specific Abilities & Cult Arcana")
            lines.append(self.render_profile_table(unit.ability_profiles))

        if unit.inline_rules:
            for ir in unit.inline_rules:
                ref = f" {ir.source_ref}" if ir.source_ref else ""
                lines.append(f"* **{ir.name}**{ref}: {ir.description}")
            lines.append("")

        if include_weapons and unit.weapon_profiles:
            lines.append("#### Associated Weapons Reference")
            lines.append(self.render_profile_table(unit.weapon_profiles))

        lines.append("---\n")
        return "\n".join(lines)

    def serialize_unit_to_dict(self, unit: ResolvedUnit) -> Dict[str, Any]:
        # Compact upgrade objects (omit null/false/empty keys to keep catalog lightweight)
        clean_upgrades: List[Dict[str, Any]] = []
        seen_upg_keys: Set[str] = set()
        for u in sorted(unit.upgrades, key=lambda x: (x.group_name, x.name, x.cost_pts)):
            if u.group_name == "Unit Size / Additional Models":
                continue
            # Deduplicate identical option + cost on the same unit
            ukey = f"{u.name.lower()}:{u.cost_pts}"
            if ukey in seen_upg_keys:
                continue
            seen_upg_keys.add(ukey)

            upg_obj: Dict[str, Any] = {
                "id": u.id,
                "name": u.name,
                "pts": int(u.cost_pts) if u.cost_pts.is_integer() else u.cost_pts,
            }
            if u.group_name:
                upg_obj["group"] = u.group_name
            if u.max_qty is not None:
                upg_obj["max"] = u.max_qty
            if u.is_default:
                upg_obj["default"] = True
            if u.grants:
                upg_obj["grants"] = u.grants
            clean_upgrades.append(upg_obj)

        clean_add_models: List[Dict[str, Any]] = []
        seen_mod_keys: Set[str] = set()
        for m in unit.additional_models:
            if m.name in seen_mod_keys:
                continue
            seen_mod_keys.add(m.name)
            clean_add_models.append({
                "id": m.id,
                "name": m.name,
                "pts": int(m.cost_pts) if m.cost_pts.is_integer() else m.cost_pts,
                "base_count": m.base_count,
                "max_added": m.max_added,
            })

        unit_dict: Dict[str, Any] = {
            "id": unit.id,
            "name": unit.name,
            "role": unit.category,
            "scope": unit.catalog_scope,
            "base_points": int(unit.base_points) if unit.base_points.is_integer() else unit.base_points,
            "base_models": unit.base_model_count,
            "composition": unit.unit_composition,
            "additional_models": clean_add_models,
            "default_wargear": sorted(unit.default_wargear),
            "upgrades": clean_upgrades,
            "special_rules": sorted(unit.rule_names),
            "profiles": [
                {"name": p.name, "type": p.type_name, "stats": p.characteristics}
                for p in unit.unit_profiles
            ],
        }
        if unit.source_ref:
            unit_dict["source_ref"] = unit.source_ref.replace("*(", "").replace(")*", "")
        return unit_dict

    def write_outputs(self) -> Tuple[int, int, int, int]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        core_files, armoury_files, legion_files = self.classify_files()

        total_units = 0
        total_weapons = 0
        total_rules = 0

        # 1. 01_Universal_Rules_Engine.md
        engine_path = self.output_dir / "01_Universal_Rules_Engine.md"
        core_rules: Dict[str, RuleItem] = {}
        core_abilities: Dict[str, ProfileItem] = {}

        for r in self.rules_by_id.values():
            if r.source_file in core_files and r.description and not self.is_other_legion_item(r.name):
                core_rules[r.name] = r
        for p in self.profiles_by_id.values():
            if p.source_file in core_files and "weapon" not in p.type_name.lower() and not self.is_other_legion_item(p.name):
                core_abilities[f"{p.type_name}:{p.name}"] = p

        with engine_path.open("w", encoding="utf-8") as f:
            f.write("# 01 - Universal Rules Engine, Reactions & Psychic Disciplines\n\n")
            f.write(f"*Compiled from: {', '.join(core_files) or 'Global Index'}*\n\n---\n\n")
            f.write("## 1. Universal Special Rules (USRs)\n\n")
            for rname in sorted(core_rules.keys()):
                rule = core_rules[rname]
                ref = f" {rule.source_ref}" if rule.source_ref else ""
                f.write(f"### {rule.name}{ref}\n{rule.description}\n\n")
                total_rules += 1

            by_type: Dict[str, List[ProfileItem]] = defaultdict(list)
            for p in core_abilities.values():
                by_type[p.type_name or "General Profiles"].append(p)

            f.write("## 2. Core Profiles, Reactions, Traits & Psychic Powers\n\n")
            for tname in sorted(by_type.keys()):
                f.write(f"### {tname}\n\n")
                profs = sorted(by_type[tname], key=lambda x: x.name)
                f.write(self.render_profile_table(profs))
                f.write("\n")

        # 2. 02_Liber_Astartes_Armoury.md
        armoury_path = self.output_dir / "02_Liber_Astartes_Armoury.md"
        armoury_weapons: Dict[str, ProfileItem] = {}
        armoury_wargear: Dict[str, ProfileItem] = {}
        armoury_rules: Dict[str, RuleItem] = {}
        armoury_units: List[ResolvedUnit] = []

        for p in self.profiles_by_id.values():
            if (p.source_file in armoury_files or p.source_file in core_files) and not self.is_other_legion_item(p.name):
                tn = p.type_name.lower()
                if "weapon" in tn:
                    armoury_weapons[p.name] = p
                elif not any(k in tn for k in ("unit", "model", "vehicle")) and p.source_file in armoury_files:
                    armoury_wargear[p.name] = p

        for r in self.rules_by_id.values():
            if r.source_file in armoury_files and r.description and r.name not in core_rules and not self.is_other_legion_item(r.name):
                armoury_rules[r.name] = r

        for fname in armoury_files:
            armoury_units.extend(self.extract_units_from_doc(self.docs_by_file[fname], scope_label="Armoury"))

        with armoury_path.open("w", encoding="utf-8") as f:
            f.write("# 02 - Legiones Astartes Universal Armoury & Standard Units\n\n")
            f.write(f"*Compiled from: {', '.join(armoury_files) or 'Global Armory Index'}*\n\n---\n\n")
            if armoury_rules:
                f.write("## 1. Legion & Armoury Special Rules\n\n")
                for rname in sorted(armoury_rules.keys()):
                    r = armoury_rules[rname]
                    ref = f" {r.source_ref}" if r.source_ref else ""
                    f.write(f"### {r.name}{ref}\n{r.description}\n\n")
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
                f.write("## 4. Standard Legiones Astartes Units (Costs, Options & Profiles)\n\n")
                seen_unames: Set[str] = set()
                for u in sorted(armoury_units, key=lambda x: x.name):
                    if u.name in seen_unames:
                        continue
                    seen_unames.add(u.name)
                    f.write(self.render_unit_markdown(u, include_weapons=False))
                    total_units += 1

        # 3. 03_<Legion>_Legion.md
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
            legion_units.extend(self.extract_units_from_doc(self.docs_by_file[fname], scope_label=self.legion))

        with legion_path.open("w", encoding="utf-8") as f:
            f.write(f"# 03 - {self.legion} Complete Faction Compendium\n\n")
            f.write(f"*Compiled from: {', '.join(legion_files) or 'No matching legion file found'}*\n\n---\n\n")
            if legion_files:
                if legion_rules:
                    f.write(f"## 1. {self.legion} Faction Rules, Cults & Arcana\n\n")
                    for rname in sorted(legion_rules.keys()):
                        r = legion_rules[rname]
                        ref = f" {r.source_ref}" if r.source_ref else ""
                        f.write(f"### {r.name}{ref}\n{r.description}\n\n")
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

        # 4. 04_Detachments_and_Force_Org.md
        det_path = self.output_dir / "04_Detachments_and_Force_Org.md"
        seen_dets: Set[str] = set()
        unique_dets: List[DetachmentItem] = []
        for d in self.detachments:
            key = f"{d.name}:{len(d.slots)}"
            if key not in seen_dets:
                seen_dets.add(key)
                unique_dets.append(d)

        prime_and_rites: List[Tuple[str, str, str]] = []
        for se in self.entries_by_id.values():
            sname = str(se.get("name") or "").strip()
            if not sname or self.is_other_legion_item(sname):
                continue
            s_file = str(se.get("_source_file") or "")
            if s_file not in core_files and s_file not in armoury_files and s_file not in legion_files:
                continue
            if any(k in sname.lower() for k in ("prime ", "rite of war", "detachment", "convocation", "armoured fist", "cohort", "delegation")):
                grants = self.extract_grants_from_upgrade(se, {str(se.get("id") or "")})
                pts = self.extract_pts(se)
                pts_str = f" (+{int(pts)} pts)" if pts > 0 else ""
                desc = self.clean_text(str(se.get("description") or ""))
                if not desc and grants:
                    desc = "Grants / Includes: " + ", ".join(grants)
                if desc or grants:
                    prime_and_rites.append((sname + pts_str, s_file, desc))

        with det_path.open("w", encoding="utf-8") as f:
            f.write("# 04 - Detachments, Force Organisation Charts & Army Construction\n\n")
            f.write("*Compiled from Core System, Legiones Astartes, and Faction Force Entries*\n\n---\n\n")
            f.write("## 1. Force Organisation Detachment Charts & Slot Limits\n\n")
            for d in sorted(unique_dets, key=lambda x: x.name):
                ref = f" {d.source_ref}" if d.source_ref else ""
                f.write(f"### {d.name}{ref}\n")
                f.write(f"* **Type:** {d.detachment_type} | **Source Catalogue:** `{d.source_file}`\n\n")
                if d.description:
                    f.write(f"{d.description}\n\n")
                if d.slots:
                    f.write("| Battlefield Role / Force Org Slot | Min Compulsory | Max Allowed |\n")
                    f.write("| :--- | :---: | :---: |\n")
                    for s in d.slots:
                        max_str = str(s.max_slots) if s.max_slots is not None else "Unlimited"
                        f.write(f"| **{s.name}** | {s.min_slots} | {max_str} |\n")
                    f.write("\n")
                if d.rules:
                    for dr in d.rules:
                        f.write(f"* {dr}\n")
                    f.write("\n")
                f.write("---\n\n")

            if prime_and_rites:
                f.write("## 2. Detachment Unlocks, Prime Advantages & Special Formations\n\n")
                seen_pr: Set[str] = set()
                for title, sfile, desc in sorted(prime_and_rites, key=lambda x: x[0]):
                    if title in seen_pr:
                        continue
                    seen_pr.add(title)
                    f.write(f"### {title}\n* **Source:** `{sfile}`\n* **Details:** {desc}\n\n")

        # 5. Compile roster_catalog.json
        catalog_units_map: Dict[str, Dict[str, Any]] = {}
        for u in sorted(armoury_units, key=lambda x: x.name):
            if u.name not in catalog_units_map:
                catalog_units_map[u.name] = self.serialize_unit_to_dict(u)
        for u in sorted(legion_units, key=lambda x: x.name):
            catalog_units_map[u.name] = self.serialize_unit_to_dict(u)

        catalog_detachments = []
        for d in sorted(unique_dets, key=lambda x: (x.detachment_type, x.name)):
            if not d.slots:
                continue
            catalog_detachments.append({
                "id": d.id,
                "name": d.name,
                "type": d.detachment_type,
                "source_ref": d.source_ref.replace("*(", "").replace(")*", ""),
                "description": d.description,
                "slots": [
                    {"role": s.name, "min": s.min_slots, "max": s.max_slots}
                    for s in d.slots
                ],
                "rules": d.rules,
            })

        cult_arcana_options = ["Corvidae", "Athanaean", "Pavoni", "Pyrae", "Raptora", "Osiraean", "Prosperine"]

        roster_catalog = {
            "metadata": {
                "system": "Horus Heresy 3rd Edition",
                "system_slug": "horus_heresy_3e",
                "faction": self.legion,
                "parser_version": "4.1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "errata_applied": self.applied_patches_log,
            },
            "composition_rules": {
                "validation_mode": "detachment_slots",
                "lord_of_war_max_ratio": 0.50,
                "allied_max_ratio": 0.50,
                "base_reactions": 3,
                "cult_arcana_options": cult_arcana_options,
            },
            "detachments": catalog_detachments,
            "units": list(catalog_units_map.values()),
        }

        catalog_json_path = self.output_dir / "roster_catalog.json"
        with catalog_json_path.open("w", encoding="utf-8") as jf:
            json.dump(roster_catalog, jf, separators=(",", ":"), ensure_ascii=False)

        return len(catalog_units_map), total_weapons, total_rules, len(catalog_detachments)


def main() -> None:
    argp = argparse.ArgumentParser(
        description="Compile BSData into roster_catalog.json and 4 LLM Markdown files."
    )
    argp.add_argument("--input-dir", type=Path, required=True, help="Path to BSData repository.")
    argp.add_argument("--output-dir", type=Path, required=True, help="Output directory.")
    argp.add_argument("--legion", type=str, default="Thousand Sons", help="Target faction/legion name.")
    argp.add_argument("--patches", type=Path, default=None, help="Optional path to errata_patches.json.")

    args = argp.parse_args()

    if not args.input_dir.exists():
        logger.error(f"Input directory does not exist: {args.input_dir}")
        sys.exit(1)

    parser = BSDataParser(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        legion=args.legion,
        patch_file=args.patches,
    )

    files = parser.discover_files()
    if not files:
        logger.error(f"No .json, .cat, or .gst files found in {args.input_dir}")
        sys.exit(1)

    logger.info(f"Pass 1: Indexing {len(files)} BSData files & applying errata patches...")
    parser.run_pass_1(files)

    logger.info("Pass 2: Compiling compact roster_catalog.json and 4 Markdown compendiums...")
    units_cnt, wpns_cnt, rules_cnt, dets_cnt = parser.write_outputs()

    catalog_file = args.output_dir / "roster_catalog.json"
    size_kb = catalog_file.stat().st_size / 1024 if catalog_file.exists() else 0

    print(
        f"\n[SUCCESS] BSData v4.1 Compilation Complete:\n"
        f"  - JSON Catalog  : {catalog_file} ({size_kb:.1f} KB)\n"
        f"  - Units Indexed : {units_cnt:,} units across Armoury & {args.legion}\n"
        f"  - Detachments   : {dets_cnt:,} Force Org charts with slot constraints\n"
        f"  - Errata Patches: {len(parser.applied_patches_log)} applied\n"
    )


if __name__ == "__main__":
    main()
