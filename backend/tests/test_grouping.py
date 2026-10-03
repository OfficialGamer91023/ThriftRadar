import json
from pathlib import Path

import pytest

from app.grouping import RawMsg, group_messages

CASES_FILE = Path(__file__).resolve().parents[2] / "shared" / "grouping_cases.json"
DOC = json.loads(CASES_FILE.read_text())


@pytest.mark.parametrize("case", DOC["cases"], ids=[c["name"] for c in DOC["cases"]])
def test_shared_grouping_case(case):
    params = DOC["defaults"] | case.get("params", {})
    msgs = [RawMsg(key=m["key"], chat=m["chat"], sender=m["sender"], ts=m["ts"], kind=m["kind"],
                   caption=m.get("caption"), bytes=m.get("bytes", 0), missing=m.get("missing", False))
            for m in case["messages"]]
    groups = group_messages(msgs, idle_s=params["idle_s"], max_span_s=params["max_span_s"],
                            max_images=params["max_images"], max_bytes=params["max_bytes"])
    assert [[m.key for m in g.messages] for g in groups] == case["expected"]


def test_fixture_names_unique():
    names = [c["name"] for c in DOC["cases"]]
    assert len(names) == len(set(names))
