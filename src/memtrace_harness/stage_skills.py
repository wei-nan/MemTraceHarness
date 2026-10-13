"""The Controller's stage skills: plan, review and develop.

The Controller is the only role. Everything else it does is a skill, a packaged ``SKILL.md`` under
``skills/<name>/`` whose front matter says which profile (model, permission, fallback chain) runs it and which
output schema it answers in, and whose ``## section`` blocks hold the stable prompt policy for that skill. The
Harness runs a skill as a fresh CLI call and hands back its artifact; the Controller never sees another skill's
conversation, which is what keeps ``review`` independent of ``develop``.

A SKILL.md section is plain markdown. Lines of one paragraph are joined with a space and a blank line starts a
new line in the prompt, so the file can be wrapped for reading without changing the text the model gets.
Callers add any leading or trailing newline themselves (see loop.py).

What a skill may follow what (``PRECONDITIONS``) is code, not prose: the Controller chooses freely among the
skills that are allowed in the current state, and the Harness refuses the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

SKILLS_DIR = Path(__file__).with_name("skills")
SKILL_NAMES = ("plan", "review", "develop")


@dataclass(frozen=True)
class StageSkill:
    name: str
    description: str
    profile_id: str
    schema: str
    permission: str
    modes: tuple[str, ...]
    body: str
    sections: Mapping[str, str]

    def section(self, key: str) -> str:
        try:
            return self.sections[key]
        except KeyError as exc:
            raise KeyError(f"skill {self.name!r} has no section {key!r}") from exc


def parse_skill(text: str, *, source: str = "SKILL.md") -> StageSkill:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{source}: a skill file starts with a --- front matter block")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError(f"{source}: the front matter is never closed") from None
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    missing = [key for key in ("name", "description", "profile", "schema", "permission") if not meta.get(key)]
    if missing:
        raise ValueError(f"{source}: front matter is missing {', '.join(missing)}")
    if meta["permission"] not in {"read-only", "workspace-write"}:
        raise ValueError(f"{source}: invalid permission {meta['permission']!r}")

    body_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] = body_lines
    for line in lines[end + 1 :]:
        if line.startswith("## "):
            key = line[3:].strip()
            if key in sections:
                raise ValueError(f"{source}: section {key!r} appears twice")
            sections[key] = current = []
        else:
            current.append(line)
    return StageSkill(
        name=meta["name"],
        description=meta["description"],
        profile_id=meta["profile"],
        schema=meta["schema"],
        permission=meta["permission"],
        modes=tuple(item.strip() for item in meta.get("modes", "").split(",") if item.strip()),
        body="\n".join(body_lines).strip(),
        sections={key: _reflow(value) for key, value in sections.items()},
    )


def _reflow(lines: list[str]) -> str:
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            paragraphs[-1].append(line.strip())
        elif paragraphs[-1]:
            paragraphs.append([])
    return "\n".join(" ".join(paragraph) for paragraph in paragraphs if paragraph)


@lru_cache(maxsize=None)
def load_skills(root: Path = SKILLS_DIR) -> Mapping[str, StageSkill]:
    skills: dict[str, StageSkill] = {}
    for path in sorted(root.glob("*/SKILL.md")):
        skill = parse_skill(path.read_text(encoding="utf-8"), source=str(path))
        if skill.name != path.parent.name:
            raise ValueError(f"{path}: name {skill.name!r} does not match its folder")
        skills[skill.name] = skill
    missing = [name for name in SKILL_NAMES if name not in skills]
    if missing:
        raise RuntimeError(f"Packaged stage skills are missing: {', '.join(missing)}")
    return skills


def skill(name: str) -> StageSkill:
    return load_skills()[name]


def section(name: str, key: str) -> str:
    """The text of one ``## key`` block of the named skill."""
    return skill(name).section(key)


def catalog() -> str:
    """What the Controller is told about its skills when it plans phases."""
    lines = []
    for name in SKILL_NAMES:
        item = skill(name)
        modes = f" Modes: {', '.join(item.modes)}." if item.modes else ""
        lines.append(f"- {name} ({item.permission}): {item.description}{modes}")
    return "\n".join(lines)


# skill -> what must already be settled in the current phase before the Controller may run it.
# "plan" = a plan that passed its review (G1); "develop" = a development result (not INVALID);
# "verified" = the plan's review plus the review of the development (G2) and the harness's own build/test.
PRECONDITIONS: Mapping[str, tuple[str, ...]] = {
    "plan": (),
    "review-g1": ("plan",),
    "develop": ("plan", "g1"),
    "review-g2": ("develop",),
}
