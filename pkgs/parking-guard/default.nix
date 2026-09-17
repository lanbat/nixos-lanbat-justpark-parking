{
  lib,
  python3,
  python3Packages,
}:

python3Packages.buildPythonApplication {
  pname = "parking-guard";
  version = "0.1.0";
  pyproject = true;

  src = ./src;

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    paho-mqtt
    pyyaml
    requests
  ];

  nativeCheckInputs = with python3Packages; [
    pytest
    pytestCheckHook
  ];

  pytestFlags = [ "${./tests}" ];

  meta.description = "JustPark allowlist sync and Frigate LPR parking enforcement";
}
