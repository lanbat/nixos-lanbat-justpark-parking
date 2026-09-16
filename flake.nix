{
  description = "lanbat plugin — JustPark booking sync + Frigate LPR unauthorized parking alerts";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    {
      self,
      nixpkgs,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = nixpkgs.lib.genAttrs systems;
    in
    {
      lanbatPlugin = {
        name = "lanbat-justpark-parking";
        version = 1;
        roles = [ "server" ];
        modules = [ ./modules/parking-guard.nix ];
      };

      packages = forAll (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          parking-guard = pkgs.callPackage ./pkgs/parking-guard { };
          default = self.packages.${system}.parking-guard;
        }
      );

      checks = forAll (system: {
        python-tests = self.packages.${system}.parking-guard;
      });

      formatter = forAll (system: nixpkgs.legacyPackages.${system}.nixfmt-tree);
    };
}
