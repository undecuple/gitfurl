{
  description = "gitfurl dev shell: Python with tkinter, pytest, ruff, Xvfb, and Wine and bubblewrap for manual testing";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
      python = pkgs: pkgs.python3.withPackages (ps: [ ps.tkinter ps.pytest ]);
    in {
      # The optional native decoder module (gitfurl._codecs, the cm stage), built with maturin
      # from native/gitfurl-codecs; crates come from its Cargo.lock.
      packages = forAll (pkgs: rec {
        codecs = pkgs.python3Packages.buildPythonPackage {
          pname = "gitfurl-codecs";
          version = "0.1.0";
          src = ./native/gitfurl-codecs;
          pyproject = true;
          cargoDeps = pkgs.rustPlatform.importCargoLock {
            lockFile = ./native/gitfurl-codecs/Cargo.lock;
          };
          nativeBuildInputs = with pkgs.rustPlatform; [ cargoSetupHook maturinBuildHook ];
          doCheck = false; # the Rust tests run in the native dev shell and in CI
        };
        default = codecs;
      });
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [ (python pkgs) ruff bubblewrap xvfb-run xorg-server xwd ];
        };
        # Adds Rust and maturin, to build the native decoder module (tools/build_codecs.sh).
        native = pkgs.mkShell {
          packages = with pkgs; [ (python pkgs) ruff xvfb-run xorg-server cargo rustc clippy rustfmt maturin ];
        };
        # Adds Wine. Large download; only for running an installer by hand.
        wine = pkgs.mkShell {
          packages = with pkgs; [ (python pkgs) ruff bubblewrap xvfb-run xorg-server xwininfo wineWow64Packages.stable ];
        };
      });
    };
}
