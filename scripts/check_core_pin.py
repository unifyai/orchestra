import importlib.metadata
import re
import sys
from pathlib import Path


def main() -> int:
    pyproject = Path("pyproject.toml").read_text()
    match = re.search(
        r'orchestra-core\s*=\s*\{[^}]*tag\s*=\s*"v(?P<tag>[^"]+)"',
        pyproject,
    )
    if match is None:
        print("FAIL: orchestra-core dependency must be pinned to a v-prefixed tag")
        return 1

    pinned_version = match.group("tag")
    installed_version = importlib.metadata.version("orchestra-core")
    if installed_version != pinned_version:
        print(
            "FAIL: installed orchestra-core version "
            f"{installed_version!r} does not match pyproject tag {pinned_version!r}",
        )
        return 1

    print(f"OK: orchestra-core pin matches installed version {installed_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
