"""执行本卡使用的 SHACL Core 子集：targetClass、property、path 和 minCount。"""
from pathlib import Path

from pyoxigraph import NamedNode, RdfFormat, Store

from plantkg.core.contracts import PendingIssue

SH = "http://www.w3.org/ns/shacl#"
RDF_TYPE = NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")


def validate_turtle(turtle: str) -> list[PendingIssue]:
    shapes, data = Store(), Store()
    shapes.load(input=Path(__file__).with_name("shapes.ttl").read_text(encoding="utf-8"), format=RdfFormat.TURTLE)
    data.load(input=turtle, format=RdfFormat.TURTLE)
    issues = []
    for target in shapes.quads_for_pattern(None, NamedNode(SH + "targetClass"), None, None):
        for prop in shapes.quads_for_pattern(target.subject, NamedNode(SH + "property"), None, None):
            paths = list(shapes.quads_for_pattern(prop.object, NamedNode(SH + "path"), None, None))
            counts = list(shapes.quads_for_pattern(prop.object, NamedNode(SH + "minCount"), None, None))
            if len(paths) != 1 or len(counts) != 1:
                raise ValueError("不支持的 SHACL 形状：需要一个 path 和 minCount")
            minimum = int(counts[0].object.value)
            for typed in data.quads_for_pattern(None, RDF_TYPE, target.object, None):
                count = sum(1 for _ in data.quads_for_pattern(typed.subject, paths[0].object, None, None))
                if count < minimum:
                    issues.append(PendingIssue(issue_id=f"b30-shape-{len(issues)}", reason="subject_unknown",
                                               subject=typed.subject.value,
                                               detail=f"{target.subject} 的 {paths[0].object} 少于 {minimum} 项"))
    return issues
