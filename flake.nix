{
  description = "receipt-ledger — レシート画像 → MF クラウド会計 仕訳帳インポート CSV";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAll = nixpkgs.lib.genAttrs systems;

      # 実行時に必要な Python パッケージ。
      pyDeps = ps: [
        ps.pydantic
        ps.pillow
        ps.pillow-heif
        ps.pdf2image
        ps.requests
      ];
      # 追加のランタイム依存 (PDF 変換に poppler が要る)。
      runtimeDeps = pkgs: [ pkgs.poppler-utils ];

      mkPkg = pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = "receipt-ledger";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = pyDeps pkgs.python3Packages;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          # PDF 変換の pdftoppm を PATH に載せる。
          postFixup = ''
            wrapProgram $out/bin/receipt-ledger \
              --prefix PATH : ${pkgs.lib.makeBinPath (runtimeDeps pkgs)}
          '';
          pythonImportsCheck = [ "receipt_ledger" ];
          nativeCheckInputs = [ pkgs.python3Packages.pytest ];
          checkPhase = ''
            runHook preCheck
            pytest -q
            runHook postCheck
          '';
        };
    in
    {
      # NixOS module。import 側で services.receipt-ledger.enable = true にして使う。
      nixosModules.default = import ./nix/module.nix self;

      packages = forAll (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in {
          default = mkPkg pkgs;
          receipt-ledger = mkPkg pkgs;
        });

      devShells = forAll (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: pyDeps ps ++ [ ps.pytest ps.ruff ]))
            ] ++ runtimeDeps pkgs;
          };
        });

      checks = forAll (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in {
          pytest = pkgs.runCommand "receipt-ledger-pytest"
            {
              nativeBuildInputs = [ (pkgs.python3.withPackages (ps: pyDeps ps ++ [ ps.pytest ])) ];
            } ''
            cp -r ${./.} src && cd src
            pytest -q
            touch $out
          '';
        });
    };
}
