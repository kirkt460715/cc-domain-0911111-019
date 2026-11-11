import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from researchmesh.contracts import MeshError, digest
from researchmesh.service import ResearchMesh
from researchmesh.metrics.scoring import (
    UNKNOWN,
    canonical_institution,
    allocate,
    largest_remainder,
    build_grid,
    link_releases,
)


def record(record_id, title="Joint platform", institution="Institute Alpha",
           kind="software", year=2025, identifiers=None):
    return {
        "id": record_id,
        "data": {
            "title": title,
            "institution": institution,
            "kind": kind,
            "year": year,
            "identifiers": identifiers or {},
        },
    }


class ScoringUnitTests(unittest.TestCase):
    def test_unknown_institution_stays_in_unknown_group(self):
        name, chain = canonical_institution("   ", {})
        self.assertIsNone(name)
        self.assertEqual(chain, [])
        name, _ = canonical_institution(None, {})
        self.assertIsNone(name)

    def test_alias_chain_resolves_against_given_mapping(self):
        mapping = {"alpha lab": "institute alpha", "old alpha": "alpha lab"}
        name, chain = canonical_institution("Old Alpha", mapping)
        self.assertEqual(name, "institute alpha")
        self.assertEqual(
            chain,
            [
                {"from": "old alpha", "to": "alpha lab"},
                {"from": "alpha lab", "to": "institute alpha"},
            ],
        )

    def test_equal_allocation_sums_to_one(self):
        shares, rule = allocate("equal", ["a", "b", None])
        self.assertEqual(sum(shares.values(), Fraction(0)), 1)
        self.assertEqual(rule["denominator"], 3)

    def test_weighted_allocation_uses_verifiable_weights(self):
        shares, rule = allocate(
            "weighted", ["a", "b"], {"a": 3, "b": 1}
        )
        self.assertEqual(shares["a"], Fraction(3, 4))
        self.assertEqual(shares["b"], Fraction(1, 4))
        self.assertEqual(sum(shares.values(), Fraction(0)), 1)
        with self.assertRaises(MeshError):
            allocate("weighted", ["a", "b"], {"a": 3})

    def test_largest_remainder_reconciles_to_total(self):
        values = [Fraction(1, 3), Fraction(1, 3), Fraction(1, 3)]
        units = largest_remainder(values, 2, ["a", "b", "c"])
        self.assertEqual(units, [34, 33, 33])
        self.assertEqual(sum(units), 100)

    def test_grid_subtotals_match_grand_total_after_rounding(self):
        items = [
            {
                "id": "m1",
                "year": 2025,
                "institutions": [
                    {"canonical": "a", "share_numerator": 1,
                     "share_denominator": 3},
                    {"canonical": "b", "share_numerator": 2,
                     "share_denominator": 3},
                ],
            },
            {
                "id": "m2",
                "year": 2025,
                "institutions": [
                    {"canonical": "a", "share_numerator": 1,
                     "share_denominator": 3},
                    {"canonical": UNKNOWN, "share_numerator": 2,
                     "share_denominator": 3},
                ],
            },
            {
                "id": "m3",
                "year": 2026,
                "institutions": [
                    {"canonical": "a", "share_numerator": 1,
                     "share_denominator": 1},
                ],
            },
        ]
        grid = build_grid(items, 2)
        row_units = sum(
            int(row["decimal"].replace(".", "")) for row in grid["row_totals"]
        )
        column_units = sum(
            int(col["decimal"].replace(".", "")) for col in grid["column_totals"]
        )
        grand_units = int(grid["grand_total"]["decimal"].replace(".", ""))
        self.assertEqual(row_units, grand_units)
        self.assertEqual(column_units, grand_units)
        self.assertEqual(grid["grand_total"]["whole_count"], 3)
        self.assertEqual(grid["dimensions"]["institutions"][-1], UNKNOWN)

    def test_rounded_cells_add_exactly_to_rows_columns_and_grand_total(self):
        # 多个机构 × 年份，含 1/2、1/3 等除不尽份额，覆盖 scale 0 的取整压力。
        items = []
        institutions = ["alpha", "beta", "gamma"]
        for index in range(9):
            year = 2024 + index % 3
            items.append(
                {
                    "id": f"m{index}",
                    "year": year,
                    "institutions": [
                        {
                            "canonical": institutions[index % 3],
                            "share_numerator": 1,
                            "share_denominator": 3,
                        },
                        {
                            "canonical": institutions[(index + 1) % 3],
                            "share_numerator": 2,
                            "share_denominator": 3,
                        },
                    ],
                }
            )
        for scale in (0, 2, 6):
            grid = build_grid(items, scale)

            def units(text):
                return int(text.replace(".", ""))

            # 每个机构行：各年格之和严格等于行总计。
            for row in grid["row_totals"]:
                cell_sum = sum(
                    units(cell["decimal"])
                    for cell in grid["cells"]
                    if cell["institution"] == row["institution"]
                )
                self.assertEqual(cell_sum, units(row["decimal"]))
            # 每个年份列：各机构格之和严格等于列总计。
            for col in grid["column_totals"]:
                cell_sum = sum(
                    units(cell["decimal"])
                    for cell in grid["cells"]
                    if cell["year"] == col["year"]
                )
                self.assertEqual(cell_sum, units(col["decimal"]))
            grand = units(grid["grand_total"]["decimal"])
            self.assertEqual(
                sum(units(row["decimal"]) for row in grid["row_totals"]), grand
            )
            self.assertEqual(
                sum(units(col["decimal"]) for col in grid["column_totals"]),
                grand,
            )
            # 精确分数不受舍入影响，仍等于成果总数。
            exact = sum(
                Fraction(entry["share_numerator"], entry["share_denominator"])
                for item in items
                for entry in item["institutions"]
            )
            self.assertEqual(exact, Fraction(len(items)))
            self.assertEqual(grid["grand_total"]["fraction"], str(len(items)))

    def test_identity_never_relies_on_master_id_alone(self):
        before = [
            {"id": "old:one", "title": "alloy", "kind": "process", "year": 2020,
             "members": ["old:one"], "identifiers": [], "institutions": []},
        ]
        after = [
            {"id": "new:one", "title": "alloy", "kind": "process", "year": 2021,
             "members": ["new:one"], "identifiers": [], "institutions": []},
        ]
        result = link_releases(before, after)
        component = result["components"][0]
        self.assertEqual(component["kind"], "continued")
        self.assertTrue(component["evidence"]["title_matches"])

    def test_merge_split_and_added_withdrawn_classification(self):
        before = [
            {"id": "a", "title": "t", "kind": "process", "year": 2020,
             "members": ["a"], "identifiers": [
                 {"namespace": "registry", "value": "x"}], "institutions": []},
            {"id": "b", "title": "t", "kind": "process", "year": 2020,
             "members": ["b"], "identifiers": [
                 {"namespace": "registry", "value": "x"}], "institutions": []},
            {"id": "gone", "title": "g", "kind": "process", "year": 2019,
             "members": ["gone"], "identifiers": [], "institutions": []},
        ]
        after = [
            {"id": "a", "title": "t", "kind": "process", "year": 2020,
             "members": ["a", "b"], "identifiers": [
                 {"namespace": "registry", "value": "x"}], "institutions": []},
            {"id": "fresh", "title": "brand new", "kind": "process", "year": 2021,
             "members": ["fresh"], "identifiers": [], "institutions": []},
        ]
        result = link_releases(before, after)
        summary = result["summary"]
        self.assertEqual(summary.get("merge"), 1)
        self.assertEqual(summary.get("added"), 1)
        self.assertEqual(summary.get("withdrawn"), 1)


class MetricsServiceTests(unittest.TestCase):
    def setUp(self):
        self.m = ResearchMesh()
        self.addCleanup(self.m.close)
        self.m.sources.register("a", "Source A", 10)
        self.m.sources.register("b", "Source B", 20)
        self.m.sources.register("c", "Source C", 30)

    def seed(self):
        self.m.sources.snapshot(
            "a1",
            "a",
            1,
            [
                record(
                    "joint",
                    institution="Institute Alpha",
                    identifiers={"registry": ["local:joint"]},
                ),
                record(
                    "solo",
                    title="Solo work",
                    institution="Institute Beta",
                    kind="dataset",
                    year=2024,
                    identifiers={},
                ),
            ],
            0,
        )
        self.m.sources.snapshot(
            "b1",
            "b",
            1,
            [
                record(
                    "jointb",
                    institution="Institute Beta",
                    identifiers={"registry": ["local:joint"]},
                )
            ],
            0,
        )

    def freeze_release(self, name):
        preview = self.m.releases.preview()
        return self.m.releases.freeze(name, preview["digest"], "publisher")

    def merge_joint(self, decision):
        preview = self.m.graph.preview("a:joint", "b:jointb")
        self.m.graph.decide(
            decision,
            "a:joint",
            "b:jointb",
            "merge",
            preview["digest"],
            "reviewer",
            "same achievement",
        )

    @staticmethod
    def find_item(payload, member):
        return next(
            item for item in payload["items"] if member in item["members"]
        )

    def test_whole_and_fractional_counts_for_joint_achievement(self):
        self.seed()
        self.merge_joint("d1")
        self.freeze_release("r1")
        payload = self.m.metrics.measure("r1")
        joint = self.find_item(payload, "a:joint")
        shares = {
            row["canonical"]: Fraction(
                row["share_numerator"], row["share_denominator"]
            )
            for row in joint["institutions"]
        }
        self.assertEqual(
            shares,
            {"institute alpha": Fraction(1, 2), "institute beta": Fraction(1, 2)},
        )
        grid = payload["grid"]
        alpha = next(
            row for row in grid["row_totals"]
            if row["institution"] == "institute alpha"
        )
        beta = next(
            row for row in grid["row_totals"]
            if row["institution"] == "institute beta"
        )
        # 整计数：联合成果在两家机构各完整计数一次；分数计数按去重机构均分
        self.assertEqual(alpha["whole_count"], 1)
        self.assertEqual(beta["whole_count"], 2)
        self.assertEqual(alpha["fraction"], "1/2")
        self.assertEqual(beta["fraction"], "3/2")
        self.assertEqual(grid["grand_total"]["whole_count"], 2)
        row_units = sum(
            int(row["decimal"].replace(".", "")) for row in grid["row_totals"]
        )
        self.assertEqual(
            row_units, int(grid["grand_total"]["decimal"].replace(".", ""))
        )

    def test_alias_normalization_runs_inside_analysis(self):
        self.seed()
        # b 源使用别名写法，词典修订 1 把它归并到 institute alpha
        self.m.sources.snapshot(
            "b2",
            "b",
            2,
            [
                record(
                    "jointb",
                    institution="Alpha Inst.",
                    identifiers={"registry": ["local:joint"]},
                )
            ],
            1,
        )
        self.m.dictionary.put(
            "institution", "alpha inst.", "institute alpha", 0, "alias"
        )
        self.m.dictionary.put(
            "institution", "institute beta", "Beta Academy", 1, "rename"
        )
        self.m.sources.reindex(2)
        self.freeze_release("r1")
        latest = self.m.metrics.measure("r1")
        names = latest["grid"]["dimensions"]["institutions"]
        self.assertEqual(names, ["beta academy", "institute alpha"])
        rev1 = self.m.metrics.measure("r1", dictionary_revision=1)
        self.assertEqual(
            rev1["grid"]["dimensions"]["institutions"],
            ["institute alpha", "institute beta"],
        )
        # 词典修订 1 含 alpha inst. -> institute alpha 的可核验跳转链
        chains = [
            basis["chain"]
            for item in rev1["items"]
            for institution in item["institutions"]
            for basis in institution["members"]
            if basis["chain"]
        ]
        self.assertTrue(chains)
        self.assertEqual(
            chains[0][0],
            {"from": "alpha inst", "to": "institute alpha"},
        )
        # 修订 0 时别名尚未建立，同一发布版得到不同的机构分组
        rev0 = self.m.metrics.measure("r1", dictionary_revision=0)
        self.assertIn("alpha inst", rev0["grid"]["dimensions"]["institutions"])

    def test_dictionary_update_does_not_rewrite_delivered_analysis(self):
        self.seed()
        self.freeze_release("r1")
        first = self.m.metrics.measure("r1", dictionary_revision=0)
        frozen = self.m.metrics.freeze("report-2025", first, "analyst")
        self.m.dictionary.put(
            "institution", "institute beta", "Beta Academy", 0, "rename"
        )
        delivered = self.m.metrics.get("report-2025")["payload"]
        self.assertEqual(delivered["digest"], frozen["digest"])
        self.assertEqual(
            delivered["grid"]["dimensions"]["institutions"],
            ["institute alpha", "institute beta"],
        )
        self.assertEqual(delivered["input_summary"]["dictionary_revision"], 0)
        self.assertTrue(self.m.dictionary.verify_history()["verified"])
        snapshot = self.m.dictionary.history_snapshot("institution", 0)
        self.assertEqual(snapshot["rows"], [])
        with self.assertRaises(MeshError):
            self.m.metrics.measure("r1", dictionary_revision=99)

    def test_dictionary_removal_is_replayable_from_history(self):
        self.seed()
        self.m.dictionary.put(
            "institution", "alpha inst.", "institute alpha", 0, "alias"
        )
        self.assertEqual(
            self.m.dictionary.resolve_at(
                "institution", "alpha inst", 1
            )["canonical"],
            "institute alpha",
        )
        self.m.dictionary.remove("institution", "alpha inst.", 1, "drop alias")
        # 修订 2 删除别名后，该名称解析回自身
        self.assertEqual(
            self.m.dictionary.resolve_at(
                "institution", "alpha inst", 2
            )["canonical"],
            "alpha inst",
        )
        # 历史修订 1 仍保留归并结果
        self.assertEqual(
            self.m.dictionary.resolve_at(
                "institution", "alpha inst", 1
            )["canonical"],
            "institute alpha",
        )
        self.assertTrue(self.m.dictionary.verify_history()["verified"])

    def test_weighted_mode_requires_complete_verifiable_weights(self):
        self.seed()
        self.merge_joint("d1")
        self.freeze_release("r1")
        payload = self.m.metrics.measure(
            "r1",
            mode="weighted",
            weights={"institute alpha": 3, "institute beta": 1},
        )
        joint = self.find_item(payload, "a:joint")
        shares = {
            row["canonical"]: row["share"] for row in joint["institutions"]
        }
        self.assertEqual(shares, {"institute alpha": "3/4", "institute beta": "1/4"})
        with self.assertRaises(MeshError):
            self.m.metrics.measure(
                "r1", mode="weighted", weights={"institute alpha": 3}
            )

    def test_filters_by_kind_year_and_scale(self):
        self.seed()
        self.freeze_release("r1")
        only_dataset = self.m.metrics.measure("r1", kinds=["dataset"])
        self.assertEqual(only_dataset["input_summary"]["included_masters"], 1)
        year_2024 = self.m.metrics.measure("r1", years=[2024])
        self.assertEqual(len(year_2024["items"]), 1)
        coarse = self.m.metrics.measure("r1", scale=0)
        for cell in coarse["grid"]["cells"]:
            self.assertNotIn(".", cell["decimal"])
        for row in coarse["grid"]["row_totals"]:
            self.assertNotIn(".", row["decimal"])
        with self.assertRaises(MeshError):
            self.m.metrics.measure("r1", kinds=["nonsense"])
        with self.assertRaises(MeshError):
            self.m.metrics.measure("r1", year_from=2030, year_to=2020)

    def test_drill_follows_cell_to_items_and_member_weights(self):
        self.seed()
        self.merge_joint("d1")
        self.freeze_release("r1")
        payload = self.m.metrics.measure("r1")
        self.m.metrics.freeze("frozen", payload, "analyst")
        drill = self.m.metrics.drill(
            "frozen", institution="Institute Beta", year=2025
        )
        self.assertEqual(len(drill["cells"]), 1)
        detail = drill["cells"][0]["detail"]
        master_ids = {row["master_id"] for row in detail}
        self.assertEqual(master_ids, {"a:joint"})
        joint_detail = detail[0]
        self.assertEqual(joint_detail["share"], "1/2")
        self.assertIn("b:jointb", joint_detail["members"])

    def test_missing_institution_remains_in_unknown_group(self):
        self.m.sources.snapshot(
            "a1",
            "a",
            1,
            [
                {
                    "id": "known",
                    "data": {
                        "title": "Documented work",
                        "institution": "Institute Alpha",
                        "kind": "process",
                        "year": 2025,
                    },
                },
                {
                    "id": "mystery",
                    "data": {
                        "title": "Unattributed work",
                        "kind": "process",
                        "year": 2025,
                    },
                },
            ],
            0,
        )
        self.freeze_release("r1")
        payload = self.m.metrics.measure("r1")
        unknown_item = self.find_item(payload, "a:mystery")
        self.assertEqual(len(unknown_item["institutions"]), 1)
        group = unknown_item["institutions"][0]
        self.assertIsNone(group["canonical"])
        self.assertTrue(group["unknown"])
        self.assertEqual(group["share"], "1")
        self.assertIsNone(payload["grid"]["dimensions"]["institutions"][-1])
        unknown_row = next(
            row
            for row in payload["grid"]["row_totals"]
            if row["institution"] is None
        )
        self.assertEqual(unknown_row["whole_count"], 1)

    def test_unknown_group_weight_uses_reserved_key_and_freezes(self):
        self.m.sources.snapshot(
            "a1",
            "a",
            1,
            [
                {
                    "id": "joint",
                    "data": {
                        "title": "Joint work",
                        "institution": "Institute Alpha",
                        "kind": "process",
                        "year": 2025,
                        "identifiers": {"registry": ["local:j"]},
                    },
                }
            ],
            0,
        )
        self.m.sources.snapshot(
            "b1",
            "b",
            1,
            [
                {
                    "id": "jointb",
                    "data": {
                        "title": "Joint work",
                        "kind": "process",
                        "year": 2025,
                        "identifiers": {"registry": ["local:j"]},
                    },
                }
            ],
            0,
        )
        preview = self.m.graph.preview("a:joint", "b:jointb")
        self.m.graph.decide(
            "d1", "a:joint", "b:jointb", "merge", preview["digest"],
            "reviewer", "joint",
        )
        self.freeze_release("r1")
        payload = self.m.metrics.measure(
            "r1",
            mode="weighted",
            weights={"institute alpha": 3, "unknown": 1},
        )
        # 规范化不应因 None 键失败（unknown 已编码为保留字符串键）
        from researchmesh.contracts import canonical as canonical_json

        canonical_json(payload)
        joint = self.find_item(payload, "a:joint")
        shares = {
            ("unknown" if row["canonical"] is None else row["canonical"]):
            row["share"]
            for row in joint["institutions"]
        }
        self.assertEqual(
            shares, {"institute alpha": "3/4", "unknown": "1/4"}
        )
        frozen = self.m.metrics.freeze("weighted-report", payload, "analyst")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "analysis.json"
            self.m.metrics.export("weighted-report", target)
            self.assertTrue(self.m.metrics.verify(target)["verified"])
        # 缺少未知组权重时明确拒绝
        with self.assertRaises(MeshError):
            self.m.metrics.measure(
                "r1", mode="weighted", weights={"institute alpha": 3}
            )
        self.assertEqual(
            self.m.metrics.get("weighted-report")["digest"], frozen["digest"]
        )

    def test_freeze_is_idempotent_and_conflict_safe(self):
        self.seed()
        self.freeze_release("r1")
        payload = self.m.metrics.measure("r1")
        first = self.m.metrics.freeze("one", payload, "analyst")
        again = self.m.metrics.freeze("one", payload, "analyst")
        self.assertEqual(first["digest"], again["digest"])
        with self.assertRaises(MeshError):
            self.m.metrics.freeze("one", {**payload, "digest": "bad"}, "analyst")
        self.assertEqual(self.m.metrics.list()[0]["id"], "one")

    def test_release_comparison_explains_merge_split_and_revisions(self):
        self.seed()
        self.freeze_release("r1")
        self.merge_joint("d1")
        self.freeze_release("r2")
        merge_report = self.m.metrics.compare("r1", "r2")
        self.assertEqual(merge_report["identity"]["summary"].get("merge"), 1)
        component = next(
            row for row in merge_report["identity"]["components"]
            if row["kind"] == "merge"
        )
        self.assertTrue(component["evidence"]["shared_identifiers"])
        # 年份修订后重新确认合并，再发布
        self.m.sources.snapshot(
            "a2",
            "a",
            2,
            [
                record(
                    "joint",
                    year=2026,
                    identifiers={"registry": ["local:joint"]},
                )
            ],
            1,
            complete=True,
        )
        preview = self.m.graph.preview("a:joint", "b:jointb")
        self.m.graph.decide(
            "d2", "a:joint", "b:jointb", "merge", preview["digest"],
            "reviewer", "reconfirm",
        )
        self.freeze_release("r3")
        revised = self.m.metrics.compare("r2", "r3")
        continued = next(
            row for row in revised["identity"]["components"]
            if row["kind"] == "continued"
        )
        self.assertIn(
            {"field": "year", "before": 2025, "after": 2026},
            continued["revisions"],
        )
        # 撤回合并 -> 拆分
        self.m.graph.revoke(
            "d2", self.m.store.revision("graph"), "reviewer", "split"
        )
        self.freeze_release("r4")
        split_report = self.m.metrics.compare("r2", "r4")
        self.assertEqual(split_report["identity"]["summary"].get("split"), 1)
        # 对比本身也可冻结，后续数据变化不影响已交付内容
        frozen = self.m.metrics.freeze("cmp", revised, "analyst")
        self.assertEqual(self.m.metrics.get("cmp")["digest"], frozen["digest"])

    def test_comparison_does_not_count_master_id_as_identity(self):
        self.m.sources.register("x", "Legacy", 40)
        self.m.sources.register("y", "Successor", 50)
        self.m.sources.snapshot(
            "x1", "x", 1,
            [record("p", title="Mystery alloy", institution="Lab One",
                    kind="process", year=2020, identifiers={})],
            0,
        )
        self.freeze_release("s1")
        self.m.sources.snapshot(
            "x2", "x", 2, [], 1, complete=True
        )
        self.m.sources.snapshot(
            "y1", "y", 1,
            [record("q", title="mystery alloy", institution="Lab Two",
                    kind="process", year=2021, identifiers={})],
            0,
        )
        self.freeze_release("s2")
        report = self.m.metrics.compare("s1", "s2")
        component = report["identity"]["components"][0]
        self.assertEqual(component["kind"], "continued")
        self.assertEqual(component["before_master_ids"], ["x:p"])
        self.assertEqual(component["after_master_ids"], ["y:q"])
        self.assertTrue(component["evidence"]["title_matches"])
        self.assertFalse(component["evidence"]["shared_members"])
        fields_changed = {row["field"] for row in component["revisions"]}
        self.assertEqual(fields_changed, {"year", "institution"})

    def test_export_roundtrip_and_tamper_detection(self):
        self.seed()
        self.freeze_release("r1")
        payload = self.m.metrics.measure("r1")
        self.m.metrics.freeze("delivered", payload, "analyst")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "analysis.json"
            exported = self.m.metrics.export("delivered", target)
            self.assertGreater(exported["bytes"], 0)
            checked = self.m.metrics.verify(target)
            self.assertTrue(checked["verified"])
            envelope = json.loads(target.read_text())
            envelope["payload"]["digest"] = "0" * 64
            target.write_text(json.dumps(envelope))
            with self.assertRaises(MeshError):
                self.m.metrics.verify(target)
            with self.assertRaises(MeshError):
                self.m.metrics.export("delivered", target)

    def test_input_summary_is_persisted_with_analysis(self):
        self.seed()
        self.m.dictionary.put(
            "institution", "institute beta", "Beta Academy", 0, "rename"
        )
        self.m.sources.reindex(1)
        self.freeze_release("r1")
        payload = self.m.metrics.measure(
            "r1",
            dictionary_revision=1,
            kinds=["software", "dataset"],
            mode="equal",
            scale=4,
        )
        summary = payload["input_summary"]
        self.assertEqual(summary["release"], "r1")
        self.assertEqual(summary["dictionary_revision"], 1)
        self.assertEqual(summary["filters"]["kinds"], ["dataset", "software"])
        self.assertEqual(summary["scale"], 4)
        self.assertEqual(summary["candidate_masters"], 3)
        self.assertEqual(summary["included_masters"], 3)
        self.assertIn("release_digest", summary)
        self.assertIn("dictionary_mapping_digest", summary)
        self.m.metrics.freeze("archived", payload, "analyst")
        stored = self.m.metrics.get("archived")["payload"]
        self.assertEqual(
            digest({k: v for k, v in stored.items() if k != "digest"}),
            stored["digest"],
        )


if __name__ == "__main__":
    unittest.main()
