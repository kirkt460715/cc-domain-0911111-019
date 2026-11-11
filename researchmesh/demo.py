from pathlib import Path
from tempfile import TemporaryDirectory
from .service import ResearchMesh


def run():
    with TemporaryDirectory() as directory:
        with ResearchMesh(Path(directory) / "mesh.db") as mesh:
            mesh.sources.register("lab", "Synthetic research laboratory", 10)
            mesh.sources.register("factory", "Synthetic industrial register", 20)
            mesh.sources.register("hub", "Synthetic regional hub", 30)
            first = {
                "title": "Precision sensor calibration",
                "institution": "Example Institute",
                "kind": "prototype",
                "year": 2026,
                "people": ["Example Researcher"],
                "identifiers": {"registry": ["local:instrument-1"]},
                "tags": ["instrument"],
            }
            second = {
                **first,
                "title": "Precision Sensor Calibration",
                "abstract": "Synthetic pilot implementation",
            }
            mesh.sources.snapshot(
                "lab1", "lab", 1, [{"id": "sensor", "data": first}], 0
            )
            mesh.sources.snapshot(
                "factory1", "factory", 1, [{"id": "instrument", "data": second}], 0
            )
            preview = mesh.releases.preview()
            mesh.releases.freeze("separate", preview["digest"], "publisher")
            proposal = mesh.graph.preview("lab:sensor", "factory:instrument")
            assert proposal["allowed"]
            mesh.graph.decide(
                "merge",
                "lab:sensor",
                "factory:instrument",
                "merge",
                proposal["digest"],
                "reviewer",
                "same synthetic registration",
            )
            assert len(mesh.graph.clusters()) == 1
            preview = mesh.releases.preview()
            mesh.releases.freeze("merged", preview["digest"], "publisher")
            difference = mesh.releases.diff("separate", "merged")
            assert len(difference["merges"]) == 1
            assert mesh.graph.impact("merge")["splits_component"]
            mesh.graph.revoke(
                "merge",
                mesh.store.revision("graph"),
                "reviewer",
                "withdraw synthetic decision",
            )
            assert len(mesh.graph.clusters()) == 2
            preview = mesh.releases.preview()
            mesh.releases.freeze("split", preview["digest"], "publisher")
            assert len(mesh.releases.diff("merged", "split")["splits"]) == 1
            path = Path(directory) / "release.json"
            mesh.releases.export("merged", path)
            assert mesh.releases.verify(path)["verified"]
            assert mesh.quality.verify()["verified"]

            # ---- 可冻结分数计量口径演示 ----
            # 重新确认联合成果，并让两家成员机构显式不同，形成跨机构联合计数。
            lab_record = {
                "title": "Precision sensor calibration",
                "institution": "Example Institute",
                "kind": "prototype",
                "year": 2025,
                "identifiers": {"registry": ["local:instrument-1"]},
            }
            factory_record = {
                "title": "Precision sensor calibration",
                "institution": "Industrial Academy",
                "kind": "prototype",
                "year": 2025,
                "identifiers": {"registry": ["local:instrument-1"]},
            }
            hub_record = {
                "title": "Regional shared dataset",
                "institution": "Regional Hub",
                "kind": "dataset",
                "year": 2025,
            }
            mesh.sources.snapshot(
                "lab2", "lab", 2, [{"id": "sensor", "data": lab_record}], 1
            )
            mesh.sources.snapshot(
                "factory2",
                "factory",
                2,
                [{"id": "instrument", "data": factory_record}],
                1,
            )
            mesh.sources.snapshot(
                "hub1", "hub", 1, [{"id": "dataset", "data": hub_record}], 0
            )
            preview = mesh.graph.preview("lab:sensor", "factory:instrument")
            mesh.graph.decide(
                "joint-merge",
                "lab:sensor",
                "factory:instrument",
                "merge",
                preview["digest"],
                "reviewer",
                "cross-institution joint achievement",
            )
            preview = mesh.releases.preview()
            mesh.releases.freeze("annual-2025", preview["digest"], "publisher")

            # 词典修订 1：工业学院别名归并；分析选定该词典版本。
            mesh.dictionary.put(
                "institution",
                "industrial academy",
                "industrial academy group",
                0,
                "official renamed wording",
            )
            mesh.sources.reindex(1)
            # 词典重索引会使既有合并证据过期，评审人按新证据重新确认联合成果。
            preview = mesh.graph.preview("lab:sensor", "factory:instrument")
            mesh.graph.decide(
                "joint-merge-dict1",
                "lab:sensor",
                "factory:instrument",
                "merge",
                preview["digest"],
                "reviewer",
                "reconfirm after dictionary reindex",
            )
            assert len(mesh.graph.clusters()) == 2
            preview = mesh.releases.preview()
            mesh.releases.freeze("annual-2025-dict1", preview["digest"], "publisher")

            measure = mesh.metrics.measure(
                "annual-2025-dict1",
                dictionary_revision=1,
                kinds=["prototype", "dataset"],
                years=[2025],
                mode="equal",
                scale=4,
            )
            joint = next(
                item
                for item in measure["items"]
                if "lab:sensor" in item["members"]
            )
            shares = {row["canonical"]: row["share"] for row in joint["institutions"]}
            assert shares == {
                "example institute": "1/2",
                "industrial academy group": "1/2",
            }
            grid = measure["grid"]
            # 整计数两家机构各完整计一次（合计 3 次计数 > 2 个成果），
            # 分数计数严格等于成果总数 2。
            assert grid["grand_total"]["whole_count"] == 2
            assert grid["grand_total"]["fraction"] == "2"
            row_units = sum(
                int(row["decimal"].replace(".", "")) for row in grid["row_totals"]
            )
            assert row_units == int(grid["grand_total"]["decimal"].replace(".", ""))

            # 加权模式：每个出现机构都必须给可核验权重。
            weighted = mesh.metrics.measure(
                "annual-2025-dict1",
                dictionary_revision=1,
                mode="weighted",
                weights={
                    "example institute": 3,
                    "industrial academy group": 1,
                    "regional hub": 2,
                },
                scale=4,
            )
            weighted_joint = next(
                item
                for item in weighted["items"]
                if "lab:sensor" in item["members"]
            )
            weighted_shares = {
                row["canonical"]: row["share"]
                for row in weighted_joint["institutions"]
            }
            assert weighted_shares == {
                "example institute": "3/4",
                "industrial academy group": "1/4",
            }

            # 冻结交付：后续发布和词典更新不得改写。
            frozen = mesh.metrics.freeze(
                "report-2025", measure, "research-office"
            )
            drill = mesh.metrics.drill(
                "report-2025", institution="Industrial Academy Group", year=2025
            )
            assert drill["cells"][0]["detail"][0]["share"] == "1/2"

            # 词典继续演进（修订 2），已交付分析的机构分组与摘要保持不变。
            mesh.dictionary.put(
                "institution",
                "regional hub",
                "regional science hub",
                1,
                "hub rebranding",
            )
            mesh.sources.reindex(2)
            preview = mesh.graph.preview("lab:sensor", "factory:instrument")
            mesh.graph.decide(
                "joint-merge-dict2",
                "lab:sensor",
                "factory:instrument",
                "merge",
                preview["digest"],
                "reviewer",
                "reconfirm after second reindex",
            )
            preview = mesh.releases.preview()
            mesh.releases.freeze("annual-2025-dict2", preview["digest"], "publisher")
            delivered = mesh.metrics.get("report-2025")["payload"]
            assert delivered["digest"] == frozen["digest"]
            assert delivered["input_summary"]["dictionary_revision"] == 1
            assert "regional science hub" not in str(
                delivered["grid"]["dimensions"]
            )
            assert mesh.dictionary.verify_history()["verified"]

            # 发布版对照：新增/撤回、合并拆分、机构与年份修订，
            # 身份判定基于成员、外部标识与标题证据而非主记录 ID。
            # 两个发布版都基于同一批冻结记录；对照选用词典修订 0，
            # 可复现别名归并前的机构口径，解释口径变化来源。
            comparison = mesh.metrics.compare(
                "annual-2025",
                "annual-2025-dict1",
                dictionary_revision=0,
            )
            assert comparison["identity_algorithm"] == "release-identity-v1"
            # 联合成果在两个发布版间延续，其成员身份证据来自共享成员与外部标识，
            # 而非主记录 ID；这正是"不能只凭主记录 ID 认定同一成果"的落地。
            # 主记录集群 ID 取最小成员键，联合成果在两侧都是 factory:instrument；
            # 真正的身份证据是共享成员记录与外部标识，而不是这个 ID 本身。
            continued = next(
                component
                for component in comparison["identity"]["components"]
                if component["kind"] == "continued"
                and component["before_master_ids"] == ["factory:instrument"]
            )
            member_evidence = {
                row["member"] for row in continued["evidence"]["shared_members"]
            }
            assert member_evidence == {"factory:instrument", "lab:sensor"}
            assert continued["evidence"]["shared_identifiers"]
            mesh.metrics.freeze("report-2025-comparison", comparison, "research-office")

            analysis_path = Path(directory) / "analysis.json"
            mesh.metrics.export("report-2025", analysis_path)
            assert mesh.metrics.verify(analysis_path)["verified"]
            return {
                "status": "ok",
                "merged_masters": 1,
                "revoked_masters": 2,
                "immutable_release_verified": True,
                "fractional_grand_total": grid["grand_total"]["fraction"],
                "whole_count_total": grid["grand_total"]["whole_count"],
                "frozen_analyses": len(mesh.metrics.list()),
                "offline": True,
            }
