import os
import tempfile
from pathlib import Path

from ..contracts import (
    identifier,
    integer,
    bounded_text,
    fields,
    sequence,
    canonical,
    decode,
    digest,
    fail,
)
from ..normalization.text import fold
from .scoring import (
    UNKNOWN,
    MEASURE_ALGORITHM,
    KINDS,
    name_key,
    canonical_institution,
    distinct_institutions,
    allocate,
    fraction_text,
    largest_remainder,
    decimal_text,
    build_grid,
    link_releases,
)
from fractions import Fraction


class Metrics:
    """从不可变发布版构建可冻结的机构分数计量与发布版对比。"""

    def __init__(self, store, releases, dictionary):
        self.store = store
        self.releases = releases
        self.dictionary = dictionary

    # ---------- 内部工具 ----------

    def _member_raw(self, key, revision, expected_digest):
        row = self.store.one(
            "SELECT raw,raw_digest FROM history WHERE key=? AND revision=?",
            (key, revision),
        )
        if row is None:
            fail("missing", "member revision absent from record history", key=key)
        if row["raw_digest"] != expected_digest:
            fail("integrity", "member raw digest differs from release manifest", key=key)
        raw = decode(row["raw"])
        if digest(raw) != row["raw_digest"]:
            fail("integrity", "member history payload changed", key=key)
        return raw

    def _master_item(self, master, mapping, mode, weights):
        basis = []
        for member in master["members"]:
            manifest = master["member_versions"][member]
            raw = self._member_raw(member, manifest["revision"], manifest["raw_digest"])
            canonical_name, chain = canonical_institution(
                raw.get("institution"), mapping
            )
            alias = fold(raw["institution"]) if isinstance(
                raw.get("institution"), str
            ) and raw["institution"].strip() else ""
            basis.append(
                {
                    "record": member,
                    "revision": manifest["revision"],
                    "raw_institution": raw.get("institution"),
                    "alias": alias,
                    "canonical": canonical_name,
                    "chain": chain,
                }
            )
        names, grouped = distinct_institutions(basis)
        shares, share_rule = allocate(mode, names, weights)
        share_rule = {**share_rule, "weights": self._encode_weights(
            share_rule["weights"]
        )}
        institutions = []
        for name in names:
            share = shares[name]
            institutions.append(
                {
                    "canonical": name,
                    "unknown": name is UNKNOWN,
                    "share_numerator": share.numerator,
                    "share_denominator": share.denominator,
                    "share": fraction_text(share),
                    "members": grouped[name],
                }
            )
        # 条目身份由排序后的成员记录派生，不随主记录集群 ID 变化；
        # 主记录 ID 与指纹仅作为可核验的附加信息。
        stable_id = digest(sorted(master["members"]))[:16]
        return {
            "id": stable_id,
            "master_id": master["id"],
            "title": master["data"]["title"],
            "kind": master["data"]["kind"],
            "year": master["data"]["year"],
            "country": master["data"]["country"],
            "members": list(master["members"]),
            "identifiers": [
                {"namespace": namespace, "value": value}
                for namespace in sorted(master["data"]["identifiers"])
                for value in master["data"]["identifiers"][namespace]
            ],
            "institutions": institutions,
            "share_rule": share_rule,
            "master_digest": master["digest"],
        }

    def _validate_filters(self, kinds, years, year_from, year_to):
        if kinds is not None:
            sequence(kinds, 1, len(KINDS))
            bad = sorted(set(kinds) - KINDS)
            if bad:
                fail("input", "unsupported achievement kind filter", kinds=bad)
        if years is not None:
            sequence(years, 1, 400)
            for value in years:
                integer(value, 1900, 2200)
            if len(set(years)) != len(years):
                fail("input", "year filter contains duplicates")
        if year_from is not None:
            integer(year_from, 1900, 2200)
        if year_to is not None:
            integer(year_to, 1900, 2200)
        if (
            year_from is not None
            and year_to is not None
            and year_from > year_to
        ):
            fail("input", "year interval is reversed")

    def _accept(self, item, kinds, years, year_from, year_to):
        if kinds is not None and item["kind"] not in kinds:
            return False
        if years is not None and item["year"] not in years:
            return False
        year = item["year"]
        if year_from is not None and (year is None or year < year_from):
            return False
        if year_to is not None and (year is None or year > year_to):
            return False
        return True

    def _check_weights(self, weights):
        if not isinstance(weights, dict) or not weights:
            fail("input", "weighted mode requires a nonempty weights object")
        cleaned = {}
        for name, value in weights.items():
            if not isinstance(name, str) or len(name) > 500:
                fail("input", "weight key must be a bounded institution name")
            integer(value, 1)
            # 保留键 unknown 用于给缺机构的未知组提供可核验权重，
            # 因为 JSON 对象无法使用 null 作为键。
            key = UNKNOWN if name == "unknown" else fold(name)
            if key in cleaned:
                fail("input", "weight keys fold to the same institution", key=name)
            cleaned[key] = value
        return cleaned

    @staticmethod
    def _encode_weights(weights):
        """把内部 None（未知组）键转为 JSON 可持久化的保留字符串键。"""
        if weights is None:
            return None
        return {("unknown" if key is UNKNOWN else key): value
                for key, value in sorted(weights.items(), key=lambda row: name_key(row[0]))}

    # ---------- 计量分析 ----------

    def measure(
        self,
        release,
        dictionary_revision=None,
        kinds=None,
        years=None,
        year_from=None,
        year_to=None,
        mode="equal",
        weights=None,
        scale=6,
    ):
        identifier(release)
        integer(scale, 0, 12)
        if mode not in {"equal", "weighted"}:
            fail("input", "score mode must be equal or weighted")
        self._validate_filters(kinds, years, year_from, year_to)
        cleaned_weights = self._check_weights(weights) if mode == "weighted" else None
        snapshot = self.releases.get(release)
        current_dict = self.store.revision("dictionary")
        if dictionary_revision is None:
            dictionary_revision = current_dict
        integer(dictionary_revision, 0)
        if dictionary_revision > current_dict:
            fail("input", "dictionary revision is in the future")
        mapping = self.dictionary.mapping_at("institution", dictionary_revision)
        mapping_digest = digest(sorted(mapping.items()))
        selected = []
        for master in sorted(snapshot["payload"]["masters"], key=lambda row: row["id"]):
            item = self._master_item(master, mapping, mode, cleaned_weights)
            if self._accept(item, kinds, years, year_from, year_to):
                selected.append(item)
        grid = build_grid(selected, scale)
        unknown = sum(1 for item in selected if any(
            row["canonical"] is UNKNOWN for row in item["institutions"]
        ))
        payload = {
            "format_version": 1,
            "algorithm": MEASURE_ALGORITHM,
            "input_summary": {
                "release": release,
                "release_digest": snapshot["digest"],
                "dictionary_revision": dictionary_revision,
                "dictionary_mapping_digest": mapping_digest,
                "filters": {
                    "kinds": sorted(kinds) if kinds else None,
                    "years": sorted(years) if years else None,
                    "year_from": year_from,
                    "year_to": year_to,
                },
                "mode": mode,
                "weights": self._encode_weights(cleaned_weights),
                "scale": scale,
                "candidate_masters": len(snapshot["payload"]["masters"]),
                "included_masters": len(selected),
                "masters_with_unknown_institution": unknown,
            },
            "items": selected,
            "grid": grid,
        }
        payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
        return payload

    MEASURE_FIELDS = {
        "format_version", "algorithm", "input_summary", "items", "grid", "digest"
    }
    COMPARE_FIELDS = {
        "format_version", "algorithm", "identity_algorithm", "input_summary",
        "identity", "explanations", "institution_deltas", "totals", "digest",
    }

    def freeze(self, analysis, payload, operator):
        identifier(analysis)
        identifier(operator)
        if not isinstance(payload, dict):
            fail("input", "analysis payload must be an object")
        allowed = (
            self.COMPARE_FIELDS
            if "identity" in payload
            else self.MEASURE_FIELDS
        )
        fields(payload, allowed, allowed)
        checksum = digest({k: v for k, v in payload.items() if k != "digest"})
        if checksum != payload["digest"]:
            fail("input", "analysis payload digest differs")
        kind = (
            payload["algorithm"]
            if "identity" not in payload
            else "release-comparison-v1"
        )
        with self.store.atomic() as con:
            old = self.store.one("SELECT * FROM analyses WHERE id=?", (analysis,))
            if old:
                if old["digest"] != checksum:
                    fail("conflict", "frozen analysis identity differs")
                return self.get(analysis)
            con.execute(
                "INSERT INTO analyses VALUES(?,?,?,?)",
                (analysis, kind, canonical(payload), checksum),
            )
            self.store.audit(
                "analysis_frozen",
                analysis,
                {
                    "kind": kind,
                    "release": payload["input_summary"].get(
                        "release",
                        payload["input_summary"].get("after_release"),
                    ),
                    "dictionary_revision": payload["input_summary"][
                        "dictionary_revision"
                    ],
                    "digest": checksum,
                    "operator": operator,
                },
            )
        return self.get(analysis)

    def get(self, analysis):
        row = self.store.one("SELECT * FROM analyses WHERE id=?", (analysis,))
        if row is None:
            fail("missing", "frozen analysis not found")
        payload = decode(row["payload"])
        if digest({k: v for k, v in payload.items() if k != "digest"}) != row["digest"]:
            fail("integrity", "analysis payload differs")
        return {"id": analysis, "payload": payload, "digest": row["digest"]}

    def list(self):
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "digest": row["digest"],
            }
            for row in self.store.rows(
                "SELECT id,kind,digest FROM analyses ORDER BY rowid"
            )
        ]

    def drill(self, analysis, institution=None, year=None):
        """从汇总格追到具体成果与权重。"""
        frozen = self.get(analysis)["payload"]
        grid = frozen["grid"]
        if institution is not None:
            bounded_text(institution, 500)
            institution = fold(institution)
        if year is not None:
            integer(year, 1900, 2200)
        rows = []
        for cell in grid["cells"]:
            if institution is not None and (
                cell["institution"] is None or fold(cell["institution"]) != institution
            ):
                continue
            if year is not None and cell["year"] != year:
                continue
            detail_items = []
            for item_id in cell["items"]:
                item = next(row for row in frozen["items"] if row["id"] == item_id)
                entry = next(
                    row
                    for row in item["institutions"]
                    if row["canonical"] == cell["institution"]
                )
                detail_items.append(
                    {
                        "id": item["id"],
                        "master_id": item["master_id"],
                        "year": item["year"],
                        "kind": item["kind"],
                        "share": entry["share"],
                        "members": [
                            basis["record"] for basis in entry["members"]
                        ],
                        "master_digest": item["master_digest"],
                    }
                )
            rows.append({**cell, "detail": detail_items})
        if institution is None and year is not None:
            total = next(
                row for row in grid["column_totals"] if row["year"] == year
            )
        elif institution is not None and year is None:
            total = next(
                row for row in grid["row_totals"]
                if row["institution"] is not None
                and fold(row["institution"]) == institution
            )
        else:
            total = grid["grand_total"]
        return {"analysis": analysis, "cells": rows, "matching_total": total}

    # ---------- 发布版对比 ----------

    def compare(
        self,
        before,
        after,
        dictionary_revision=None,
        kinds=None,
        years=None,
        year_from=None,
        year_to=None,
        mode="equal",
        weights=None,
        scale=6,
    ):
        identifier(before)
        identifier(after)
        if before == after:
            fail("input", "comparison requires two distinct releases")
        integer(scale, 0, 12)
        if mode not in {"equal", "weighted"}:
            fail("input", "score mode must be equal or weighted")
        self._validate_filters(kinds, years, year_from, year_to)
        cleaned_weights = self._check_weights(weights) if mode == "weighted" else None
        left = self.releases.get(before)
        right = self.releases.get(after)
        current_dict = self.store.revision("dictionary")
        if dictionary_revision is None:
            dictionary_revision = current_dict
        integer(dictionary_revision, 0)
        if dictionary_revision > current_dict:
            fail("input", "dictionary revision is in the future")
        mapping = self.dictionary.mapping_at("institution", dictionary_revision)
        mapping_digest = digest(sorted(mapping.items()))

        def items(snapshot):
            result = []
            for master in sorted(snapshot["payload"]["masters"], key=lambda r: r["id"]):
                item = self._master_item(master, mapping, mode, cleaned_weights)
                if self._accept(item, kinds, years, year_from, year_to):
                    result.append(item)
            return result

        before_items = items(left)
        after_items = items(right)
        identity = link_releases(before_items, after_items)

        def totals(rows):
            grid = build_grid(rows, scale)
            scores = {}
            for item in rows:
                for entry in item["institutions"]:
                    name = entry["canonical"]
                    scores[name] = scores.get(name, Fraction(0)) + Fraction(
                        entry["share_numerator"], entry["share_denominator"]
                    )
            return grid, scores

        left_grid, left_scores = totals(before_items)
        right_grid, right_scores = totals(after_items)
        names = sorted(
            set(left_scores) | set(right_scores), key=name_key
        )
        deltas = []
        exact_deltas = [
            right_scores.get(name, Fraction(0)) - left_scores.get(name, Fraction(0))
            for name in names
        ]
        units = largest_remainder(
            [abs(value) for value in exact_deltas], scale, names
        )
        for name, exact, magnitude in zip(names, exact_deltas, units):
            signed = magnitude if exact >= 0 else -magnitude
            deltas.append(
                {
                    "institution": name,
                    "unknown": name is UNKNOWN,
                    "before": fraction_text(left_scores.get(name, Fraction(0))),
                    "after": fraction_text(right_scores.get(name, Fraction(0))),
                    "delta_fraction": fraction_text(exact),
                    "delta_decimal": (
                        ("-" if signed < 0 else "")
                        + decimal_text(abs(signed), scale)
                    ),
                    "signed_delta_units": signed,
                    "before_whole": next(
                        (row["whole_count"] for row in left_grid["row_totals"]
                         if row["institution"] == name),
                        0,
                    ),
                    "after_whole": next(
                        (row["whole_count"] for row in right_grid["row_totals"]
                         if row["institution"] == name),
                        0,
                    ),
                }
            )
        delta_units = list(units)
        signed_delta_units = [row["signed_delta_units"] for row in deltas]
        before_grand_units = int(
            left_grid["grand_total"]["decimal"].replace(".", "")
        )
        after_grand_units = int(
            right_grid["grand_total"]["decimal"].replace(".", "")
        )
        reconciliation = {
            "scale": scale,
            "rule": "largest_remainder",
            "abs_delta_units": sum(delta_units),
            "signed_delta_units": sum(signed_delta_units),
            "after_grand_total_units": after_grand_units,
            "before_grand_total_units": before_grand_units,
            "net_change_reconciles": (
                before_grand_units + sum(signed_delta_units)
                == after_grand_units
            ),
            "note": "signed rounded deltas reconcile before and after grand totals",
        }
        explanations = []
        for component in identity["components"]:
            explanations.append(
                {
                    "component": {
                        "kind": component["kind"],
                        "before": component["before"],
                        "after": component["after"],
                    },
                    "identity_evidence": component["evidence"],
                    "field_revisions": component["revisions"],
                }
            )
        payload = {
            "format_version": 1,
            "algorithm": "release-comparison-v1",
            "identity_algorithm": identity["algorithm"],
            "input_summary": {
                "before_release": before,
                "before_digest": left["digest"],
                "after_release": after,
                "after_digest": right["digest"],
                "dictionary_revision": dictionary_revision,
                "dictionary_mapping_digest": mapping_digest,
                "filters": {
                    "kinds": sorted(kinds) if kinds else None,
                    "years": sorted(years) if years else None,
                    "year_from": year_from,
                    "year_to": year_to,
                },
                "mode": mode,
                "weights": self._encode_weights(cleaned_weights),
                "scale": scale,
            },
            "identity": identity,
            "explanations": explanations,
            "institution_deltas": deltas,
            "totals": {
                "before": left_grid["grand_total"],
                "after": right_grid["grand_total"],
                "reconciliation": reconciliation,
            },
        }
        payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
        return payload

    # ---------- 导出与校验 ----------

    def export(self, analysis, path):
        frozen = self.get(analysis)
        envelope = {
            "type": "researchmesh-analysis",
            "version": 1,
            "id": analysis,
            **{k: v for k, v in frozen.items() if k != "id"},
        }
        content = canonical(envelope).encode("utf-8")
        target = Path(path)
        if target.exists() or target.is_symlink() or not target.parent.is_dir():
            fail("path", "export requires a new file in an existing directory")
        descriptor, name = tempfile.mkstemp(prefix=".mesh-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                fail("path", "export destination appeared concurrently")
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(target), "digest": frozen["digest"], "bytes": len(content)}

    @staticmethod
    def verify(path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100000000:
            fail("path", "analysis must be a bounded regular local file")
        row = decode(path.read_text("utf-8"))
        fields(
            row,
            {"type", "version", "id", "payload", "digest"},
            {"type", "version", "id", "payload", "digest"},
        )
        if row["type"] != "researchmesh-analysis" or row["version"] != 1:
            fail("version", "unknown analysis envelope")
        payload = row["payload"]
        if digest({k: v for k, v in payload.items() if k != "digest"}) != row["digest"]:
            fail("integrity", "analysis fingerprint differs")
        if payload["digest"] != row["digest"]:
            fail("integrity", "analysis inner fingerprint differs")
        grid = payload.get("grid")
        checked = {"id": row["id"], "digest": row["digest"], "verified": True}
        if grid is not None:
            checked["institutions"] = len(grid["dimensions"]["institutions"])
            checked["items"] = len(payload["items"])
        return checked
