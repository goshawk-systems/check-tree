"""The generic process skills and agent contracts stay short, generic and unable to start agents."""
import pathlib
import re

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent
NEW_SKILLS = {"autonomy": 120, "done-at-the-consumer": 120, "spend": 120, "grounding": 60, "work-by-capability": 120}
AGENTS = ["builder", "test-author"]
FORBIDDEN = re.compile(r"paul|familycare|athena|murmur", re.I)


def frontmatter(path):
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, f"{path} has no frontmatter"
    fields = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    return {k.strip(): v.strip() for k, v in fields.items()}, text


@pytest.mark.parametrize("name,limit", NEW_SKILLS.items())
def test_skill_is_valid_short_and_generic(name, limit):
    path = PKG / "skills" / name / "SKILL.md"
    assert path.exists(), f"missing {path}"
    fm, text = frontmatter(path)
    assert fm.get("name") == name
    assert fm.get("description"), "description is empty"
    assert len(text.splitlines()) < limit, f"{name} has {len(text.splitlines())} lines, limit {limit}"
    hit = FORBIDDEN.search(text)
    assert not hit, f"{name} contains {hit.group(0)!r}"


@pytest.mark.parametrize("name", AGENTS)
def test_agent_is_sonnet_and_cannot_start_agents(name):
    path = PKG / "agents" / f"{name}.md"
    assert path.exists(), f"missing {path}"
    fm, text = frontmatter(path)
    assert fm.get("name") == name
    assert fm.get("model") == "sonnet"
    tools = [t.strip() for t in fm.get("tools", "").split(",") if t.strip()]
    assert tools, "no tools line"
    assert not {"Agent", "Task"} & set(tools), f"{name} may start agents: {tools}"
    assert not FORBIDDEN.search(text)


def test_start_here_points_to_the_new_skills_and_setup_repo():
    text = (PKG / "skills" / "start-here" / "SKILL.md").read_text()
    for name in NEW_SKILLS:
        assert f"/check-tree:{name}" in text
    assert "/check-tree:setup-repo" in text
