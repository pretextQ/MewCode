# 来源：公众号@小林coding
# 简历模版：jianli.xiaolinnote.com
# Agent网站：xiaolinnote.com
# 后端八股网站：xiaolincoding.com

"""Shared utilities for skill-creator scripts."""

from pathlib import Path



# 简历模版：jianli.xiaolinnote.com
# 后端八股网站：xiaolincoding.com
# 来源：公众号@小林coding
# Agent网站：xiaolinnote.com

def parse_skill_md(skill_path: Path) -> tuple[str, str, str]:
    """Parse a SKILL.md file, returning (name, description, full_content)."""
    content = (skill_path / "SKILL.md").read_text()
    lines = content.split("\n")

    # Agent网站：xiaolinnote.com
    # 后端八股网站：xiaolincoding.com
    # 来源：公众号@小林coding
    # 简历模版：jianli.xiaolinnote.com

    if lines[0].strip() != "---":
        raise ValueError("SKILL.md missing frontmatter (no opening ---)")

    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    # 简历模版：jianli.xiaolinnote.com
    # Agent网站：xiaolinnote.com
    # 来源：公众号@小林coding
    # 后端八股网站：xiaolincoding.com

    if end_idx is None:
        raise ValueError("SKILL.md missing frontmatter (no closing ---)")

    name = ""
    description = ""
    frontmatter_lines = lines[1:end_idx]
    i = 0
    while i < len(frontmatter_lines):
        line = frontmatter_lines[i]
        if line.startswith("name:"):
            name = line[len("name:"):].strip().strip('"').strip("'")
        elif line.startswith("description:"):
            value = line[len("description:"):].strip()
            # Handle YAML multiline indicators (>, |, >-, |-)
            if value in (">", "|", ">-", "|-"):
                continuation_lines: list[str] = []
                i += 1
                while i < len(frontmatter_lines) and (frontmatter_lines[i].startswith("  ") or frontmatter_lines[i].startswith("\t")):
                    continuation_lines.append(frontmatter_lines[i].strip())
                    i += 1
                description = " ".join(continuation_lines)
                continue
            else:
                description = value.strip('"').strip("'")
        i += 1

    return name, description, content

    # 后端八股网站：xiaolincoding.com
    # Agent网站：xiaolinnote.com
    # 来源：公众号@小林coding
    # 简历模版：jianli.xiaolinnote.com
