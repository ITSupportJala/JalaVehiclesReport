{ pkgs }: {
  deps = [
    pkgs.python311
    pkgs.sqlite
    pkgs.openssl
    pkgs.libffi
    pkgs.zlib
  ];
}
