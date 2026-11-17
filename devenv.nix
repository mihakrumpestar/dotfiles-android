{
  pkgs,
  ...
}:
{
  # adb (the Android Debug Bridge, the link to the phone) and fastboot come from
  # android-tools, the task runner is go-task, and uv runs the Python tools.
  packages = with pkgs; [
    android-tools
    go-task
    uv
  ];

  languages.python.enable = true;

  enterTest = ''
    uv run pytest -q
  '';
}
