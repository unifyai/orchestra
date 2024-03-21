from typing import List

# Retrieve html_theme_options from docs/conf.py
from docs.conf import html_theme_options

html_theme_options["navbar_end"] = ["theme-switcher", "navbar-icon-links"]
html_theme_options.pop("switcher", None)  # Version switcher shouldn't be in model hub
html_sidebars = {"**": ["custom-toc-tree"]}

project = "Platform"
html_title = "Unify Documentation"

# Removing kapa.ai integration
html_js_files: List[str] = []
