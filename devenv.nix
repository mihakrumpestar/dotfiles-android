{
  pkgs,
  ...
}:
{
  # adb and fastboot for talking to the device; go for the tooling.
  packages = with pkgs; [
    android-tools
    go-task
  ];

  languages.go.enable = true;

  enterTest = ''
    go test ./...
  '';
}
