{
  description = "gitfurl dev shell: Python with tkinter, pytest, ruff, Xvfb, and Wine and bubblewrap for manual testing";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in {
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [ (python3.withPackages (ps: [ ps.tkinter ps.pytest ])) ruff bubblewrap xvfb-run xorg-server xwd ];
        };
        # Adds Wine. Large download; only for running an installer by hand.
        wine = pkgs.mkShell {
          packages = with pkgs; [ (python3.withPackages (ps: [ ps.tkinter ps.pytest ])) ruff bubblewrap xvfb-run xorg-server xwininfo wineWow64Packages.stable ];
        };
      });
    };
}
