{
  description = "Run Minecraft Bedrock for Windows (GDK) on Linux with native Xbox identity";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      pkgs = import nixpkgs {
        system = "x86_64-linux";
        config.allowUnfree = true;
      };
      bolPython = pkgs.python312.withPackages (ps: with ps; [
        pyside6
        cryptography
        packaging
        python-xlib
        certifi
      ]);
      steam-run = pkgs.steam-run.override (prev: {
        targetPkgs = pkgs: prev.targetPkgs pkgs ++ [ pkgs.libxcomposite ];
      });
    in
    {
      packages.x86_64-linux.default = pkgs.stdenv.mkDerivation {
        pname = "bedrock-on-linux";
        version = "2.2.5";

        src = ./.;

        nativeBuildInputs = [ pkgs.makeWrapper ];

        installPhase = ''
          mkdir -p $out/lib/bedrock-on-linux $out/bin $out/share/applications $out/share/icons/hicolor/256x256/apps

          cp -r bol $out/lib/bedrock-on-linux/
          cp bedrock-on-linux $out/lib/bedrock-on-linux/

          cp data/bedrock-on-linux.desktop $out/share/applications/
          cp data/icon.png $out/share/icons/hicolor/256x256/apps/bedrock-on-linux.png

          makeWrapper ${steam-run}/bin/steam-run $out/bin/bedrock-on-linux \
            --add-flags "${bolPython}/bin/python3" \
            --add-flags "$out/lib/bedrock-on-linux/bedrock-on-linux" \
            --prefix PYTHONPATH : "$out/lib/bedrock-on-linux"
        '';

        meta = {
          homepage = "https://github.com/Wyze3306/BedrockOnLinux";
          license = pkgs.lib.licenses.mit;
          mainProgram = "bedrock-on-linux";
        };
      };
    };
}
