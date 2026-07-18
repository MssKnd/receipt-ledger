# NixOS module。自分の flake の input として取り込み、
#   services.receipt-ledger.enable = true;
# とする。inbox にファイルが置かれると systemd path unit が scan を発火し、
# 取りこぼし対策に定期 timer でも scan する。
#
# 使い方 (import する側で):
#   imports = [ receipt-ledger.nixosModules.default ];
#   services.receipt-ledger = {
#     enable = true;
#     user = "youruser";               # inbox/CSV を所有するユーザ
#     creditSubAccount = "立替者名";    # 貸方補助科目
#     accounts = [
#       { account = "会議費"; hints = [ "カフェ" "コーヒー" "食事" ]; }
#       ...
#     ];
#     settings.ollama_model = "qwen2.5vl:7b";
#     ntfyUrlFile = config.age.secrets.receipt-ntfy-url.path;  # 任意
#   };

self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.receipt-ledger;
  pkg = self.packages.${pkgs.system}.receipt-ledger;

  # accounts + settings を TOML にまとめる。
  tomlFormat = pkgs.formats.toml { };
  configFile = tomlFormat.generate "receipt-ledger.toml" (
    {
      base_dir = cfg.baseDir;
      credit_account = cfg.creditAccount;
      credit_sub_account = cfg.creditSubAccount;
      fallback_account = cfg.fallbackAccount;
      accounts = cfg.accounts;
    }
    // cfg.settings
  );

  # imported/ はサービス自身は触らない (MF 取込済み CSV の手動移動先, ADR-1)
  # が、状態機械のディレクトリ一式はここで揃える。
  subdirs = [ "inbox" "processed" "failed" "review" "csv" "imported" ];
in
{
  options.services.receipt-ledger = {
    enable = lib.mkEnableOption "receipt-ledger レシート→仕訳CSVパイプライン";

    user = lib.mkOption {
      type = lib.types.str;
      example = "youruser";
      description = "処理を実行するユーザ (inbox/CSV を所有する。SMB 共有と同じ所有者にする)。";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "users";
      description = "実行グループ。";
    };

    baseDir = lib.mkOption {
      type = lib.types.str;
      default = "/data/nobackup/receipts";
      description = "inbox/processed/failed/review/csv を置くルート。";
    };

    creditAccount = lib.mkOption {
      type = lib.types.str;
      default = "短期借入金";
      description = "貸方勘定科目 (固定)。";
    };

    creditSubAccount = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "立替者名";
      description = "貸方補助科目 (固定)。立替者名などを import する側で設定する。";
    };

    fallbackAccount = lib.mkOption {
      type = lib.types.str;
      default = "雑費";
      description = "借方科目を自動判定できないときのフォールバック。";
    };

    accounts = lib.mkOption {
      type = lib.types.listOf (lib.types.attrsOf lib.types.anything);
      default = [ ];
      example = [
        { account = "会議費"; hints = [ "カフェ" "コーヒー" ]; }
        { account = "新聞図書費"; sub_account = ""; hints = [ "書店" "書籍" ]; }
      ];
      description = "借方勘定科目ホワイトリスト。account / sub_account / hints を持つ。";
    };

    settings = lib.mkOption {
      type = tomlFormat.type;
      default = { };
      example = { ollama_model = "qwen2.5vl:7b"; min_confidence = 0.6; };
      description = "その他の設定 (Config のキー)。TOML にマージされる。";
    };

    ntfyUrlFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "ntfy 通知先 URL を含むファイル (agenix secret のパスなど)。";
    };

    ntfyTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "ntfy トークンを含むファイル (任意)。";
    };

    interval = lib.mkOption {
      type = lib.types.str;
      default = "hourly";
      description = "取りこぼし対策の定期スキャン間隔 (systemd OnCalendar)。";
    };
  };

  config = lib.mkIf cfg.enable {
    # ディレクトリを用意 (所有者は実行ユーザ、Samba 書き込みと揃える)。
    systemd.tmpfiles.rules =
      [ "d ${cfg.baseDir} 0755 ${cfg.user} ${cfg.group} - -" ]
      ++ map (d: "d ${cfg.baseDir}/${d} 0755 ${cfg.user} ${cfg.group} - -") subdirs;

    systemd.services.receipt-ledger = {
      description = "レシート inbox をスキャンして MF 仕訳 CSV を生成";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [ pkg ];
      environment.RECEIPT_LEDGER_CONFIG = "${configFile}";
      # secret は LoadCredential で渡す: systemd が root として読み、サービス
      # ユーザに $CREDENTIALS_DIRECTORY 経由で見せる。これで agenix 既定の
      # root:root 0400 のままでもサービスユーザから読めて、権限不足で scan 前に
      # ユニットごと落ちる問題 (bash -e) を避けられる。
      script = ''
        ${lib.optionalString (cfg.ntfyUrlFile != null)
          ''export RECEIPT_LEDGER_NTFY_URL="$(cat "$CREDENTIALS_DIRECTORY/ntfy-url")"''}
        ${lib.optionalString (cfg.ntfyTokenFile != null)
          ''export RECEIPT_LEDGER_NTFY_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/ntfy-token")"''}
        exec ${pkg}/bin/receipt-ledger scan
      '';
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        UMask = "0022";
        LoadCredential =
          lib.optional (cfg.ntfyUrlFile != null) "ntfy-url:${cfg.ntfyUrlFile}"
          ++ lib.optional (cfg.ntfyTokenFile != null) "ntfy-token:${cfg.ntfyTokenFile}";
        # 同時多重起動を防ぐ (path unit と timer が同時に走らないように)。
        # oneshot なので systemd が直列化するが、念のため。
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.baseDir ];
        ProtectHome = true;
        PrivateTmp = true;
      };
    };

    # inbox に新規ファイル → 即 scan。
    systemd.paths.receipt-ledger = {
      description = "レシート inbox の変更を監視";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathModified = "${cfg.baseDir}/inbox";
        Unit = "receipt-ledger.service";
      };
    };

    # 取りこぼし対策の定期スキャン。
    systemd.timers.receipt-ledger = {
      description = "レシート inbox の定期スキャン";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.interval;
        Persistent = true;
        Unit = "receipt-ledger.service";
      };
    };
  };
}
