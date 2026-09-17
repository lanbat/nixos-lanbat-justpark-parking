# modules/parking-guard.nix
#
# lanbat plugin: JustPark allowlist sync, Frigate LPR unauthorized parking alerts,
# and misparking incidents that notify the booking's client until acknowledged.
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

  stateDir = "/var/lib/parking-guard";

  wahaCfg = cfg.waha or { };
  wahaEnabled = wahaCfg.enable or false;

  guardConfig = pkgs.writeText "parking-guard-config.json" (
    builtins.toJSON {
      stateDir = stateDir;
      mqttHost = "127.0.0.1";
      mqttPort = 1883;
      mqttUser = "parking-guard";
      mqttPasswordFile = config.age.secrets.parking-guard-mqtt-pass.path;
      mqttTopicLpr = "frigate/tracked_object_update";
      mqttTopicEvents = "frigate/events";
      mqttTopicPrefix = "parking-guard";
      cameras = cfg.cameras;
      minScore = cfg.minScore;
      graceMinutes = cfg.graceMinutes;
      cooldownMinutes = cfg.cooldownMinutes;
      residentPlates = cfg.residentPlates;
      nvrDomain = "nvr.${config.lanbat.deployment.domain}";

      # Misparking detection. Draw the bays in the Frigate zone editor and paste the
      # normalized coordinates here; `box` arrives in pixels, hence the frame size.
      frameWidth = cfg.frameWidth or 1920;
      frameHeight = cfg.frameHeight or 1080;
      objectLabels = cfg.objectLabels or [ "car" ];
      evaluationMode = cfg.evaluationMode or "hybrid";
      settleFrames = cfg.settleFrames or 30;
      minSlotCoverage = cfg.minSlotCoverage or 0.4;
      straddleCoverage = cfg.straddleCoverage or 0.25;
      forwardTolerance = cfg.forwardTolerance or 0.08;
      forwardAxis = cfg.forwardAxis or "down";
      slots = cfg.slots or [ ];
      areas = cfg.areas or [ ];

      enforcement = {
        enabled = cfg.enforcement.enabled or true;
        whenBookingsAtLeast = cfg.enforcement.whenBookingsAtLeast or 2;
        alwaysEnforceForbidden = cfg.enforcement.alwaysEnforceForbidden or true;
        repeatMinutes = cfg.enforcement.repeatMinutes or 30;
        resolveGraceSeconds = cfg.enforcement.resolveGraceSeconds or 60;
      };

      notify = {
        # SMS last: every other rung is free, this one is billed per message.
        clientLadder = cfg.notify.clientLadder or [
          "whatsapp"
          "telegram"
          "email"
          "sms"
        ];
        # ntfy and Pushover are operator-only and never address a client.
        ownerChannels = cfg.notify.ownerChannels or [
          "ntfy"
          "pushover"
          "telegram"
        ];
        stepTimeoutSeconds = cfg.notify.stepTimeoutSeconds or 180;
        ackGraceMinutes = cfg.notify.ackGraceMinutes or 10;
        escalateAfterMinutes = cfg.notify.escalateAfterMinutes or 15;
        maxRounds = cfg.notify.maxRounds or 2;
        siteName = cfg.notify.siteName or "the driveway";
      };

      ack = {
        baseUrl = cfg.ack.baseUrl or "https://ntfy.sh";
        # Unguessable topic used purely as an inbound relay, so no port is opened.
        topic = cfg.ack.topic or "";
        pollIntervalSeconds = cfg.ack.pollIntervalSeconds or 25;
        telegramPolling = cfg.ack.telegramPolling or true;
      };

      contacts = {
        overridesPath = "${stateDir}/contacts.yaml";
        retentionDays = cfg.contacts.retentionDays or 7;
        defaultCountryCode = cfg.contacts.defaultCountryCode or "+44";
      };
    }
  );

  secretEnvironment = [
    "PARKING_GUARD_NOTIFY_ENV=${config.age.secrets.parking-guard-notify-env.path}"
    "PARKING_GUARD_MESSAGING_ENV=${config.age.secrets.parking-guard-messaging-env.path}"
  ];
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
        parking-guard-messaging-env = { };
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
        "read frigate/events"
        # Home Assistant flips match-day enforcement through this topic.
        "read parking-guard/command/#"
        "write parking-guard/#"
      ];
    };

    systemd.tmpfiles.rules = [
      "d ${stateDir} 0750 parking-guard parking-guard -"
      "d ${stateDir}/import 0750 parking-guard parking-guard -"
      # Client phone numbers and email addresses live here and in the database.
      "f ${stateDir}/contacts.yaml 0640 parking-guard parking-guard -"
    ]
    ++ lib.optional wahaEnabled "d ${stateDir}/waha 0750 parking-guard parking-guard -";

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
        # Channel credentials are read from the files directly rather than through
        # EnvironmentFile, which is fussy about quoting in tokens.
        Environment = secretEnvironment;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ stateDir ];
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
        ] ++ secretEnvironment;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ stateDir ];
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
        PathChanged = "${stateDir}/import/bookings.csv";
        Unit = "parking-guard-csv-import.service";
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
        ] ++ secretEnvironment;
      };
    };

    # Optional: a self-hosted WhatsApp Web session, for deployments that would rather
    # not run a Meta Business account. Unofficial, and the number can be banned.
    virtualisation.oci-containers.containers = lib.mkIf wahaEnabled {
      parking-guard-waha = {
        image = wahaCfg.image or "devlikeapro/waha:latest";
        ports = [ "127.0.0.1:${toString (wahaCfg.port or 3000)}:3000" ];
        volumes = [ "${stateDir}/waha:/app/.sessions" ];
        environment = {
          WHATSAPP_DEFAULT_ENGINE = wahaCfg.engine or "WEBJS";
        };
      };
    };
  };
}
