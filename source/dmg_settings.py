# Layout of the macOS disk image. build.sh passes it to dmgbuild:
#   dmgbuild -s dmg_settings.py -D app_dir=<folder with the two apps> -D icons_dir=<icons folder> ...
import os

app_dir = defines.get("app_dir", ".")      # noqa: F821  (dmgbuild provides "defines")
icons_dir = defines.get("icons_dir", "icons")  # noqa: F821

files = [
    os.path.join(app_dir, "SchoologyGrades.app"),
    os.path.join(app_dir, "SchoologySetup.app"),
]
symlinks = {"Applications": "/Applications"}

background = os.path.join(icons_dir, "dmg_background.png")
window_rect = ((200, 120), (660, 420))
icon_size = 104
text_size = 13
icon_locations = {
    "SchoologyGrades.app": (170, 140),
    "SchoologySetup.app": (170, 290),
    "Applications": (500, 215),
}

format = "UDZO"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
