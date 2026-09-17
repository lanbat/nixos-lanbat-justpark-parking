# modules/parking-guard.nix
#
# lanbat plugin: JustPark allowlist sync + Frigate LPR unauthorized parking alerts.
#
# Requires lanbatPlugins.services on the same host (Frigate, Mosquitto, Home Assistant).
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.lanbat.deployment.parkingGuard or null;

  parkingGuardPkg = pkgs.callPackage ../pkgs/parking-guard { };

  guardConfig = pkgs.writeText "parking-guard-config.json" (
    builtins.toJSON {
      stateDir = "/var/lib/parking-guard";
      mqttHost = "127.0.0.1";
      mqttPort = 1883;
      mqttUser = "parking-guard";
      mqttPasswordFile = config.age.secrets.parking-guard-mqtt-pass.path;
      mqttTopicLpr = "frigate/tracked_object_update";
      mqttTopicPrefix = "parking-guard";
      cameras = cfg.cameras;
      minScore = cfg.minScore;
      graceMinutes = cfg.graceMinutes;
      cooldownMinutes = cfg.cooldownMinutes;
      residentPlates = cfg.residentPlates;
      nvrDomain = "nvr.${config.lanbat.deployment.domain}";
    }
  );
in
{
  config = lib.mkIf (cfg != null) {
    lanbat.services.parking-guard = {
      tier = "always-on";
      units = [
        "parking-guard"
        "parking-guard-sync"
        "parking-guard-csv-import"
      ];
      secrets = {
        justpark-api-env = { };
        parking-guard-mqtt-pass = { };
        parking-guard-notify-env = { };
      };
      account = {
        name = "parking-guard";
        uid = 986;
      };
    };

    lanbat.mosquitto.extraUsers.parking-guard = {
      passwordFile = config.age.secrets.parking-guard-mqtt-pass.path;
      acl = [
        "read frigate/tracked_object_update"
        "write parking-guard/#"
      ];
    };

    systemd.tmpfiles.rules = [
      "d /var/lib/parking-guard 0750 parking-guard parking-guard -"
      "d /var/lib/parking-guard/import 0750 parking-guard parking-guard -"
    ];

    environment.etc."parking-guard/config.json".source = guardConfig;

    systemd.services.parking-guard = {
      description = "Frigate LPR parking enforcement evaluator";
      wantedBy = [ "multi-user.target" ];
      after = [
        "mosquitto.service"
        "network-online.target"
      ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "simple";
        User = "parking-guard";
        Group = "parking-guard";
        ExecStart = "${parkingGuardPkg}/bin/parking-guard --config /etc/parking-guard/config.json";
        Restart = "on-failure";
        RestartSec = "10s";
        EnvironmentFile = [
          config.age.secrets.justpark-api-env.path
        ];
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "/var/lib/parking-guard" ];
        PrivateTmp = true;
      };
    };

    systemd.services.parking-guard-sync = {
      description = "Sync JustPark / CSV parking allowlist";
      serviceConfig = {
        Type = "oneshot";
        User = "parking-guard";
        Group = "parking-guard";
        ExecStart = "${parkingGuardPkg}/bin/parking-guard-sync --config /etc/parking-guard/config.json";
        EnvironmentFile = [
          config.age.secrets.justpark-api-env.path
        ];
        Environment = [
          "JUSTPARK_SITE_ID=${cfg.siteId}"
        ];
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "/var/lib/parking-guard" ];
      };
    };

    systemd.timers.parking-guard-sync = {
      description = "Periodic JustPark / CSV allowlist sync";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = "${toString cfg.syncIntervalMinutes}min";
        Persistent = true;
      };
    };

    systemd.paths.parking-guard-csv-import = {
      description = "Import Luna CSV when bookings.csv changes";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathChanged = "/var/lib/parking-guard/import/bookings.csv";
        Unit = "parking-guard-csv-import.service";
        MakeDirectory = true;
      };
    };

    systemd.services.parking-guard-csv-import = {
      description = "Import Luna CSV booking allowlist";
      serviceConfig = {
        Type = "oneshot";
        User = "parking-guard";
        Group = "parking-guard";
        ExecStart = "${parkingGuardPkg}/bin/parking-guard-sync --config /etc/parking-guard/config.json";
        EnvironmentFile = [
          config.age.secrets.justpark-api-env.path
        ];
        Environment = [
          "JUSTPARK_SITE_ID=${cfg.siteId}"
        ];
      };
    };
  };
}
