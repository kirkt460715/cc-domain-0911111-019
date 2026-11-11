"""可冻结分数计量的纯函数模块。

所有分数计算使用 fractions.Fraction 精确有理数，舍入只发生在展示层，
并通过最大余额法（largest remainder）保证舍入后的小计与总计严格一致。
"""

from fractions import Fraction

from ..contracts import fail
from ..normalization.text import fold

UNKNOWN = None
MEASURE_ALGORITHM = "fractional-count-v1"
COMPARE_ALGORITHM = "release-identity-v1"
KINDS = {"patent", "prototype", "process", "dataset", "software"}


def name_key(name):
    """未知机构（None）排在最后，其余按名称排序。"""
    return (name is None, name or "")


def resolve_chain(alias, mapping):
    """按给定历史词典映射解析别名，返回 (规范名, 跳转链)。"""
    visited = set()
    current = alias
    chain = []
    while True:
        if current in visited:
            fail("cycle", "dictionary contains alias cycle")
        visited.add(current)
        target = mapping.get(current)
        if target is None:
            return current, chain
        chain.append({"from": current, "to": target})
        current = target
        if len(visited) > 100:
            fail("capacity", "alias chain too long")


def canonical_institution(raw_value, mapping):
    """从成员记录的原始机构字段提取规范机构，缺机构归入未知组。"""
    if not isinstance(raw_value, str):
        return UNKNOWN, []
    text = raw_value.strip()
    if not text:
        return UNKNOWN, []
    alias = fold(text)
    if not alias:
        return UNKNOWN, []
    target, chain = resolve_chain(alias, mapping)
    if not target:
        return UNKNOWN, chain
    return target, chain


def distinct_institutions(member_basis):
    """汇总一个成果各成员记录解析出的去重机构。

    member_basis: [{"record", "revision", "raw_institution", "alias",
                    "canonical", "chain"}]
    返回 (排序后的规范机构列表, 规范机构 -> 成员依据列表)。
    """
    grouped = {}
    for basis in member_basis:
        grouped.setdefault(basis["canonical"], []).append(basis)
    names = sorted(grouped, key=name_key)
    return names, grouped


def allocate(mode, names, weights=None):
    """计算单个成果在各去重机构间的贡献分配，单项贡献始终合计为一。"""
    if not names:
        fail("input", "achievement has no institution group")
    if mode == "equal":
        share = Fraction(1, len(names))
        return (
            {name: share for name in names},
            {"mode": "equal", "denominator": len(names), "weights": None},
        )
    if mode != "weighted":
        fail("input", "score mode must be equal or weighted")
    if not weights:
        fail("input", "weighted mode requires explicit weights")
    missing = [name for name in names if name not in weights]
    if missing:
        fail(
            "input",
            "every distinct institution needs a verifiable weight",
            missing=sorted(missing, key=name_key),
        )
    total = sum(weights[name] for name in names)
    return (
        {name: Fraction(weights[name], total) for name in names},
        {
            "mode": "weighted",
            "denominator": total,
            "weights": {name: weights[name] for name in names},
        },
    )


def fraction_text(value):
    if value.denominator == 1:
        return str(value.numerator)
    return str(value.numerator) + "/" + str(value.denominator)


def decimal_text(units, scale):
    """units 是 10**-scale 的整数倍，渲染为定长小数字符串。"""
    factor = 10**scale
    whole, remainder = divmod(units, factor)
    if scale == 0:
        return str(whole)
    return str(whole) + "." + str(remainder).zfill(scale)


def largest_remainder(values, scale, keys):
    """最大余额法把一组 Fraction 舍入到 scale 位小数，总和与整体舍入一致。

    返回与 values 等长的整数（单位 10**-scale）。keys 提供余数并列时的
    确定性次序。
    """
    scaled = [value * (10**scale) for value in values]
    floors = [value.numerator // value.denominator for value in scaled]
    target = (sum(scaled)).numerator // (sum(scaled)).denominator
    remainder = target - sum(floors)
    order = sorted(
        range(len(values)),
        key=lambda index: (
            -(scaled[index].numerator / scaled[index].denominator
              - floors[index]),
            name_key(keys[index]),
        ),
    )
    result = floors[:]
    for index in order[:remainder]:
        result[index] += 1
    return result


def fraction_value(value, scale, units=None):
    if units is None:
        units = value.numerator // value.denominator
    return {
        "fraction": fraction_text(value),
        "decimal": decimal_text(units, scale),
    }


def allocate_units(values, target_units, keys, scale):
    """最大余额法：在各值下限（floor）基础上分配到指定的目标单位总数。

    用于让一组格值的舍入结果严格等于一个已经确定的外围总计，
    从而保证格 -> 行/列 -> 总计逐级可加。values 为精确分数，
    target_units 为 10**-scale 单位下的整数目标。
    """
    factor = 10**scale
    scaled = [value * factor for value in values]
    floors = [value.numerator // value.denominator for value in scaled]
    remainder = target_units - sum(floors)
    if remainder < 0 or remainder > len(values):
        fail(
            "rounding",
            "rounded total cannot be distributed across cells",
            target=target_units,
            floors=sum(floors),
        )
    order = sorted(
        range(len(values)),
        key=lambda index: (
            -(
                scaled[index].numerator / scaled[index].denominator
                - floors[index]
            ),
            name_key(keys[index]),
        ),
    )
    result = floors[:]
    for index in order[:remainder]:
        result[index] += 1
    return result


def build_grid(items, scale):
    """按 机构 × 年份 组装整计数与分数计数并列的汇总格。

    舍入采用两级最大余额法，保证：
    每行各格舍入值之和 == 该行行总计；
    每列各格舍入值之和 == 该列列总计；
    行总计之和 == 列总计之和 == 总计。
    精确分数（fraction）始终保留，舍入只影响展示小数。
    """
    score_cells = {}
    whole_cells = {}
    years = set()
    institutions = set()
    for item in items:
        year = item["year"]
        years.add(year)
        for entry in item["institutions"]:
            name = entry["canonical"]
            institutions.add(name)
            cell = (name, year)
            score_cells[cell] = score_cells.get(cell, Fraction(0)) + Fraction(
                entry["share_numerator"], entry["share_denominator"]
            )
            whole_cells.setdefault(cell, set()).add(item["id"])
    year_columns = sorted(years, key=lambda value: (value is None, value))
    institution_rows = sorted(institutions, key=name_key)

    # 精确的行/列/总计分数。
    exact_row = {
        name: sum(
            (score_cells.get((name, year), Fraction(0)) for year in year_columns),
            Fraction(0),
        )
        for name in institution_rows
    }
    exact_column = {
        year: sum(
            (
                score_cells.get((name, year), Fraction(0))
                for name in institution_rows
            ),
            Fraction(0),
        )
        for year in year_columns
    }
    grand_exact = sum(exact_row.values(), Fraction(0))

    # 第一级：行总计做整体最大余额，确定每行的舍入单位数；
    # 行单位之和天然等于总计的舍入单位数。
    row_target = largest_remainder(
        [exact_row[name] for name in institution_rows],
        scale,
        institution_rows,
    )
    row_target_by_name = dict(zip(institution_rows, row_target))

    # 第二级：每行内部把该行的舍入单位分配到各年份格。
    cell_units = {}
    for name in institution_rows:
        row_values = [
            score_cells.get((name, year), Fraction(0)) for year in year_columns
        ]
        units = allocate_units(
            row_values, row_target_by_name[name], year_columns, scale
        )
        for year, value in zip(year_columns, units):
            cell_units[(name, year)] = value

    cells = []
    for name in institution_rows:
        for year in year_columns:
            exact = score_cells.get((name, year), Fraction(0))
            units = cell_units[(name, year)]
            cell_whole = whole_cells.get((name, year), set())
            cells.append(
                {
                    "institution": name,
                    "year": year,
                    "whole_count": len(cell_whole),
                    "items": sorted(cell_whole),
                    **fraction_value(exact, scale, units),
                }
            )

    row_totals = []
    for name in institution_rows:
        row_item_ids = {
            item_id
            for cell, item_ids in whole_cells.items()
            if cell[0] == name
            for item_id in item_ids
        }
        row_totals.append(
            {
                "institution": name,
                "whole_count": len(row_item_ids),
                "items": sorted(row_item_ids),
                **fraction_value(
                    exact_row[name], scale, row_target_by_name[name]
                ),
            }
        )

    # 列总计直接由已舍入格值求和，保证与各格严格可加。
    column_totals = []
    for year in year_columns:
        column_units = sum(
            cell_units[(name, year)] for name in institution_rows
        )
        column_item_ids = {
            item_id
            for cell, item_ids in whole_cells.items()
            if cell[1] == year
            for item_id in item_ids
        }
        column_totals.append(
            {
                "year": year,
                "whole_count": len(column_item_ids),
                "items": sorted(column_item_ids),
                **fraction_value(exact_column[year], scale, column_units),
            }
        )

    grand_units = sum(row_target)
    grand_total = {
        "whole_count": len(items),
        "items": sorted(item["id"] for item in items),
        **fraction_value(grand_exact, scale, grand_units),
    }
    return {
        "dimensions": {
            "institutions": institution_rows,
            "years": year_columns,
            "unknown_institution_token": None,
        },
        "cells": cells,
        "row_totals": row_totals,
        "column_totals": column_totals,
        "grand_total": grand_total,
        "rounding": {
            "scale": scale,
            "rule": "largest_remainder",
            "note": "cells add exactly to row and column totals; rows, columns and grand total reconcile",
        },
    }


def link_releases(before_items, after_items):
    """基于证据匹配两个发布版的成果，不依赖主记录 ID 同一性。"""
    before = {item["id"]: item for item in before_items}
    after = {item["id"]: item for item in after_items}
    parent = {}
    rank = {}

    def node(side, ident):
        key = (side, ident)
        parent.setdefault(key, key)
        rank.setdefault(key, 0)
        return key

    for ident in before:
        node("b", ident)
    for ident in after:
        node("a", ident)

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def join(left, right):
        a, b = root(left), root(right)
        if a == b:
            return
        if rank[a] < rank[b]:
            a, b = b, a
        parent[b] = a
        if rank[a] == rank[b]:
            rank[a] += 1

    shared_members = {}
    shared_identifiers = {}

    def remember(store, token, left, right):
        store.setdefault(token, {"b": set(), "a": set()})
        store[token]["b"].add(left)
        store[token]["a"].add(right)

    member_index = {}
    identifier_index = {}
    for side, items in (("b", before_items), ("a", after_items)):
        for item in items:
            for member in item["members"]:
                member_index.setdefault(member, []).append((side, item["id"]))
            for identifier in item["identifiers"]:
                identifier_index.setdefault(
                    (identifier["namespace"], identifier["value"]), []
                ).append((side, item["id"]))
    for token, occurrences in member_index.items():
        sides = {side for side, _ in occurrences}
        if len(sides) < 2:
            continue
        lefts = sorted(ident for side, ident in occurrences if side == "b")
        rights = sorted(ident for side, ident in occurrences if side == "a")
        for left in lefts:
            for right in rights:
                remember(shared_members, token, left, right)
                join(node("b", left), node("a", right))
    for token, occurrences in identifier_index.items():
        sides = {side for side, _ in occurrences}
        if len(sides) < 2:
            continue
        lefts = sorted(ident for side, ident in occurrences if side == "b")
        rights = sorted(ident for side, ident in occurrences if side == "a")
        for left in lefts:
            for right in rights:
                remember(shared_identifiers, token, left, right)
                join(node("b", left), node("a", right))

    title_matches = []
    strong_touched = set()
    for token, value in shared_members.items():
        for ident in value["b"]:
            strong_touched.add(("b", ident))
        for ident in value["a"]:
            strong_touched.add(("a", ident))
    for token, value in shared_identifiers.items():
        for ident in value["b"]:
            strong_touched.add(("b", ident))
        for ident in value["a"]:
            strong_touched.add(("a", ident))
    free_before = sorted(
        ident for ident in before if ("b", ident) not in strong_touched
    )
    free_after = sorted(
        ident for ident in after if ("a", ident) not in strong_touched
    )
    candidates = []
    for left in free_before:
        for right in free_after:
            old, new = before[left], after[right]
            if fold(old["title"]) != fold(new["title"]) or old["kind"] != new["kind"]:
                continue
            year_delta = None
            if old["year"] is not None and new["year"] is not None:
                year_delta = abs(old["year"] - new["year"])
            candidates.append(
                (
                    year_delta is None,
                    year_delta if year_delta is not None else 0,
                    left,
                    right,
                )
            )
    matched_after = set()
    for _, year_delta, left, right in sorted(candidates):
        if right in matched_after:
            continue
        matched_after.add(right)
        title_matches.append(
            {
                "before": left,
                "after": right,
                "year_delta": year_delta,
                "basis": "folded_title_and_kind",
            }
        )
        join(node("b", left), node("a", right))

    groups = {}
    for key in parent:
        groups.setdefault(root(key), []).append(key)

    components = []
    for keys in groups.values():
        old_ids = sorted(ident for side, ident in keys if side == "b")
        new_ids = sorted(ident for side, ident in keys if side == "a")
        evidence = {
            "shared_members": [
                {"member": token, "before": sorted(value["b"]),
                 "after": sorted(value["a"])}
                for token, value in sorted(shared_members.items())
                if value["b"] & set(old_ids) and value["a"] & set(new_ids)
            ],
            "shared_identifiers": [
                {"namespace": token[0], "value": token[1],
                 "before": sorted(value["b"]), "after": sorted(value["a"])}
                for token, value in sorted(shared_identifiers.items())
                if value["b"] & set(old_ids) and value["a"] & set(new_ids)
            ],
            "title_matches": [
                row
                for row in title_matches
                if row["before"] in old_ids and row["after"] in new_ids
            ],
        }
        if not old_ids:
            kind = "added"
        elif not new_ids:
            kind = "withdrawn"
        elif len(old_ids) == 1 and len(new_ids) == 1:
            kind = "continued"
        elif len(old_ids) == 1:
            kind = "split"
        elif len(new_ids) == 1:
            kind = "merge"
        else:
            kind = "restructured"
        component = {
            "kind": kind,
            "before": old_ids,
            "after": new_ids,
            "before_master_ids": [
                before[ident].get("master_id", ident) for ident in old_ids
            ],
            "after_master_ids": [
                after[ident].get("master_id", ident) for ident in new_ids
            ],
            "evidence": evidence,
            "revisions": [],
        }
        if kind == "continued":
            old, new = before[old_ids[0]], after[new_ids[0]]
            if fold(old["title"]) != fold(new["title"]):
                component["revisions"].append(
                    {"field": "title", "before": old["title"], "after": new["title"]}
                )
            if old["kind"] != new["kind"]:
                component["revisions"].append(
                    {"field": "kind", "before": old["kind"], "after": new["kind"]}
                )
            if old["year"] != new["year"]:
                component["revisions"].append(
                    {"field": "year", "before": old["year"], "after": new["year"]}
                )
            old_inst = {
                row["canonical"] for row in old["institutions"] if row["canonical"]
            }
            new_inst = {
                row["canonical"] for row in new["institutions"] if row["canonical"]
            }
            if old_inst != new_inst:
                component["revisions"].append(
                    {
                        "field": "institution",
                        "before": sorted(old_inst),
                        "after": sorted(new_inst),
                    }
                )
        components.append(component)
    components.sort(key=lambda row: (row["kind"], row["before"], row["after"]))
    summary = {}
    for component in components:
        summary[component["kind"]] = summary.get(component["kind"], 0) + 1
    return {
        "algorithm": COMPARE_ALGORITHM,
        "identity_rule": "shared member records, shared external identifiers, then folded title plus kind; master ids are never treated as proof",
        "components": components,
        "summary": dict(sorted(summary.items())),
    }
