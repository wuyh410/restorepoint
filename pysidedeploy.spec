[app]
title = RestorePoint
project_dir = .
input_file = app.py
exec_directory = build/qt-output
project_file = pyproject.toml
icon =

[python]
packages = Nuitka==4.1.1,ordered-set,zstandard

[qt]
qml_files =
excluded_qml_plugins =
modules = Core,Gui,Widgets
plugins = imageformats,iconengines,platforms

[nuitka]
mode = standalone
macos.permissions =
extra_args = --noinclude-qt-translations --lto=no --static-libpython=no
